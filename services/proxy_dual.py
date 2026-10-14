"""One-call DUAL synchronisation for direct links and EasyProxy extractors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import urllib.parse
from typing import Any
from urllib.parse import urljoin

import aiohttp

import config_store
from config import check_password, resolve_proxy_alias
from services.proxy_shared import (
    BYPASS_PROXIES_CONTEXT,
    BYPASS_WARP_CONTEXT,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    ClientTimeout,
    ManifestRewriter,
    get_public_base_url,
    logger,
    web,
)
from services.dual import service as dual_service
from services.dual.security import resolves_publicly, valid_public_url
from services.dual.video import video_track_id


class DualLinksError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_LANGUAGE_ALIASES = {
    # Values come from the HLS master LANGUAGE/NAME attributes.
    "eng": {
        "en", "eng", "english", "inglese",
        "english #2", "english 5.1 (dd+)", "english 5.1 (dd+) #2",
    },
    "ita": {
        "it", "ita", "italian", "italiano", "italian 5.1 (dd+)",
    },
    "spa": {"es", "spa", "spanish", "spagnolo", "spanish 5.1 (dd+)"},
    "fra": {
        "fr", "fra", "french", "francese", "french #2",
        "french 5.1 (dd+)", "french 5.1 (dd+) #2",
    },
    "deu": {"de", "deu", "ger", "german", "tedesco", "german 5.1 (dd+)"},
    "hin": {"hi", "hin", "hindi", "hindi 5.1 (dd+)"},
    "rus": {"ru", "rus", "russian", "russo", "russian 5.1 (dd+)"},
}
_SAFE_HEADERS = re.compile(r"^[A-Za-z0-9-]+$")
_DUAL_SYNC_TIMEOUT_SECONDS = 120


def _dual_error_code(error: BaseException) -> str:
    """Map DUAL failures to stable API error codes."""
    status = int(getattr(error, "status", 0) or 0)
    message = str(getattr(error, "message", "") or error).lower()
    if status == 409 or any(
        marker in message
        for marker in ("sync", "synchron", "incompatible", "offset")
    ):
        return "sync_unavailable"
    if "audio" in message and status in {0, 400, 404, 410, 422, 502}:
        return "audio_unavailable"
    return "dual_error"


def _normalise_language(value: Any) -> str:
    raw = str(value or "").strip().lower()
    for canonical, aliases in _LANGUAGE_ALIASES.items():
        if raw in aliases:
            return canonical
    return raw


def _attrs(line: str) -> dict[str, str]:
    """Parse the comma-separated HLS attribute list, preserving quoted commas."""
    result: dict[str, str] = {}
    for match in re.finditer(r'([A-Z0-9-]+)="([^"]*)"|([A-Z0-9-]+)=([^,]*)', line):
        key = match.group(1) or match.group(3)
        value = match.group(2) if match.group(1) else match.group(4)
        result[key] = value.strip()
    return result


def _safe_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DualLinksError(400, "headers must be an object")
    headers: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name)
        value = str(raw_value).strip()
        if not _SAFE_HEADERS.fullmatch(name) or not value or "\r" in value or "\n" in value:
            raise DualLinksError(400, "invalid source header")
        if len(value) > 1024:
            raise DualLinksError(400, "source header is too long")
        headers[name] = value
    return headers


def _is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF:" in text or "#EXT-X-MEDIA:" in text


def _playlist_durations(text: str) -> list[float]:
    durations: list[float] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#EXTINF:"):
            continue
        try:
            durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
        except ValueError:
            return []
    return durations


def _same_audio_timeline(left: str, right: str) -> bool:
    """Match renditions of the same edit even when encoders split segments differently.

    Segment boundaries differ per encoder, so identical EXTINF values are too
    strict. A cumulative drift bound keeps clearly different edits out while
    still accepting the same track encoded twice (e.g. ita/eng of one provider).
    """
    left_durations = _playlist_durations(left)
    right_durations = _playlist_durations(right)
    if not left_durations or len(left_durations) != len(right_durations):
        return False
    drift = 0.0
    for a, b in zip(left_durations, right_durations):
        drift += a - b
        if abs(drift) > 5.0:
            return False
    return True


def _master_entries(text: str, base_url: str) -> tuple[list[dict], list[dict]]:
    variants: list[dict] = []
    audios: list[dict] = []
    pending_variant: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            attributes = _attrs(line.split(":", 1)[1])
            if attributes.get("TYPE", "").upper() == "AUDIO":
                uri = attributes.get("URI")
                if uri:
                    attributes["url"] = urljoin(base_url, uri)
                    audios.append(attributes)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            attributes = _attrs(line.split(":", 1)[1])
            width, height = 0, 0
            resolution = attributes.get("RESOLUTION", "")
            if "x" in resolution.lower():
                try:
                    width, height = (int(item) for item in re.split("x", resolution, flags=re.I))
                except ValueError:
                    pass
            pending_variant = {
                "attributes": attributes,
                "width": width,
                "height": height,
            }
        elif pending_variant is not None and line and not line.startswith("#"):
            pending_variant["url"] = urljoin(base_url, line)
            variants.append(pending_variant)
            pending_variant = None
    return variants, audios


def _language_match(item: dict, wanted: str) -> bool:
    values = {
        str(item.get("LANGUAGE") or "").lower(),
        str(item.get("NAME") or "").lower(),
        str(item.get("GROUP-ID") or "").lower(),
    }
    aliases = _LANGUAGE_ALIASES.get(wanted, {wanted})
    return any(value in aliases or any(alias in value for alias in aliases) for value in values)


def _audio_quality(item: dict) -> tuple[int, int, int, int]:
    """Prefer the best rendition when the user selects only a language."""
    name = str(item.get("NAME") or "").lower()
    try:
        channels = int(str(item.get("CHANNELS") or "0").split(",", 1)[0])
    except ValueError:
        channels = 0
    surround = 1 if channels >= 6 or any(value in name for value in ("5.1", "7.1", "surround")) else 0
    codec = 2 if any(value in name for value in ("eac3", "dd+", "dolby", "atmos")) else 1 if "ac-3" in name else 0
    return surround, codec, channels, len(name)


class HLSProxyDualMixin:
    """Resolve video/audio sources, then run the in-process DUAL pipeline."""

    @staticmethod
    def _spec_url(spec: dict) -> str:
        value = spec.get("url") or spec.get("d")
        if not value:
            raise DualLinksError(400, "source url is required")
        value = str(value).strip()
        if not valid_public_url(value, require_https=False):
            raise DualLinksError(400, "source url must be a public http(s) URL")
        return value

    @staticmethod
    def _stable_spec_id(spec: Any) -> str:
        """ID stabile per la cache: pagina + extractor, mai URL estratti (token)."""
        if isinstance(spec, dict):
            extractor = str(spec.get("extractor") or spec.get("host") or "").strip().lower()
            url = str(spec.get("d") or spec.get("url") or "").strip()
            return f"{extractor}|{url}"
        return str(spec or "").strip()

    @staticmethod
    def _routing(spec: dict) -> tuple[bool, bool, str | None]:
        raw_proxy = str(spec.get("proxy") or spec.get("proxy_url") or "").strip()
        proxy_off = raw_proxy.lower() == "off" or bool(spec.get("proxy_off"))
        forced_proxy = (
            None
            if proxy_off or not raw_proxy
            else resolve_proxy_alias(urllib.parse.unquote(raw_proxy))
        )
        warp_off = str(spec.get("warp") or "").lower() == "off" or bool(spec.get("warp_off"))
        return warp_off, proxy_off, forced_proxy

    async def _resolve_dual_spec(self, spec: Any, required_resolution: int = 0) -> dict:
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, dict):
            raise DualLinksError(400, "source must be an object or URL string")

        target_url = self._spec_url(spec)
        headers = _safe_headers(spec.get("headers"))
        extractor_name = str(spec.get("extractor") or spec.get("host") or "").strip().lower()
        warp_off, proxy_off, forced_proxy = self._routing(spec)
        if not extractor_name:
            if not await resolves_publicly(target_url, require_https=False):
                raise DualLinksError(400, "source URL does not resolve publicly")
            return {
                "url": target_url,
                "headers": headers,
                "extractor_name": "",
                "warp_off": warp_off,
                "proxy_off": proxy_off,
                "forced_proxy": forced_proxy,
            }

        # Apply the same admin routing overrides used by the public extractor
        # endpoint. DUAL resolves extractors in-process, so it cannot rely on
        # handle_extractor_request to apply these settings for it.
        extractor_key = extractor_name.replace("_direct", "").replace("_noproxy", "")
        warp_off_extractors = {
            str(value).strip().lower()
            for value in config_store.get("warp_off_extractors", [])
        }
        proxy_off_extractors = {
            str(value).strip().lower()
            for value in config_store.get("proxy_off_extractors", [])
        }
        if extractor_key in warp_off_extractors:
            warp_off = True
        if extractor_key in proxy_off_extractors:
            proxy_off = True

        bypass_token = BYPASS_WARP_CONTEXT.set(warp_off)
        proxy_bypass_token = BYPASS_PROXIES_CONTEXT.set(proxy_off)
        selected_token = SELECTED_PROXY_CONTEXT.set(forced_proxy)
        strict_token = STRICT_PROXY_CONTEXT.set(bool(forced_proxy))
        extractor = None
        extractor_key = None
        try:
            extractor = await self.get_extractor(
                target_url,
                headers,
                host=extractor_name,
                bypass_warp=warp_off,
            )
            extract_kwargs = {
                "request_headers": headers,
                "bypass_warp": warp_off,
                "proxy": forced_proxy,
            }
            if required_resolution > 0:
                extract_kwargs["required_resolution"] = required_resolution
            result = await extractor.extract(target_url, **extract_kwargs)
            extractor_key = self._extractor_key_for_instance(extractor)
            base_name = (extractor_key or extractor_name).replace("_direct", "").replace("_noproxy", "")
            selected_proxy = result.get("selected_proxy")
            if not selected_proxy:
                selected_proxy = (
                    getattr(extractor, "last_used_proxy", None)
                    or getattr(extractor, "selected_proxy", None)
                    or getattr(extractor, "_session_proxy", None)
                    or getattr(extractor, "session_proxy", None)
                )
            stream_url = str(result.get("destination_url") or "").strip()
            if not stream_url or not valid_public_url(stream_url, require_https=False):
                raise DualLinksError(502, "extractor returned an invalid media URL")
            if not await resolves_publicly(stream_url, require_https=False):
                raise DualLinksError(502, "extractor media URL does not resolve publicly")
            if warp_off and selected_proxy and "127.0.0.1" in selected_proxy:
                selected_proxy = None
            return {
                "url": stream_url,
                "headers": _safe_headers(result.get("request_headers") or headers),
                "extractor_name": base_name,
                "warp_off": bool(result.get("bypass_warp", warp_off)),
                "proxy_off": proxy_off,
                "forced_proxy": selected_proxy or forced_proxy,
                "manifest": result.get("captured_manifest") or "",
            }
        except DualLinksError:
            raise
        except Exception as exc:
            raise DualLinksError(502, f"extractor failed: {type(exc).__name__}: {exc}") from exc
        finally:
            # Shared extractor lifecycle belongs to the registry owner.
            BYPASS_WARP_CONTEXT.reset(bypass_token)
            BYPASS_PROXIES_CONTEXT.reset(proxy_bypass_token)
            SELECTED_PROXY_CONTEXT.reset(selected_token)
            STRICT_PROXY_CONTEXT.reset(strict_token)

    async def _fetch_dual(self, source: dict, *, binary: bool = False) -> tuple[Any, str]:
        url = str(source["url"])
        bypass_token = BYPASS_WARP_CONTEXT.set(bool(source.get("warp_off")))
        proxy_bypass_token = BYPASS_PROXIES_CONTEXT.set(bool(source.get("proxy_off")))
        selected_token = SELECTED_PROXY_CONTEXT.set(source.get("forced_proxy"))
        strict_token = STRICT_PROXY_CONTEXT.set(bool(source.get("forced_proxy")))
        try:
            session, _ = await self._get_proxy_session(
                url,
                bypass_warp=bool(source.get("warp_off")),
                forced_proxy=source.get("forced_proxy"),
                session_key=source.get("stream_key"),
            )
            async with session.get(
                url,
                headers=source.get("headers") or {},
                allow_redirects=True,
                timeout=ClientTimeout(total=45, connect=15, sock_connect=15, sock_read=45),
            ) as response:
                if response.status >= 400:
                    logger.warning("[DUAL] source fetch failed: %s -> HTTP %s", url, response.status)
                    raise DualLinksError(502, f"source returned HTTP {response.status}")
                final_url = str(response.url)
                if binary:
                    return await response.read(), final_url
                return await response.text(), final_url
        except DualLinksError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DualLinksError(502, f"source fetch failed: {type(exc).__name__}") from exc
        finally:
            BYPASS_WARP_CONTEXT.reset(bypass_token)
            BYPASS_PROXIES_CONTEXT.reset(proxy_bypass_token)
            SELECTED_PROXY_CONTEXT.reset(selected_token)
            STRICT_PROXY_CONTEXT.reset(strict_token)

    async def _manifest(self, source: dict) -> tuple[str, str]:
        captured = str(source.get("manifest") or "")
        if captured:
            return captured, str(source["url"])
        text, final_url = await self._fetch_dual(source)
        return str(text), final_url

    @staticmethod
    def _pick_video(text: str, base_url: str, requested: int) -> tuple[str, int, str | None, bool]:
        variants, audios = _master_entries(text, base_url)
        if not variants:
            return base_url, requested or 1080, None, False
        def is_4k(item: dict) -> bool:
            return (item.get("width") or 0) >= 3800 or (item.get("height") or 0) >= 2144

        exact = [item for item in variants if is_4k(item)] if requested >= 2160 else []
        if not exact:
            target = requested or max(item["height"] for item in variants)
            exact = [item for item in variants if item["height"] == target]
        candidates = exact or sorted(
            variants,
            key=lambda item: (abs((item["height"] or 1080) - (requested or 1080)), -(item["height"] or 0)),
        )
        selected = candidates[0]
        group = selected["attributes"].get("AUDIO", "")
        reference = None
        muxed_reference = False
        reference_candidates = [item for item in audios if not group or item.get("GROUP-ID") == group]
        if reference_candidates:
            english = [item for item in reference_candidates if _language_match(item, "eng")]
            reference = (english or reference_candidates)[0].get("url")
        elif len(variants) > 1:
            reference = min(
                variants,
                key=lambda item: (item["height"] or 10_000, item["width"] or 10_000),
            )["url"]
            muxed_reference = reference != selected["url"]
        return selected["url"], 2160 if is_4k(selected) else (selected["height"] or target or 1080), reference, muxed_reference

    @staticmethod
    def _pick_audio(
        text: str,
        base_url: str,
        language: str,
        requested_alias: str = "",
        bypass_language: bool = False,
    ) -> tuple[str, dict]:
        if not _is_master(text):
            return base_url, {"manifest": text, "base_url": base_url}
        variants, audios = _master_entries(text, base_url)
        requested_alias = str(requested_alias or "").strip().lower()
        exact_alias = [
            item for item in audios
            if str(item.get("NAME") or "").strip().lower() == requested_alias
        ] if requested_alias else []
        matches = exact_alias or [item for item in audios if _language_match(item, language)]
        if not matches:
            if bypass_language and not audios and variants:
                def variant_quality(item: dict) -> tuple[int, int]:
                    try:
                        bandwidth = int(item.get("attributes", {}).get("BANDWIDTH") or 0)
                    except (TypeError, ValueError):
                        bandwidth = 0
                    return item.get("height") or 0, bandwidth

                selected = max(variants, key=variant_quality)
                logger.warning(
                    "[DUAL] no separate audio renditions; bypass enabled, using muxed variant %s",
                    selected.get("url") or "",
                )
                return selected["url"], {
                    "language": language,
                    "name": "Muxed audio",
                    "hls_language": language,
                    "language_bypassed": True,
                    "muxed_audio": True,
                }
            if bypass_language and audios:
                fallback_pool = [
                    item for item in audios
                    if str(item.get("DEFAULT") or "").strip().upper() == "YES"
                ] or audios
                selected = max(
                    fallback_pool,
                    key=lambda item: (
                        str(item.get("DEFAULT") or "").strip().upper() == "YES",
                        *_audio_quality(item),
                    ),
                )
                logger.warning(
                    "[DUAL] audio language '%s' not matched; bypass enabled, using '%s'",
                    language,
                    selected.get("NAME") or selected.get("URI") or "default track",
                )
                return selected["url"], {
                    "language": language,
                    "name": selected.get("NAME") or "",
                    "hls_language": selected.get("LANGUAGE") or language,
                    "language_bypassed": True,
                }
            available = sorted({item.get("LANGUAGE") or item.get("NAME") or "unknown" for item in audios})
            suffix = ", ".join(available[:12]) or "none"
            raise DualLinksError(400, f"audio language '{language}' not found; available: {suffix}")
        selected = matches[0] if exact_alias else max(matches, key=_audio_quality)
        return selected["url"], {
            "language": language,
            "name": selected.get("NAME") or "",
            "hls_language": selected.get("LANGUAGE") or "",
        }

    @staticmethod
    def _audio_key(playlist: str) -> str:
        for raw in playlist.splitlines():
            line = raw.strip()
            if not line.startswith("#EXT-X-KEY:"):
                continue
            attrs = _attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "").upper()
            if method == "NONE":
                return ""
            if method != "AES-128" or not attrs.get("URI"):
                raise DualLinksError(400, "selected audio uses unsupported encryption")
            return attrs["URI"]
        return ""

    @staticmethod
    def _cached_sync_result(
        lookup: dict | None,
        cache_key: str,
        reference_audio_url: str,
        validate_muxed_reference: bool,
    ) -> dict | None:
        """Accept only the same cache entries accepted by SyncEngine.

        Prefer the reference_matches_video marker. Entries written before the
        offset API persisted it fall back to the measured video_start_time.
        """
        if not isinstance(lookup, dict):
            return None
        details = lookup.get("details") or lookup
        if not isinstance(details, dict) or str(
            details.get("status") or lookup.get("status") or ""
        ).lower() != "ok":
            return None
        reference_validated = details.get("reference_matches_video")
        if validate_muxed_reference and reference_validated is False:
            return None
        if (
            reference_audio_url
            and reference_validated is not True
            and "video_start_time" not in details
        ):
            return None
        return {"status": "ok", "cached": True, **details, "cache_key": cache_key}

    async def _prepare_dual_audio(
        self,
        request,
        source: dict,
        playlist: str,
        playlist_base: str,
        token: str,
        media_key: str,
        language: str,
    ) -> dict:
        key_uri = self._audio_key(playlist)
        key_bytes = b""
        if key_uri:
            key_source = dict(source)
            key_source["url"] = urljoin(playlist_base, key_uri)
            key_bytes, _ = await self._fetch_dual(key_source, binary=True)
            if len(key_bytes) != 16:
                raise DualLinksError(400, "audio AES key must be 16 bytes")
        prepared = await self._dual_json(
            request,
            "POST",
            "/dual/aprep",
            {
                "token": token,
                "playlist": playlist,
                "key": base64.b64encode(key_bytes).decode(),
                "mediaKey": media_key,
                "lang": language,
                "baseUrl": playlist_base,
                "headers": source.get("headers") or {},
                "warp_off": bool(source.get("warp_off")),
                "proxy_off": bool(source.get("proxy_off")),
                "proxy_url": source.get("forced_proxy") or "",
                "extractor_name": source.get("extractor_name") or "",
            },
        )
        # cleanup_idle evicts tracks after TRACK_IDLE_SECONDS without segment
        # requests, and a bridge sync can keep this track idle far longer.
        # Pin it until the DUAL build finishes (unpinned by the caller).
        hid = str(prepared.get("hid") or "")
        if hid:
            dual_service.audio.pin(hid)
        return prepared

    async def _dual_json(self, request, method: str, path: str, body: dict | None = None) -> dict:
        if method != "POST":
            raise DualLinksError(500, "unsupported internal DUAL method")
        try:
            if path == "/session":
                return await dual_service.create_session_data()
            if path == "/dual/aprep":
                return await dual_service.prepare_audio_data(body or {}, request)
            if path == "/sync":
                return await dual_service.sync_audio_data(body or {}, request)
            raise DualLinksError(500, f"unsupported internal DUAL path: {path}")
        except dual_service.DualServiceError as exc:
            raise DualLinksError(exc.status, str(exc.detail)) from exc

    async def _dual_sync_json(self, request, body: dict) -> dict:
        """Bound sync work so the public HLS request can return its error playlist."""
        try:
            return await asyncio.wait_for(
                self._dual_json(request, "POST", "/sync", body),
                timeout=_DUAL_SYNC_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise DualLinksError(409, "DUAL sync timed out") from exc

    @staticmethod
    def _decode_dual_descriptor(value: str) -> dict:
        encoded = str(value or "").strip()
        if not encoded or len(encoded) > 256 * 1024:
            raise DualLinksError(400, "dual descriptor is missing or too large")
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(encoded + padding)
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DualLinksError(400, "invalid Base64 JSON dual descriptor") from exc
        if not isinstance(body, dict):
            raise DualLinksError(400, "dual descriptor must contain a JSON object")
        return body

    @staticmethod
    def _audio_url_with_sync(url: str, sync: dict) -> str:
        parts = urllib.parse.urlsplit(str(url))
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        try:
            offset_ms = int(round(float(sync.get("offset") or 0) * 1000))
        except (TypeError, ValueError):
            offset_ms = 0
        try:
            rate_nano = int(round(float(sync.get("rate") or 1) * 1_000_000_000))
        except (TypeError, ValueError):
            rate_nano = 1_000_000_000
        query.update({"o": str(offset_ms), "r": str(rate_nano)})
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

    @staticmethod
    def _dual_video_proxy_url(request, video_url: str, video: dict, track_id: int = 0) -> str:
        params = {
            "url": video_url,
            "redirect_stream": "true",
            "direct_hls": "1",
        }
        for name, value in (video.get("headers") or {}).items():
            params[f"h_{name}"] = value
        if video.get("warp_off"):
            params["warp"] = "off"
        if video.get("proxy_off"):
            params["proxy"] = "off"
        elif video.get("forced_proxy"):
            params["proxy"] = video["forced_proxy"]
        if video.get("stream_key"):
            params["stream_key"] = video["stream_key"]
        if track_id:
            params["dual_video_track"] = str(track_id)
        api_password = request.query.get("api_password")
        if api_password:
            params["api_password"] = api_password
        return (
            f"{get_public_base_url(request)}/proxy/hls/manifest.m3u8?"
            f"{urllib.parse.urlencode(params)}"
        )

    @staticmethod
    def _dual_master(result: dict) -> str:
        resolution = int(result["resolution"])
        width = max(2, round(resolution * 16 / 9))
        bandwidth = 25_000_000 if resolution >= 2160 else 8_000_000
        language = str(result.get("audio_hls_language") or result["audio_lang"])
        name = str(result.get("audio_name") or result["audio_lang"]).replace('"', "'")
        audio_url = str(result["audio_url"]).replace('"', "%22")
        video_url = str(result["video_url"]).replace('"', "%22")
        codecs = str(result.get("video_codecs") or "avc1.640028,mp4a.40.2").replace('"', "")
        return "\n".join([
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="dual-audio",LANGUAGE="{language}",NAME="{name}",DEFAULT=YES,AUTOSELECT=YES,URI="{audio_url}"',
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{resolution},CODECS="{codecs}",AUDIO="dual-audio"',
            video_url,
            "",
        ])

    @staticmethod
    def _video_codecs(text: str, base_url: str, video_url: str) -> str:
        """CODECS reali della variante scelta (hls.js li usa per il SourceBuffer)."""
        try:
            variants, _ = _master_entries(text, base_url)
            for item in variants:
                if item.get("url") != video_url:
                    continue
                raw = str(item["attributes"].get("CODECS", ""))
                video = next((
                    part.strip() for part in raw.split(",")
                    if part.strip().split(".")[0].lower() not in
                    {"mp4a", "ac-3", "ac-4", "ec-3", "opus", "vorbis", "alac"}
                ), "")
                if video:
                    return f"{video},mp4a.40.2"
        except (AttributeError, TypeError, ValueError):
            pass
        return "avc1.640028,mp4a.40.2"

    async def _build_dual_result(self, request, body: dict) -> dict:
        requested_audio_lang = str(
            body.get("audio_lang") or body.get("audioLanguage") or ""
        ).strip()
        bypass_audio_language = body.get(
            "bypass_audio_language", body.get("audio_language_bypass", False)
        )
        bypass_audio_language = (
            bypass_audio_language is True
            or str(bypass_audio_language).strip().lower() in {"1", "true", "yes", "on"}
        )
        audio_lang = _normalise_language(requested_audio_lang)
        if audio_lang not in _LANGUAGE_ALIASES:
            raise DualLinksError(400, "audio_lang is required; use a standard language code such as ita or eng")

        video_spec = body.get("video") or body.get("video_url")
        audio_spec = body.get("audio") or body.get("audio_url")
        requested_resolution = int(body.get("resolution") or 2160)
        required_video_resolution = 2160 if requested_resolution >= 2160 else 0
        # These two sources are independent. Resolve both concurrently so a
        # slow extractor/proxy on one side does not delay starting the other.
        if required_video_resolution:
            video_task = self._resolve_dual_spec(
                video_spec, required_resolution=required_video_resolution
            )
        else:
            video_task = self._resolve_dual_spec(video_spec)
        video, audio = await asyncio.gather(
            video_task,
            self._resolve_dual_spec(audio_spec),
        )
        playback_key = request.query.get("stream_key") or f"dual-{secrets.token_hex(6)}"
        video["stream_key"] = f"{playback_key}-video"
        audio["stream_key"] = f"{playback_key}-audio"

        # Both initial manifests are independent as well. The selected audio
        # playlist is fetched later because its URL comes from audio_text.
        (video_text, video_base), (audio_text, audio_base) = await asyncio.gather(
            self._manifest(video),
            self._manifest(audio),
        )
        video_url, resolution, auto_reference, muxed_reference = self._pick_video(
            video_text, video_base, requested_resolution
        )
        explicit_reference = str(body.get("reference_audio_url") or "").strip()
        reference_audio_url = explicit_reference or auto_reference
        validate_muxed_reference = bool(muxed_reference and not explicit_reference)

        selected_audio_url, audio_meta = self._pick_audio(
            audio_text,
            audio_base,
            audio_lang,
            requested_audio_lang,
            bypass_audio_language,
        )
        audio_media = dict(audio)
        audio_media["url"] = selected_audio_url
        audio_media["manifest"] = ""
        audio_playlist, audio_playlist_base = await self._manifest(audio_media)

        media_key = str(body.get("media_key") or body.get("mediaKey") or "").strip()
        if not media_key:
            media_key = hashlib.sha1(self._stable_spec_id(video_spec).encode()).hexdigest()[:24]
        video_fingerprint = str(body.get("video_fingerprint") or "").strip()
        if not video_fingerprint:
            video_fingerprint = hashlib.sha1(
                f"video|{self._stable_spec_id(video_spec)}".encode()
            ).hexdigest()[:20]

        audio_segments, _, _ = dual_service.audio._parse_playlist(
            audio_playlist, audio_playlist_base
        )
        audio_fingerprint = dual_service.audio.source_fingerprint(audio_segments)
        if not audio_fingerprint:
            raise DualLinksError(400, "selected audio playlist has no segments")
        cache_key = dual_service.offsets.key(
            media_key, resolution, video_fingerprint, audio_fingerprint
        )
        cache_payload = {
            "cache_key": cache_key,
            "media_key": media_key,
            "resolution": resolution,
            "video_fingerprint": video_fingerprint,
            "audio_fingerprint": audio_fingerprint,
            "vpsAccess": body.get("vpsAccess", ""),
        }
        # Check the shared offset cache before bridge audio, session work and
        # the expensive sync. SyncEngine receives the checked flag so misses
        # are not queried a second time later in the same request.
        cache_lookup = await dual_service.offsets.lookup(cache_payload)
        cached_sync = self._cached_sync_result(
            cache_lookup,
            cache_key,
            reference_audio_url,
            validate_muxed_reference,
        )
        if cached_sync:
            logger.info("[DUAL] offset cache hit before audio preparation key=%s", cache_key)

        session_result = await self._dual_json(request, "POST", "/session", {})
        token = str(session_result.get("token") or "")
        if not token:
            raise DualLinksError(502, "DUAL service did not return a session token")

        video_routing = {
            "warp_off": bool(video.get("warp_off")),
            "proxy_off": bool(video.get("proxy_off")),
            "proxy_url": video.get("forced_proxy") or "",
            "extractor_name": video.get("extractor_name") or "",
        }
        def sync_body(candidate: dict) -> dict:
            return {
                "token": token,
                "media_key": media_key,
                "resolution": resolution,
                "video_url": video_url,
                "video_headers": video.get("headers") or {},
                "reference_audio_url": reference_audio_url,
                "validate_muxed_reference": validate_muxed_reference,
                "audio_hid": candidate.get("hid") or "",
                "audio_fingerprint": candidate.get("audio_fingerprint") or "",
                "video_fingerprint": video_fingerprint,
                "_offset_cache_checked": True,
                **video_routing,
            }

        # Register the requested audio while the English bridge sync runs: the
        # registration only depends on the playlist and the session token.
        pinned_hids: list[str] = []

        async def _prepare_and_track(*args) -> dict:
            prepared = await self._prepare_dual_audio(*args)
            hid = str(prepared.get("hid") or "")
            if hid:
                pinned_hids.append(hid)
            return prepared

        prepared_task = asyncio.create_task(
            _prepare_and_track(
                request,
                audio_media,
                audio_playlist,
                audio_playlist_base,
                token,
                media_key,
                audio_lang,
            )
        )

        audio_hid = ""
        synced = cached_sync
        bridge_used = False
        bridge_attempted = False

        def _unpin_all() -> None:
            while pinned_hids:
                dual_service.audio.unpin(pinned_hids.pop())

        try:
            try:
                if synced is None and audio_lang != "eng":
                    try:
                        bridge_url, bridge_meta = self._pick_audio(audio_text, audio_base, "eng")
                        if bridge_url != selected_audio_url:
                            bridge_media = dict(audio)
                            bridge_media["url"] = bridge_url
                            bridge_media["manifest"] = ""
                            bridge_playlist, bridge_base = await self._manifest(bridge_media)
                            if _same_audio_timeline(audio_playlist, bridge_playlist):
                                logger.info(
                                    "[DUAL] English-first sync trying eng='%s' requested=%s",
                                    (bridge_meta.get("name") or "English"),
                                    audio_lang,
                                )
                                bridge = await _prepare_and_track(
                                    request,
                                    bridge_media,
                                    bridge_playlist,
                                    bridge_base,
                                    token,
                                    media_key,
                                    "eng",
                                )
                                bridge_attempted = True
                                bridge_sync = await self._dual_sync_json(
                                    request, sync_body(bridge)
                                )
                                if str(bridge_sync.get("status") or "") == "ok":
                                    synced = bridge_sync
                                    bridge_used = True
                                    report_task = asyncio.create_task(
                                        dual_service.offsets.report(cache_payload, bridge_sync)
                                    )
                                    if hasattr(self, "_background_tasks") and isinstance(self._background_tasks, set):
                                        self._background_tasks.add(report_task)
                                        report_task.add_done_callback(self._background_tasks.discard)
                                    logger.info(
                                        "[DUAL] English-first sync succeeded; using requested %s track via '%s' offset",
                                        audio_lang,
                                        bridge_meta.get("name") or "English",
                                    )
                                else:
                                    logger.info(
                                        "[DUAL] English-first sync rejected; falling back to requested %s track",
                                        audio_lang,
                                    )
                            else:
                                logger.warning("[DUAL] English bridge skipped: ita/eng timeline mismatch")
                        else:
                            logger.info(
                                "[DUAL] English bridge skipped: no separate eng rendition (extractor=%s)",
                                str(audio.get("extractor_name") or ""),
                            )
                    except DualLinksError as exc:
                        logger.warning("[DUAL] English sync bridge unavailable: %s", exc.message)
                if bridge_attempted and not bridge_used:
                    session_result = await self._dual_json(request, "POST", "/session", {})
                    token = str(session_result.get("token") or "")
                    if not token:
                        raise DualLinksError(502, "DUAL service did not return a fallback session token")
            except BaseException:
                prepared_task.cancel()
                raise
            prepared = await prepared_task
            audio_hid = str(prepared.get("hid") or "")
            if not audio_hid:
                raise DualLinksError(502, "DUAL service did not register the selected audio")
            # A bridge sync can outlive the session idle timeout, so rebuild the
            # audio URL on the token that is valid for this request.
            prepared["url"] = dual_service._audio_url(request, audio_hid, token)
            if synced is None or str(synced.get("status") or "") != "ok":
                synced = await self._dual_sync_json(request, sync_body(prepared))
        finally:
            _unpin_all()
        status = str(synced.get("status") or "")
        if status != "ok":
            detail = synced.get("message") or synced.get("detail") or "DUAL sync failed"
            raise DualLinksError(409, str(detail))
        result = {
            "status": status,
            "audio_lang": audio_lang,
            "audio_name": audio_meta.get("name") or "",
            "audio_hls_language": audio_meta.get("hls_language") or audio_lang,
            "resolution": resolution,
            "video_url": video_url,
            "reference_audio_url": (
                reference_audio_url
                if synced.get("reference_matches_video", True)
                else ""
            ),
            "audio_url": self._audio_url_with_sync(str(prepared.get("url") or ""), synced),
            "audio_hid": audio_hid,
            "sync": synced,
            "video_codecs": self._video_codecs(video_text, video_base, video_url),
        }
        if bridge_used:
            result["sync_bridge"] = "eng"
        # Discover the actual video ID; do not assume that video is track 1.
        video_media = dict(video, url=video_url, manifest="")
        media_text, media_base = await self._manifest(video_media)
        map_line = next((line for line in media_text.splitlines() if line.startswith("#EXT-X-MAP:")), "")
        map_uri = re.search(r'URI="([^"]+)"', map_line)
        track_id = 0
        if map_uri:
            init_url = ManifestRewriter._inherit_query_if_missing(
                urljoin(media_base, map_uri.group(1)), urllib.parse.urlparse(media_base).query
            )
            init_source = dict(video_media, url=init_url)
            byte_range = re.search(r'BYTERANGE="(\d+)(?:@(\d+))?"', map_line)
            if byte_range:
                length = int(byte_range.group(1))
                start = int(byte_range.group(2) or 0)
                init_source["headers"] = dict(video_media.get("headers") or {})
                init_source["headers"]["Range"] = f"bytes={start}-{start + length - 1}"
            init_bytes, _ = await self._fetch_dual(init_source, binary=True)
            if byte_range and len(init_bytes) != length:
                # Some origins ignore Range and return the complete object.
                init_bytes = init_bytes[start:start + length]
            try:
                track_id = video_track_id(init_bytes)
            except ValueError as exc:
                raise DualLinksError(502, str(exc)) from exc
        result["video_url"] = self._dual_video_proxy_url(request, video_url, video, track_id)
        result["m3u8"] = self._dual_master(result)
        return result

    async def handle_dual_sync_links(self, request):
        """Sync a direct/extracted video with a selected-language audio track."""
        if not check_password(request):
            return web.json_response({"detail": "Unauthorized: Invalid API Password"}, status=401)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"detail": "invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"detail": "JSON body must be an object"}, status=400)
        try:
            result = await self._build_dual_result(request, body)
            result.pop("m3u8", None)
            return web.json_response(result, status=200)
        except DualLinksError as exc:
            return web.json_response({"status": "error", "detail": exc.message}, status=exc.status)
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "detail": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("DUAL link sync failed")
            return web.json_response(
                {"status": "error", "detail": f"dual link sync failed: {type(exc).__name__}"},
                status=502,
            )

    async def handle_dual_server_m3u8(self, request):
        """Build a combined HLS master from a Base64 JSON descriptor."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")
        if request.method == "HEAD":
            return web.Response(
                content_type="application/vnd.apple.mpegurl",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-store",
                },
            )
        try:
            body = self._decode_dual_descriptor(request.query.get("d", ""))
            bypass_query = str(
                request.query.get("bypass_audio_language") or ""
            ).strip().lower()
            if bypass_query in {"1", "true", "yes", "on"}:
                body["bypass_audio_language"] = True
            result = await self._build_dual_result(request, body)
            return web.Response(
                text=result["m3u8"],
                content_type="application/vnd.apple.mpegurl",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-store",
                },
            )
        except DualLinksError as exc:
            return web.json_response(
                {
                    "status": "error",
                    "code": _dual_error_code(exc),
                    "detail": exc.message,
                },
                status=exc.status,
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
            )
        except (ValueError, TypeError) as exc:
            return web.json_response(
                {"status": "error", "code": "invalid_request", "detail": str(exc)},
                status=400,
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
            )
        except Exception as exc:
            logger.exception("DUAL server master build failed")
            return web.json_response(
                {
                    "status": "error",
                    "code": "internal_error",
                    "detail": f"dual server build failed: {type(exc).__name__}",
                },
                status=502,
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
            )


__all__ = ["HLSProxyDualMixin"]
