import logging
import socket
import re
import asyncio
import base64
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin
from typing import Dict, Any
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from config import (
    get_connector_for_proxy,
    get_preferred_proxy_for_url,
    get_ordered_proxies_for_url,
)
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

class _WatchPlayerParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.player_src = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "iframe" and attrs.get("id") == "playerFrame":
            self.player_src = attrs.get("src")

def _decode_barecrop_econfig(enc: str) -> str | None:
    """Decode window._econfig payload used by barecrop.net embeds (Player 2).

    Port of the _0x1b7ade() function in /assets/stream.js:
      s = atob(enc) -> split in 4 chunks -> reorder [2,0,3,1] ->
      strip char at index 3 of each chunk -> atob each -> join ->
      atob -> JSON {stream_url, stream_url_nop2p, ...}
    Returns the m3u8 URL or None.
    """
    try:
        if not enc:
            return None
        s = base64.b64decode(enc).decode("latin1")
        n = 4
        order = [2, 0, 3, 1]
        chunk_len = math.ceil(len(s) / n)
        parts = [s[i * chunk_len:(i + 1) * chunk_len] for i in range(n)]
        arr = [""] * n
        for i, o in enumerate(order):
            a = str(parts[i])
            if len(a) < 4:
                return None
            a = a[:3] + a[4:]
            arr[o] = base64.b64decode(a).decode("latin1")
        final = base64.b64decode("".join(arr)).decode("utf-8", errors="ignore")
        cfg = json.loads(final)
        if isinstance(cfg, dict):
            url = cfg.get("stream_url") or cfg.get("stream_url_nop2p")
            if isinstance(url, str) and url.startswith("http"):
                return url.strip()
        return None
    except Exception:
        return None

class DLStreamsExtractor(BaseExtractor):
    """Extractor for daddy live / dlstreams streams."""

    # Return before the request layer kills the task at 30s: an unresponsive
    # dlive.sx would otherwise burn 10s per candidate, hit the outer timeout
    # and turn concurrent requests into 500s.
    EXTRACT_BUDGET_SECONDS = 20.0
    CANDIDATE_TIMEOUT_SECONDS = 6
    # Players poll the extractor every few seconds. Re-scraping the player
    # pages on every poll gets the iframe host to answer 429, so reuse a
    # validated stream URL for a short window instead. When a re-scrape fails,
    # serve the last known URL only if it still probes alive (never hang the
    # player 40-50s on a dead playlist) with a small backoff so the failing
    # host is not hammered. Stale window tracks the signed-token lifetime (~3-4 min).
    STREAM_CACHE_SECONDS = 30.0
    STREAM_CACHE_STALE_SECONDS = 300.0
    STREAM_CACHE_FAIL_BACKOFF_SECONDS = 15.0
    # Source-switch hysteresis: an isolated single-cycle validation failure
    # (e.g. an intermittent TikTok 403) must not flip playback to another
    # source mid-stream. A move to a lower-preference player is deferred for
    # this many consecutive rounds while the last-served URL still probes
    # alive; moves up (or to the same source) always win immediately.
    SOURCE_DAMP_STRIKES = 1
    # Transient-outage ride-through: when a fresh re-scrape fails AND the
    # cached URL probes dead, still serve the cached URL for this long after
    # the last successful validation instead of handing the player a dead
    # fallback URL (whose upstream 404 would kill the session for good).
    # Segment signatures (TikTok x-expires, assetrage e=) outlive the master
    # playlist, so the player keeps consuming buffer and recovers alone.
    # Bounded by consecutive-serve cap so a truly dead source is given up.
    STALE_DEAD_GRACE_SECONDS = 90.0
    STALE_DEAD_MAX_CONSECUTIVE = 6
    MAX_HOST_BACKOFF_SECONDS = 3600.0

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="dlstreams")
        self.request_headers = request_headers or {}
        self.entry_origin = ""
        self.stream_origin = ""
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
        self.session = None
        self._route_sessions = {}
        # Lazy direct session for segment probes on warp-excluded hosts
        # (see _probe_session_for): playback fetches those direct, so the
        # liveness check must test that path, not the WARP extractor session.
        self._direct_probe_session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = proxies or []
        self.bypass_warp_active = bypass_warp
        self._inflight_extract_tasks: dict = {}
        self._stream_cache: dict = {}
        # Source-switch hysteresis state (per channel_key): last served player
        # page and consecutive deferred down-moves. See _damp_downmove.
        self._last_served_candidate: dict = {}
        self._downmove_strikes: dict = {}
        # Transient-outage ride-through state (per channel_key): monotonic
        # time of last successful validation + consecutive dead-stale serves.
        self._last_validated_at: dict = {}
        self._dead_stale_serves: dict = {}
        # host -> monotonic deadline: honour upstream Retry-After instead of
        # scraping a host that already said "too many requests".
        self._host_backoff: dict[str, float] = {}

    @staticmethod
    def _host_of(url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    @classmethod
    def _parse_retry_after(cls, value: str | None) -> float | None:
        """Retry-After as delta-seconds or HTTP-date, clamped to a sane range."""
        raw = str(value or "").strip()
        if not raw:
            return None
        seconds = None
        if raw.isdigit():
            seconds = float(raw)
        else:
            try:
                target = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            if target is None:
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        if seconds is None:
            return None
        return max(1.0, min(float(seconds), cls.MAX_HOST_BACKOFF_SECONDS))

    def _host_limited_for(self, url: str) -> float:
        """Seconds left before this host may be queried again (0 = free)."""
        deadline = self._host_backoff.get(self._host_of(url), 0.0)
        return max(0.0, deadline - time.monotonic())

    def _mark_host_limited(self, url: str, retry_after: str | None) -> float:
        host = self._host_of(url)
        seconds = self._parse_retry_after(retry_after)
        if seconds is None:
            seconds = self.STREAM_CACHE_FAIL_BACKOFF_SECONDS
        self._host_backoff[host] = time.monotonic() + seconds
        logger.warning("DLStreams: %s rate-limited, backing off %.0fs", host, seconds)
        return seconds

    def _rate_limit_wait(self) -> float:
        """Longest remaining host backoff, dropping expired entries."""
        now = time.monotonic()
        self._host_backoff = {
            host: deadline
            for host, deadline in self._host_backoff.items()
            if deadline > now
        }
        if not self._host_backoff:
            return 0.0
        return max(self._host_backoff.values()) - now

    def _prioritize_player_urls(self, channel_id: str, requested_url: str = "", watch_html: str = "") -> list[str]:
        players = self._build_player_urls(channel_id, requested_url=requested_url)
        # Honour an explicit player, otherwise use the site's active iframe.
        requested_player = urlparse(requested_url)._replace(query="", fragment="").geturl()
        if requested_player in players:
            preferred = requested_player
        else:
            parser = _WatchPlayerParser()
            parser.feed(watch_html)
            preferred = urljoin(requested_url, parser.player_src or "")
        # Only reorder known players for this channel on the entry origin.
        if preferred in players:
            players.remove(preferred)
            players.insert(0, preferred)
        return players

    @staticmethod
    def _origin_of(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _route_diagnostic(self, url: str) -> str:
        """Describe routing state without exposing proxy credentials."""
        try:
            ordered = get_ordered_proxies_for_url(
                url,
                "dlstreams",
                self.proxies,
                self.bypass_warp_active,
            )
            warp_url = getattr(_cfg, "WARP_PROXY_URL", "")
            route_types = ["WARP" if proxy == warp_url else "proxy" for proxy in ordered]
            route_summary = ",".join(route_types) or "none"
        except Exception as exc:
            route_summary = f"unavailable({type(exc).__name__})"

        warp_enabled = bool(_cfg._get_dynamic_warp_enabled())
        warp_excluded = bool(_cfg._is_warp_excluded(url or ""))
        direct_allowed = _cfg.is_direct_connection_allowed(self.bypass_warp_active)
        host = urlparse(url or "").netloc or "unknown"
        return (
            f"target={host} warp={'on' if warp_enabled else 'off'} "
            f"warp_excluded={'yes' if warp_excluded else 'no'} "
            f"candidates={route_summary} direct={'allowed' if direct_allowed else 'disabled'}"
        )

    def _sync_entry_origin_from_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin != self.entry_origin:
                logger.debug("DLStreams entry origin changed from %s to %s", self.entry_origin, origin)
                self.entry_origin = origin
            if not self.stream_origin:
                self.stream_origin = origin

    def _get_cookie_header_for_url(self, url: str) -> str | None:
        if not self.session or self.session.closed or not self.session.cookie_jar:
            return None

        parsed = urlparse(url)
        cookies = self.session.cookie_jar.filter_cookies(
            f"{parsed.scheme}://{parsed.netloc}/"
        )
        cookie_header = "; ".join(f"{key}={morsel.value}" for key, morsel in cookies.items())
        return cookie_header or None

    @staticmethod
    def _extract_channel_id(url: str) -> str:
        match_id = re.search(r"(?:id=|premium|stream-)(\d+)", url)
        channel_id = match_id.group(1) if match_id else str(url)
        if not channel_id.isdigit():
            channel_id = channel_id.replace("premium", "")
        return channel_id

    def _build_player_urls(self, channel_id: str, requested_url: str = "") -> list[str]:
        origin = self.entry_origin.rstrip("/")
        # Personal branch: Player 2 (/cast/) is always last resort, for every
        # channel and with no per-channel pins. That backend can serve a
        # technically valid stream (HTTP 200, valid MPEG-TS, advancing timestamps)
        # whose video track is unplayable, so every cheap liveness probe promotes
        # it while playback shows green/black with working audio -- and placed
        # second it shadows the truly working players behind it. The order
        # otherwise follows the site's player list. If Player 2 is ever the only
        # working backend, the loop still reaches it, so playback starts anyway.
        # NOTE: this also means Player 1 (image-wrapped, heavier and 403-prone)
        # is tried first everywhere: upstream will never take this as-is.
        # This is the BASE order: _prioritize_player_urls (upstream) moves the
        # explicitly requested or site-active player to the front; everything
        # else keeps this order, so Player 2 stays last unless the site itself
        # serves it as active (or the user requests it outright).
        order = ["stream", "watch", "plus", "casting", "player", "hub", "cast"]
        return [f"{origin}/{folder}/stream-{channel_id}.php" for folder in order]

    @staticmethod
    def _inherit_query_if_missing(absolute_url: str, base_url: str) -> str:
        """CDNs here issue a token in the playlist URL and list relative URIs.

        Plain urljoin drops the parent token, so the segment/variant probe
        becomes a 404 and a working player is reported as dead.
        """
        parsed = urlparse(absolute_url)
        if parsed.query:
            return absolute_url
        base_query = urlparse(base_url).query
        if not base_query:
            return absolute_url
        return parsed._replace(query=base_query).geturl()

    @classmethod
    def _first_media_uri(cls, body: str, base_url: str, want_playlist: bool) -> str | None:
        """First non-comment URI in a playlist: variant (.m3u8) or segment."""
        uris = cls._first_media_uris(body, base_url, want_playlist, limit=1)
        return uris[0] if uris else None

    @classmethod
    def _first_media_uris(cls, body: str, base_url: str, want_playlist: bool, limit: int = 2) -> list:
        """First ``limit`` non-comment URIs in a playlist: variants or segments."""
        out = []
        for raw in (body or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower().split("?")[0].split("#")[0]
            if (low.endswith(".m3u8")) == want_playlist:
                out.append(cls._inherit_query_if_missing(urljoin(base_url, line), base_url))
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _segment_looks_like_image(segment_url: str, content_type: str, head: bytes) -> bool:
        """True when a segment is (or may be) an image container instead of raw TS."""
        if head[:1] == b"\x47":
            return False  # already clean MPEG-TS
        if head.startswith(b"\x89PNG\r\n\x1a\n") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            return True
        low = (segment_url or "").lower()
        return (
            (content_type or "").startswith("image/")
            or ".image" in low
            or "tiktokcdn" in low
            or "~tplv-" in low
        )

    @staticmethod
    def _ts_video_payload(data: bytes) -> bool | None:
        """Inspect aligned TS packets for video PES, not just a PMT declaration.

        False means actual audio PES were found without video. None leaves
        unsupported payloads inconclusive (e.g. private/extended stream IDs).
        A clear video PES starts with 00 00 01 E0..EF after the TS adaptation
        field. No codec probing or transcoding is required.
        """
        audio = False
        unknown = False
        for offset in range(0, len(data) - 187, 188):
            packet = data[offset:offset + 188]
            if packet[0] != 0x47 or packet[1] & 0x80 or packet[3] & 0xC0:
                return None
            if not packet[1] & 0x40 or not packet[3] & 0x10:
                continue
            start = 5 + packet[4] if packet[3] & 0x20 else 4
            payload = packet[start:]
            if len(payload) < 4:
                # A PES header may continue in the next TS packet. This
                # lightweight inspector cannot exclude video in that case.
                unknown = True
                continue
            if payload[:3] != b"\x00\x00\x01":
                continue
            stream_id = payload[3]
            if 0xE0 <= stream_id <= 0xEF:
                return True
            if 0xC0 <= stream_id <= 0xDF:
                audio = True
            else:
                unknown = True
        return False if audio and not unknown else None

    async def _get_direct_probe_session(self):
        """Plain direct session for probing warp-excluded segment hosts.

        Playback serves those segments direct (warp_exclude_domains); probing
        them through WARP produced intermittent 403s that flipped the chosen
        source on every re-extraction, stalling playback mid-stream.
        """
        session = self._direct_probe_session
        if session is not None and not session.closed:
            return session
        session = ClientSession(
            timeout=ClientTimeout(total=30, connect=10),
            connector=TCPConnector(limit=0, limit_per_host=0),
            headers=self.base_headers,
        )
        self._direct_probe_session = session
        return session

    async def _probe_session_for(self, url: str, session):
        """Session matching the playback route for a segment URL.

        Warp-excluded hosts (e.g. tiktokcdn) are fetched direct at playback:
        proxy_core treats "warp-excluded with no proxy left in the chain" as
        direct-only even when direct fallback is otherwise disabled, and the
        serving request here carries proxy=off anyway. Probing those hosts
        through WARP produced intermittent 403s that flipped the chosen
        source on every re-extraction, stalling playback mid-stream.
        Anything else keeps the extractor session (manifest path is WARP).
        """
        try:
            if _cfg._is_warp_excluded(url):
                logger.debug("DLStreams: probing %s direct (warp-excluded)", self._host_of(url))
                return await self._get_direct_probe_session()
        except Exception as e:
            logger.debug("DLStreams: probe routing check failed, keeping extractor session: %s", e)
        return session

    async def _segment_carries_video(
        self, session, segment_url: str, headers: dict, timeout: float,
        deep: bool = True, encrypted: bool = False,
    ) -> bool:
        """Bounded probe; stop downloading native TS as soon as video is found."""
        max_bytes = 16 * 1024 * 1024
        # Probe the segment on its playback route: warp-excluded CDN hosts
        # are served direct, and WARP probes 403 intermittently there. This
        # wraps the upstream video-PES inspection without changing it: the
        # route matches playback, the verdict logic stays upstream's.
        session = await self._probe_session_for(segment_url, session)
        async with session.get(
            segment_url, headers=headers, timeout=ClientTimeout(total=timeout)
        ) as resp:
            if resp.status not in (200, 206):
                return False
            head = await resp.content.read(4096)
            if not head:
                return False
            if not deep or encrypted:
                return True
            is_image = self._segment_looks_like_image(
                segment_url, (resp.headers.get("Content-Type") or "").lower(), head
            )
            if is_image:
                from services.proxy_core import HLSProxyCoreMixin
                body = bytearray(head)
                async for chunk in resp.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return False
                unwrapped = await asyncio.to_thread(
                    HLSProxyCoreMixin._strip_fake_png_header_from_ts, bytes(body)
                )
                return (
                    len(unwrapped) > 188 and unwrapped[0] == 0x47
                    and unwrapped[188] == 0x47
                    and self._ts_video_payload(unwrapped) is not False
                )
            if head[0] != 0x47:
                # fMP4 and other containers retain the existing HTTP probe.
                return True
            pending = b""
            body = bytearray()
            # Native TS can start with a long audio run. A missing video PES
            # is only conclusive once the complete segment has been examined.
            async def chunks():
                yield head
                async for chunk in resp.content.iter_chunked(65536):
                    yield chunk

            async for chunk in chunks():
                body.extend(chunk)
                pending += chunk
                if len(body) > max_bytes:
                    return False
                complete = len(pending) // 188 * 188
                evidence = self._ts_video_payload(pending[:complete])
                if evidence is True:
                    return True
                pending = pending[complete:]
            if resp.status == 200 and not pending and self._ts_video_payload(bytes(body)) is False:
                logger.info("DLStreams: segment contains audio PES but no video payload")
                return False
            return True

    async def _is_stream_alive(self, session, stream_url: str, headers: dict, budget: float | None = None, deep: bool = True) -> bool:
        """Validate the full chain: master -> variant -> first segment (+ AES key).

        Some CDNs serve the manifest (200) while segments 403 (IP-bound
        tokens, datacenter blocks). A manifest-only check would accept a
        dead player and hide the working ones, so follow the chain with
        bounded segment probes.

        With ``deep=True`` an image-wrapped segment (PNG/WebP, e.g. Player 1) is
        downloaded once and unwrapped exactly like the proxy does when serving it:
        if no MPEG-TS can be recovered (static banner/placeholder image, which the
        proxy would answer with HTTP 204) the player is reported dead so the caller
        moves on to the next player. Native TS must carry video PES, rather
        than only announcing a video PID in its program map.
        ``deep=False`` keeps the HTTP-only probe for callers that request it.
        """
        deadline = time.monotonic() + (budget if budget is not None else self.EXTRACT_BUDGET_SECONDS)

        def _timeout(default: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            return min(default, remaining)

        try:
            async with session.get(stream_url, headers=headers, timeout=_timeout(10)) as resp:
                if resp.status != 200:
                    logger.debug("DLStreams: validation of %s -> HTTP %s", stream_url, resp.status)
                    return False
                master = await resp.text()
            if "#EXTM3U" not in master[:8000] and "#EXT-X-" not in master[:8000]:
                logger.debug("DLStreams: validation of %s -> not a playlist", stream_url)
                return False
            current_url, current_body = stream_url, master
            # Master -> variant (media playlists contain #EXTINF, masters don't)
            if "#EXTINF" not in master:
                variant = self._first_media_uri(master, stream_url, want_playlist=True)
                if not variant:
                    logger.debug("DLStreams: validation of %s -> no variant found", stream_url)
                    return False
                async with session.get(variant, headers=headers, timeout=_timeout(8)) as resp:
                    if resp.status != 200:
                        logger.debug("DLStreams: validation variant %s -> HTTP %s", variant, resp.status)
                        return False
                    current_body = await resp.text()
                current_url = variant
                if "#EXTM3U" not in current_body[:8000]:
                    return False
            # Variant -> segments. A single URL can 403 while its siblings
            # serve fine (per-URL signed tokens), so try the second listed
            # segment before condemning the player. Each attempt keeps the
            # upstream bounded-probe semantics: no Range on a deep TS probe
            # (an audio-only prefix cannot establish whether video exists
            # later), Range only for the shallow/encrypted HTTP probe.
            segments = self._first_media_uris(current_body, current_url, want_playlist=False, limit=2)
            if not segments:
                logger.debug("DLStreams: validation of %s -> no segments listed", stream_url)
                return False
            encrypted = bool(re.search(r'#EXT-X-KEY:[^\n]*METHOD=(?!NONE\b)', current_body))
            alive = False
            for cand_seg in segments:
                probe_headers = {k: v for k, v in headers.items() if k.lower() != "range"}
                if not deep or encrypted:
                    probe_headers["Range"] = "bytes=0-4095"
                try:
                    if await self._segment_carries_video(
                        session, cand_seg, probe_headers, _timeout(12), deep=deep, encrypted=encrypted
                    ):
                        alive = True
                        break
                except Exception as e:
                    logger.debug("DLStreams: segment probe of %s failed: %s", cand_seg, e)
                    continue
                logger.debug("DLStreams: segment probe of %s -> no video payload", cand_seg)
            if not alive:
                return False
            # AES key, if the variant uses one
            key_match = re.search(r'#EXT-X-KEY:[^\n]*URI="([^"]+)"', current_body)
            if key_match:
                key_url = self._inherit_query_if_missing(
                    urljoin(current_url, key_match.group(1)), current_url
                )
                try:
                    async with session.get(key_url, headers=headers, timeout=_timeout(8)) as resp:
                        if resp.status != 200:
                            logger.debug("DLStreams: key probe of %s -> HTTP %s", key_url, resp.status)
                            return False
                except Exception as e:
                    logger.debug("DLStreams: key probe of %s failed: %s", key_url, e)
                    return False
            self._last_validated_manifest = current_body or master
            return True
        except Exception as e:
            logger.debug("DLStreams: validation of %s failed: %s", stream_url, e)
            return False

    def _build_result(self, stream_url: str, playback_headers: dict) -> Dict[str, Any]:
        """Build the extractor response payload for an accepted stream URL."""
        parsed_stream = urlparse(stream_url)
        # Sync session cookies for playback/proxying
        self.stream_origin = f"{parsed_stream.scheme}://{parsed_stream.netloc}"
        # Store cookies in session if needed
        cookie_header = self._get_cookie_header_for_url(stream_url)
        if cookie_header:
            playback_headers = {**playback_headers, "Cookie": cookie_header}
        manifest_text = getattr(self, "_last_validated_manifest", None) or ""
        return {
            "destination_url": stream_url,
            "request_headers": playback_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "captured_manifest": None,
            "captured_manifests": {stream_url: manifest_text},
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

    def _damp_downmove(self, channel_key, order_index: dict, candidate: str):
        """Defer a move to a lower-preference player, or return None.

        Returns the candidate when the switch is deferred (isolated failure of
        the last-served source); returns None when the result should be served
        immediately (same source, move up in preference, already-failed-over,
        or no history). Recording of the served candidate happens here for
        immediate serves; deferred serves are recorded by extract() only if
        the switch is actually accepted.
        """
        if channel_key is None:
            return None
        last = self._last_served_candidate.get(channel_key)
        new_idx = order_index.get(candidate, len(order_index))
        last_idx = order_index.get(last, -1) if last else -1
        if last is None or new_idx <= last_idx:
            self._last_served_candidate[channel_key] = candidate
            self._downmove_strikes.pop(channel_key, None)
            return None
        if self._downmove_strikes.get(channel_key, 0) < self.SOURCE_DAMP_STRIKES:
            logger.warning(
                "DLStreams: holding last-served source %s, deferring %s (isolated validation failure)",
                last,
                candidate,
            )
            return candidate
        self._last_served_candidate[channel_key] = candidate
        self._downmove_strikes.pop(channel_key, None)
        return None

    async def _extract_directly(self, url: str, channel_id: str, channel_key=None) -> Dict[str, Any] | None:
        """Fast path direct HTTP M3U8 extraction."""
        session = await self._get_session(url)
        seen_iframes: set[str] = set()
        
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.EXTRACT_BUDGET_SECONDS

        def _remaining() -> float:
            return deadline - loop.time()

        def _timeout(default: float) -> float:
            return max(1.0, min(default, _remaining()))

        async def _fetch_html(target: str, hdrs: dict, what: str) -> str | None:
            # Hard event-loop-level cap for small HTML page fetches. The
            # per-request aiohttp timeout did not stop a pathological ~60s
            # stall (connector-level hang outside timeout coverage) that
            # starved manifest refresh while polls piled up waiting for the
            # in-flight extraction. Raises asyncio.TimeoutError on breach;
            # returns None on non-200. Deep stream validation is NOT capped
            # here (it legitimately downloads multi-MB segments).
            async def _do() -> str | None:
                async with session.get(target, headers=hdrs, timeout=_timeout(self.CANDIDATE_TIMEOUT_SECONDS)) as resp:
                    if resp.status != 200:
                        logger.debug("DLStreams: %s %s returned status %s", what, target, resp.status)
                        if resp.status == 429:
                            self._mark_host_limited(target, resp.headers.get("Retry-After"))
                        return None
                    return await resp.text()
            try:
                return await asyncio.wait_for(_do(), timeout=self.CANDIDATE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning(
                    "DLStreams: %s %s exceeded %ss, skipping",
                    what, target, self.CANDIDATE_TIMEOUT_SECONDS,
                )
                raise

        watch_html = ""
        if urlparse(url).path == "/watch.php" and not self._host_limited_for(url):
            try:
                # Same hard cap as candidate/iframe pages: the upstream 3s
                # aiohttp timeout alone does not cover connector-level hangs.
                watch_html = await _fetch_html(url, dict(self.base_headers), "watch page") or ""
            except Exception as exc:
                logger.debug("DLStreams: default player unavailable: %s", exc)
        player_urls = self._prioritize_player_urls(
            channel_id, requested_url=url, watch_html=watch_html
        )
        order_index = {u: i for i, u in enumerate(player_urls)}

        for candidate_index, candidate in enumerate(player_urls):
            if _remaining() <= 2.0:
                logger.warning(
                    "DLStreams: extraction budget of %.0fs exhausted after %d/%d players",
                    self.EXTRACT_BUDGET_SECONDS,
                    candidate_index,
                    len(player_urls),
                )
                break
            limited_for = self._host_limited_for(candidate)
            if limited_for > 0:
                logger.debug(
                    "DLStreams: %s rate-limited for another %.0fs, skipping",
                    self._host_of(candidate),
                    limited_for,
                )
                continue
            if channel_id == "861" and "/cast/stream-" in candidate.lower():
                # 861 quarantine: the Player 2 backend serves a frozen video
                # track (proven md5-identical frames). This still applies on
                # top of the upstream site-following selection: frozen video
                # carries video PES, so the upstream video-PES inspection
                # promotes it as alive, and parking 861 on it wedges the
                # player beyond recovery -- worse than a stall that heals
                # when Player 1 returns.
                logger.debug("DLStreams: skipping Player 2 for 861 (known-frozen backend)")
                continue
            try:
                headers = {
                    "User-Agent": self.base_headers["User-Agent"],
                    "Referer": url,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                }
                
                logger.debug("DLStreams: GET stream page %s", candidate)
                html = await _fetch_html(candidate, headers, "stream page")
                if html is None:
                    continue
                
                # Extract player iframe src (support absolute, protocol-relative and relative URLs)
                iframe_src = None
                if BeautifulSoup:
                    try:
                        soup = BeautifulSoup(html, "html.parser")
                        iframe_el = soup.find("iframe", id="thatframe") or soup.find("iframe")
                        if iframe_el:
                            iframe_src = iframe_el.get("src")
                    except Exception as e:
                        logger.debug("DLStreams: bs4 parsing hiccup: %s", e)
                
                if not iframe_src:
                    match = re.search(r'<iframe\s+[^>]*src=["\']([^"\']+)["\']', html, re.I)
                    if match:
                        iframe_src = match.group(1).strip()
                
                if not iframe_src:
                    logger.debug("DLStreams: player iframe not found in HTML of %s", candidate)
                    continue

                # Resolve relative / protocol-relative iframe URLs against the candidate page
                if iframe_src.startswith("//"):
                    iframe_src = urlparse(candidate).scheme + ":" + iframe_src
                elif iframe_src.startswith("/"):
                    iframe_src = urljoin(candidate, iframe_src)
                elif not re.match(r"^https?://", iframe_src, re.I):
                    iframe_src = urljoin(candidate + "/", iframe_src)
                
                logger.debug("DLStreams: found player iframe: %s", iframe_src)
                if iframe_src in seen_iframes:
                    logger.debug("DLStreams: iframe %s already attempted, skipping", iframe_src)
                    continue
                seen_iframes.add(iframe_src)
                limited_for = self._host_limited_for(iframe_src)
                if limited_for > 0:
                    logger.debug(
                        "DLStreams: %s rate-limited for another %.0fs, skipping iframe",
                        self._host_of(iframe_src),
                        limited_for,
                    )
                    continue
                
                # Fetch iframe player page
                iframe_headers = headers.copy()
                iframe_headers["Referer"] = candidate
                iframe_headers["Origin"] = self.entry_origin
                
                iframe_html = await _fetch_html(iframe_src, iframe_headers, "iframe page")
                if iframe_html is None:
                    continue
                
                # Extract stream URL from iframe HTML.
                # Supported formats:
                #  1) barecrop.net Player 2: window._econfig='...' (Clappr, scrambled base64)
                #  2) classic: source:window.atob('...') / atob('...')
                #  3) direct: source: 'https://...m3u8'
                #  4) fallback: any https://...m3u8 URL in page
                stream_url = None

                m_cfg = re.search(r"window\._econfig\s*=\s*['\"]([^'\"]{100,})['\"]", iframe_html)
                if m_cfg:
                    stream_url = _decode_barecrop_econfig(m_cfg.group(1))
                    if stream_url:
                        logger.debug("DLStreams: decoded _econfig stream URL: %s", stream_url)

                if not stream_url:
                    atob_match = re.search(r"(?:window\.)?atob\(['\"]([A-Za-z0-9+/=]{20,})['\"]\)", iframe_html)
                    if atob_match:
                        try:
                            cand = base64.b64decode(atob_match.group(1)).decode('utf-8', errors='ignore').strip()
                            if cand.startswith("http"):
                                stream_url = cand
                                logger.debug("DLStreams: decrypted stream URL: %s", stream_url)
                            else:
                                logger.debug("DLStreams: atob payload is not a URL: %s", cand[:80])
                        except Exception as e:
                            logger.debug("DLStreams: atob decode failed: %s", e)

                if not stream_url:
                    direct_match = re.search(r"source\s*:\s*['\"](https?://[^'\"]+?)['\"]", iframe_html)
                    if direct_match:
                        stream_url = direct_match.group(1).strip()
                        logger.debug("DLStreams: extracted direct stream URL: %s", stream_url)

                if not stream_url:
                    m3u8_match = re.search(r"https?://[^\s'\"<>\\]+\.m3u8[^\s'\"<>\\]*", iframe_html)
                    if m3u8_match:
                        stream_url = m3u8_match.group(0)
                        logger.debug("DLStreams: extracted m3u8 fallback URL: %s", stream_url)

                if not stream_url:
                    logger.debug("DLStreams: no stream URL found. Iframe HTML start: %s", iframe_html[:200])
                    continue

                if not stream_url.startswith("http"):
                    logger.debug("DLStreams: invalid stream URL: %s", stream_url[:120])
                    continue
                
                # Format response payload
                parsed_iframe = urlparse(iframe_src)
                iframe_origin = f"{parsed_iframe.scheme}://{parsed_iframe.netloc}"
                
                # Use iframe origin as the Referer/Origin for playback headers to pass CDN security checks
                ref_origin = iframe_origin
                
                playback_headers = {
                    "Referer": f"{ref_origin}/",
                    "Origin": ref_origin,
                    "User-Agent": self.base_headers["User-Agent"],
                    "Accept": "*/*",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "cross-site",
                }
                
                # Return the first stream that is actually playable from here.
                # A dead Player 1 URL must not hide a working Player 2.
                if await self._is_stream_alive(
                    session, stream_url, playback_headers, budget=_remaining()
                ):
                    logger.info("DLStreams: working stream found via %s", candidate)
                    result = self._build_result(stream_url, playback_headers)
                    result["_validated"] = True
                    damped = self._damp_downmove(channel_key, order_index, candidate)
                    if damped:
                        result["_damped"] = damped
                    return result
                logger.info("DLStreams: stream from %s is dead, trying next player", candidate)
                continue
                
            except Exception as e:
                logger.debug("DLStreams: direct extraction candidate %s failed: %s", candidate, e)
                if candidate_index == 0 and isinstance(e, asyncio.TimeoutError):
                    # Every candidate shares the dlive.sx origin: when the first
                    # page does not answer, the others will not either.
                    logger.warning("DLStreams: site page timed out, aborting candidate loop")
                    break
                continue

        return None


    async def _get_session(self, url: str | None = None):
        # Determine the correct proxy for the current state
        target_url = url or self.stream_origin or self.entry_origin
        forced = None if self._force_direct else _cfg.effective_forced_proxy(target_url, self._forced_proxy)
        if forced:
            proxy_url = forced
        else:
            proxy_url = await get_preferred_proxy_for_url(target_url, "dlstreams", self.proxies, self.bypass_warp_active)
        if proxy_url is None and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError(
                "DLStreams: no usable proxy route; direct fallback disabled "
                f"[{self._route_diagnostic(target_url)}]"
            )
        self.last_used_proxy = proxy_url
        
        session = self._route_sessions.get(proxy_url)
        if session is not None and not session.closed:
            self.session = session
            self._session_proxy = proxy_url
            return session

        # DLStreams keys and segments appear to be tied to a consistent
        # egress/session context. Using rotating/global proxies here can
        # produce a different AES key than the browser receives.
        if proxy_url:
            connector = get_connector_for_proxy(proxy_url)
            logger.debug("DLStreams: Using proxy session: %s", proxy_url)
        else:
            connector = TCPConnector(limit=0, limit_per_host=0)
            logger.debug("DLStreams: Using direct session (Real IP)")
        
        timeout = ClientTimeout(total=30, connect=10)
        self.session = ClientSession(
            timeout=timeout,
            connector=connector,
            headers=self.base_headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self._session_proxy = proxy_url # Store for future comparison
        self._route_sessions[proxy_url] = self.session
        return self.session

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Extracts the M3U8 URL and headers bypassing the public watch page."""
        self._sync_entry_origin_from_url(url)
        self._apply_routing_kwargs(url, kwargs)
        channel_id = self._extract_channel_id(url)
        requested_player = urlparse(url)._replace(query="", fragment="").geturl()
        req_mode = (
            requested_player if requested_player in self._build_player_urls(channel_id)
            else "default"
        )
        channel_key = (f"premium{channel_id}", req_mode, self._forced_proxy, self._force_direct, self.bypass_warp_active)

        cached = self._stream_cache.get(channel_key)
        if cached and cached[0] > time.monotonic() and not kwargs.get("force_refresh"):
            logger.debug("DLStreams: reusing cached stream URL for %s", channel_key)
            return dict(cached[2])

        try:
            existing_task = self._inflight_extract_tasks.get(channel_key)
            if existing_task and not existing_task.done():
                logger.debug("DLStreams: waiting for in-flight extraction of %s", channel_key)
                result = await existing_task
            else:
                task = asyncio.create_task(self._extract_impl(url, channel_id=channel_id, channel_key=channel_key, **kwargs))
                self._inflight_extract_tasks[channel_key] = task
                try:
                    result = await task
                finally:
                    current_task = self._inflight_extract_tasks.get(channel_key)
                    if current_task is task:
                        self._inflight_extract_tasks.pop(channel_key, None)
        except Exception:
            if cached and cached[1] > time.monotonic():
                if await self._is_cached_still_alive(cached[2]):
                    logger.warning(
                        "DLStreams: extraction failed for %s, serving last known stream URL",
                        channel_key,
                    )
                    self._dead_stale_serves[channel_key] = 0
                    self._backoff_cache_entry(channel_key, cached)
                    return dict(cached[2])
                ride_through = self._serve_dead_stale(channel_key, cached)
                if ride_through is not None:
                    return ride_through
                logger.warning(
                    "DLStreams: cached stream for %s is dead, not serving stale",
                    channel_key,
                )
            raise

        if result and result.pop("_validated", False):
            damped_candidate = result.pop("_damped", None)
            if damped_candidate and cached and cached[1] > time.monotonic():
                if await self._is_cached_still_alive(cached[2]):
                    logger.warning(
                        "DLStreams: deferring source switch for %s, serving last known stream URL",
                        channel_key,
                    )
                    self._downmove_strikes[channel_key] = self._downmove_strikes.get(channel_key, 0) + 1
                    self._dead_stale_serves[channel_key] = 0
                    self._backoff_cache_entry(channel_key, cached)
                    return dict(cached[2])
                logger.warning(
                    "DLStreams: last known stream for %s is dead, accepting switch to %s",
                    channel_key,
                    damped_candidate,
                )
            if damped_candidate:
                self._last_served_candidate[channel_key] = damped_candidate
                self._downmove_strikes.pop(channel_key, None)
            now = time.monotonic()
            self._stream_cache[channel_key] = (
                now + self.STREAM_CACHE_SECONDS,
                now + self.STREAM_CACHE_STALE_SECONDS,
                dict(result),
            )
            self._last_validated_at[channel_key] = now
            self._dead_stale_serves[channel_key] = 0
        elif result and cached and cached[1] > time.monotonic():
            if await self._is_cached_still_alive(cached[2]):
                logger.warning(
                    "DLStreams: no player validated for %s, serving last known stream URL",
                    channel_key,
                )
                self._dead_stale_serves[channel_key] = 0
                self._backoff_cache_entry(channel_key, cached)
                return dict(cached[2])
            ride_through = self._serve_dead_stale(channel_key, cached)
            if ride_through is not None:
                return ride_through
            logger.warning(
                "DLStreams: cached stream for %s is dead, not serving stale",
                channel_key,
            )
        elif cached and cached[1] > time.monotonic():
            # Upstream 26f9c9e removed the dead-fallback return: when no
            # player validates, result is None instead of a dead URL. Cover
            # that path with the same stale/ride-through treatment, so a
            # transient dual-backend outage still rides through on cache
            # instead of killing the session with nothing to serve.
            if await self._is_cached_still_alive(cached[2]):
                logger.warning(
                    "DLStreams: extraction found nothing for %s, serving last known stream URL",
                    channel_key,
                )
                self._dead_stale_serves[channel_key] = 0
                self._backoff_cache_entry(channel_key, cached)
                return dict(cached[2])
            ride_through = self._serve_dead_stale(channel_key, cached)
            if ride_through is not None:
                return ride_through
            logger.warning(
                "DLStreams: cached stream for %s is dead, not serving stale",
                channel_key,
            )
        elif result:
            result.pop("_validated", None)
            result.pop("_damped", None)
        return result

    def _backoff_cache_entry(self, channel_key, cached: tuple[float, float, dict]) -> None:
        """Delay the next scrape, honouring any Retry-After from the hosts."""
        wait = max(self.STREAM_CACHE_FAIL_BACKOFF_SECONDS, self._rate_limit_wait())
        self._stream_cache[channel_key] = (
            time.monotonic() + wait,
            cached[1],
            cached[2],
        )

    def _serve_dead_stale(self, channel_key, cached: tuple[float, float, dict]):
        """Serve the cached URL even though its probe just failed.

        Returns the cached result dict while inside the transient-outage
        grace window, else None (caller keeps the previous behaviour).
        Applies backoff so the failing origin is not hammered every poll.
        """
        now = time.monotonic()
        last_ok = self._last_validated_at.get(channel_key, 0.0)
        serves = self._dead_stale_serves.get(channel_key, 0)
        if now - last_ok > self.STALE_DEAD_GRACE_SECONDS:
            return None
        if serves >= self.STALE_DEAD_MAX_CONSECUTIVE:
            return None
        self._dead_stale_serves[channel_key] = serves + 1
        logger.warning(
            "DLStreams: upstream outage, serving stale for %s despite failed probe "
            "(ride-through %d/%d)",
            channel_key,
            serves + 1,
            self.STALE_DEAD_MAX_CONSECUTIVE,
        )
        self._backoff_cache_entry(channel_key, cached)
        return dict(cached[2])

    async def _is_cached_still_alive(self, cached_result: dict) -> bool:
        """Cheap HTTP-only probe before serving a stale URL after a failed refresh.

        deep=False on purpose: the upstream default (full video-PES segment
        download, up to 16MB) is too heavy and too slow for a per-poll
        liveness check -- it would time out on big Player 1 segments and
        wrongly condemn healthy stale URLs. Frozen-backend detection stays
        with the 861 quarantine, not with this probe.
        """
        try:
            stream_url = (cached_result or {}).get("destination_url", "")
            if not stream_url:
                return False
            headers = (cached_result or {}).get("request_headers", {}) or {}
            session = await self._get_session(stream_url)
            return await self._is_stream_alive(session, stream_url, headers, budget=4.0, deep=False)
        except Exception:
            return False

    async def _extract_impl(self, url: str, channel_id: str, channel_key=None, **kwargs) -> Dict[str, Any]:
        try:
            session = await self._get_session(url)

            # Direct browser-less HTTP extraction (only active path)
            direct_error = None
            try:
                logger.info("DLStreams: Attempting direct browser-less HTTP extraction for %s", f"premium{channel_id}")
                direct_result = await self._extract_directly(url, channel_id, channel_key)
                if direct_result:
                    logger.info("DLStreams: Direct browser-less extraction succeeded for %s!", f"premium{channel_id}")
                    return direct_result
            except Exception as direct_exc:
                direct_error = direct_exc
                logger.debug(
                    "DLStreams: browser-less extraction failed for %s: %s",
                    f"premium{channel_id}",
                    direct_exc,
                    exc_info=True,
                )

            raise ExtractorError("Could not retrieve manifest via extraction.") from direct_error

        except asyncio.CancelledError:
            raise
        except ExtractorError:
            raise
        except Exception as e:
            logger.debug("DLStreams internal extraction hiccup for %s: %s", url, e, exc_info=True)
            raise ExtractorError(f"DLStreams extraction failed: {str(e)}") from None

    async def close(self):
        pending_tasks = list(self._inflight_extract_tasks.values())
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._inflight_extract_tasks.clear()
        sessions = set(self._route_sessions.values())
        if self.session is not None:
            sessions.add(self.session)
        if self._direct_probe_session is not None:
            sessions.add(self._direct_probe_session)
        for session in sessions:
            if not session.closed:
                await session.close()
        self._route_sessions.clear()
        self.session = None
        self._direct_probe_session = None
