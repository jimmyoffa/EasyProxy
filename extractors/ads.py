import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlparse

from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

ADS_CONFIG_URL = "https://raw.githubusercontent.com/realbestia1/domains/refs/heads/main/domains.json"
ADS_COOKIE = "sid=518cf65bd4bfc95de0d6d58d186fa39e3417eadd34a4d514e7437fc21b85411e"
ADS_HOST_PATTERN = re.compile(r"^(?:www\.)?altadefinizionestreaming\.[a-z]{2,}$")
ADS_FILM_PATTERN = re.compile(r"/film/.+-(\d+)/?")
ADS_SERIES_PATTERN = re.compile(r"/serie-tv/(?:.+-)?(\d+)(?:/(\d+)/(\d+))?/?")
_ADS_CONFIG_TTL = 60
_ads_cookie = ""
_ads_origin = ""
_ads_config_loaded_at = 0.0


def ads_configured_host() -> str:
    """Host of the configured ADS domain (empty until the config is loaded)."""
    return (urlparse(_ads_origin).hostname or "").lower()


class ADSExtractor(BaseExtractor):
    """Resolve AltadefinizioneStreaming's IP-bound CDN URLs."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="ads", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    @staticmethod
    def _cookie_from_kwargs(kwargs: dict) -> str:
        for key, value in kwargs.items():
            if key.lower() == "h_cookie":
                return str(value or "").strip()
        return ""

    async def _remote_config(self) -> tuple[str, str]:
        """Read ADS domain and cookie from the shared domains config.

        Falls back on the request URL origin and the packaged cookie when the
        config cannot be loaded.
        """
        global _ads_cookie, _ads_origin, _ads_config_loaded_at
        if _ads_cookie and time.monotonic() - _ads_config_loaded_at < _ADS_CONFIG_TTL:
            return _ads_cookie, _ads_origin
        try:
            response = await self._make_request(
                ADS_CONFIG_URL,
                headers={"Accept": "application/json"},
            )
            config = json.loads(response.text)
            cookie = str(config.get("ADS_COOKIE") or "").strip()
            if cookie:
                _ads_cookie = cookie
            domain = str(config.get("ADS") or "").strip()
            parsed_domain = urlparse(domain if "://" in domain else f"https://{domain}")
            if parsed_domain.hostname:
                _ads_origin = f"{parsed_domain.scheme or 'https'}://{parsed_domain.netloc}"
            _ads_config_loaded_at = time.monotonic()
        except Exception as exc:
            logger.warning("Unable to refresh ADS config: %s", exc)
        return _ads_cookie or ADS_COOKIE, _ads_origin

    async def extract(self, url: str, **kwargs) -> dict:
        self._apply_routing_kwargs(url, kwargs)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"}:
            raise ExtractorError("ADS: invalid URL")

        request_query = {key: value for key, value in parse_qsl(parsed.query)} if parsed.query else {}
        remote_cookie, remote_origin = await self._remote_config()
        configured_host = urlparse(remote_origin).hostname or ""
        if not ADS_HOST_PATTERN.fullmatch(host) and host != configured_host:
            raise ExtractorError("ADS: invalid URL")

        origin = remote_origin or f"{parsed.scheme}://{parsed.netloc}"
        if parsed.path.startswith("/api/player-sources/"):
            sources_url = f"{origin}{parsed.path}"
            if parsed.query:
                sources_url += f"?{parsed.query}"
        else:
            film_match = ADS_FILM_PATTERN.fullmatch(parsed.path)
            series_match = ADS_SERIES_PATTERN.fullmatch(parsed.path)
            if film_match:
                sources_url = f"{origin}/api/player-sources/movie/{film_match.group(1)}"
            elif series_match:
                season = (
                    series_match.group(2)
                    or str(kwargs.get("season") or request_query.get("season") or "1")
                )
                episode = (
                    series_match.group(3)
                    or str(kwargs.get("episode") or request_query.get("episode") or "1")
                )
                sources_url = (
                    f"{origin}/api/player-sources/tv/{series_match.group(1)}/{season}/{episode}"
                )
            else:
                raise ExtractorError("ADS: unsupported direct URL")

        cookie = self._cookie_from_kwargs(kwargs) or remote_cookie
        if not cookie:
            raise ExtractorError("ADS: cookie unavailable")

        api_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": f"{origin}/",
            "Accept": "application/json,text/plain,*/*",
            "Cookie": cookie,
        }
        response = await self._make_request(sources_url, headers=api_headers)
        payload = response.json
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, list):
            raise ExtractorError("ADS: invalid player-sources response")

        source = next(
            (
                item for item in sources
                if isinstance(item, dict)
                and str(item.get("provider") or "").lower() == "cdn"
                and item.get("url")
            ),
            None,
        )
        stream_url = str(source.get("url") if source else "").strip()
        stream_parsed = urlparse(stream_url)
        if stream_parsed.scheme not in {"http", "https"} or not stream_parsed.hostname:
            raise ExtractorError("ADS: CDN source not found")

        return {
            "destination_url": stream_url,
            "request_headers": {
                "User-Agent": self.base_headers["User-Agent"],
                "Referer": f"{origin}/",
                "Accept": "*/*",
            },
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }
