"""Small aiohttp client helper shared by the in-process DUAL service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin

import aiohttp

from config import get_connector_for_proxy
from .security import resolves_publicly, valid_public_url


MAX_ASSET_REDIRECTS = 5
MAX_ASSET_BYTES = 20 * 1024 * 1024


class AssetFetchError(ValueError):
    """A DUAL upstream asset could not be fetched.

    Inherits ValueError so existing callers keep their fallback handling, while
    ``status`` lets the service answer with a meaningful HTTP status.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _proxy_settings(proxy: str):
    proxy = str(proxy or "").strip()
    if not proxy:
        return None, None

    if proxy.startswith(("socks5://", "socks5h://", "socks4://", "socks4a://")):
        # Use central connector policy: WARP closes upstream sockets per response.
        return get_connector_for_proxy(proxy), None
    return None, proxy


def create_client_session(proxy: str = "", timeout: float = 30):
    connector, request_proxy = _proxy_settings(proxy)
    kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout)}
    if connector is not None:
        kwargs["connector"] = connector
    return aiohttp.ClientSession(**kwargs), request_proxy


@asynccontextmanager
async def client_session(proxy: str = "", timeout: float = 30):
    session, request_proxy = create_client_session(proxy, timeout)
    async with session:
        yield session, request_proxy


async def download_asset(
    url: str,
    headers: dict | None,
    destination: Path,
    routing=None,
    *,
    max_redirects: int = MAX_ASSET_REDIRECTS,
    max_bytes: int = MAX_ASSET_BYTES,
) -> int:
    """Download one public HTTPS asset with redirect and size guards.

    Shared by the DUAL audio and subtitle stores so both enforce the same
    public-URL checks, redirect cap and size cap.
    """
    current_url = url
    for redirect_count in range(max_redirects + 1):
        if not valid_public_url(current_url) or not await resolves_publicly(current_url):
            raise ValueError("asset URL is not public HTTPS")
        proxy = routing.proxy_for(current_url) if routing is not None else None
        route = f"proxy={proxy}" if proxy else "direct"
        try:
            async with client_session(proxy, timeout=30) as (client, request_proxy):
                async with client.get(
                    current_url,
                    headers=headers or {},
                    proxy=request_proxy,
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            raise ValueError("asset redirect has no location")
                        next_url = urljoin(current_url, location)
                        if not valid_public_url(next_url) or not await resolves_publicly(next_url):
                            raise ValueError("asset redirect is not public HTTPS")
                        if redirect_count >= max_redirects:
                            raise ValueError("too many asset redirects")
                        current_url = next_url
                        continue

                    if response.status >= 400:
                        raise AssetFetchError(
                            f"upstream HTTP {response.status} ({route})",
                            502,
                        )
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            too_large = int(content_length) > max_bytes
                        except ValueError as exc:
                            raise ValueError("invalid asset content length") from exc
                        if too_large:
                            raise ValueError("asset is too large")

                    try:
                        total = 0
                        with destination.open("wb") as output:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise ValueError("asset is too large")
                                output.write(chunk)
                    except Exception:
                        destination.unlink(missing_ok=True)
                        raise
                    return total
        except AssetFetchError:
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            # Proxy failures (WARP/Tor SOCKS) land here: surface the route so a
            # dead tunnel is visible in the DUAL answer instead of a raw 500.
            raise AssetFetchError(
                f"{type(exc).__name__}: {exc} ({route})",
                504,
            ) from exc

    raise ValueError("too many asset redirects")


__all__ = [
    "AssetFetchError",
    "client_session",
    "create_client_session",
    "download_asset",
]
