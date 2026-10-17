import logging
import re
from urllib.parse import urljoin, urlparse

from config import get_solver_proxy_url, is_direct_connection_allowed
from extractors.base import BaseExtractor, ExtractorError
from services.flaresolverr import FlareSolverrError, solve_cloudflare
from utils.packed import eval_solver

logger = logging.getLogger(__name__)

_TURNSTILE_GATE_RE = re.compile(
    r"challenges\.cloudflare\.com/turnstile|cf-turnstile", re.IGNORECASE
)
_EMBED_CODE_RE = re.compile(r"/e/([A-Za-z0-9_-]+)")

_MEDIA_PATTERNS = [
    r'file:"(.*?)"',
    r'sources:\s*\[\s*\{\s*file:\s*"([^"]+)"',
    r'https?://[^"\'\s]+\.m3u8[^"\'\s]*',
    r'https?://[^"\'\s]+\.mp4[^"\'\s]*',
]


class DroploadExtractor(BaseExtractor):
    """Dropload URL extractor.

    The embed page sits behind a Cloudflare Turnstile gate. FlareSolverr
    clicks the widget; the page callback then submits ``op=embed`` and the
    browser player immediately consumes the stream token in the returned
    page.  Re-submitting the form over HTTP with the solver cookies (which
    carry the cleared ``vcap`` captcha marker) returns a fresh player page
    whose token has not been consumed yet.
    """

    TURNSTILE_TABS = 1
    REQUEST_TIMEOUT_TOTAL = 90
    # The Turnstile solve happens in an IPv4-only browser and the cleared
    # ``vcap`` marker is IP-bound, so every request must leave via IPv4.
    force_ipv4 = True

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="dropload", bypass_warp=bypass_warp)

    @staticmethod
    def _extract_m3u8(text: str) -> str | None:
        match = re.search(r'https?://[^"\'\s]+\.m3u8[^"\'\s]*', text)
        return match.group(0) if match else None

    async def _solve_turnstile(self, url: str):
        """Return ``(html, cookie_header, user_agent)`` for a solved page."""
        proxy = (
            None
            if (self._force_direct or self.bypass_warp_active) and not self._session_proxy
            else get_solver_proxy_url(self._session_proxy)
        )
        allow_direct = self._force_direct or is_direct_connection_allowed(self.bypass_warp_active)
        html = ""
        cookie_header = ""
        user_agent = ""
        for _ in range(2):
            try:
                solution = await solve_cloudflare(
                    url,
                    proxy_url=proxy,
                    allow_direct=allow_direct,
                    tabs_till_verify=self.TURNSTILE_TABS,
                )
            except FlareSolverrError as exc:
                raise ExtractorError(
                    f"Dropload extraction failed: FlareSolverr unavailable: {exc}"
                ) from exc
            html = solution.response or ""
            cookie_header = solution.cookie_header
            user_agent = solution.user_agent
            if not _TURNSTILE_GATE_RE.search(html):
                break
            logger.warning("Dropload: Turnstile still present in solver response, retrying")
        return html, cookie_header, user_agent

    async def _fetch_player_page(
        self, url: str, code: str, cookie_header: str, user_agent: str
    ) -> str:
        """Re-submit the embed form over HTTP for an unconsumed stream token."""
        parsed = urlparse(url)
        headers = {
            "User-Agent": user_agent or self.base_headers["User-Agent"],
            "Referer": url,
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        resp = await self._make_request(
            url,
            method="POST",
            headers=headers,
            data={"op": "embed", "file_code": code},
        )
        return resp.text or ""

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Dropload URL."""
        self._apply_routing_kwargs(url, kwargs)

        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Referer": referer,
            "User-Agent": self.base_headers["User-Agent"],
        }

        session = await self._get_session(url)
        resp = await self._make_request(url, headers=headers)
        html = resp.text or ""
        extra_headers = {}

        if _TURNSTILE_GATE_RE.search(html):
            logger.info("Dropload: Turnstile gate detected, trying FlareSolverr")
            solved_html, cookie_header, user_agent = await self._solve_turnstile(url)
            if _TURNSTILE_GATE_RE.search(solved_html):
                raise ExtractorError(
                    "Dropload extraction failed: Cloudflare Turnstile challenge "
                    "not solved by FlareSolverr"
                )
            if user_agent:
                extra_headers["User-Agent"] = user_agent
            if cookie_header:
                extra_headers["Cookie"] = cookie_header

            code_match = _EMBED_CODE_RE.search(parsed.path or "")
            html = solved_html
            if code_match:
                try:
                    fresh_html = await self._fetch_player_page(
                        url, code_match.group(1), cookie_header, user_agent
                    )
                    if "m3u8" in fresh_html or "mp4" in fresh_html:
                        html = fresh_html
                except ExtractorError as exc:
                    logger.warning("Dropload: fresh player page failed, using solver page: %s", exc)

        final_url = self._extract_m3u8(html)
        if not final_url:
            mp4_match = re.search(r'https?://[^"\'\s]+\.mp4[^"\'\s]*', html)
            if mp4_match:
                final_url = mp4_match.group(0)

        if not final_url:
            try:
                final_url = await eval_solver(
                    session, resp.url, headers, _MEDIA_PATTERNS, text=html
                )
            except Exception:
                final_url = None

        if not final_url:
            raise ExtractorError("Dropload extraction failed: no media URL found")

        req_headers = {
            **self.base_headers,
            **extra_headers,
            "referer": url,
            "origin": referer.rstrip("/"),
        }
        mediaflow_endpoint = "proxy_stream_endpoint" if ".mp4" in final_url else self.mediaflow_endpoint

        return {
            "destination_url": urljoin(url, final_url),
            "request_headers": req_headers,
            "mediaflow_endpoint": mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

    async def close(self):
        await super().close()
