"""Resolve DUAL outbound routing with EasyProxy's live configuration."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import time
from urllib.parse import unquote

import config_store
import config as config_module
from config import (
    BYPASS_PROXIES_CONTEXT,
    BYPASS_WARP_CONTEXT,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    get_proxy_for_url,
    resolve_proxy_alias,
)


_CONFIG_REFRESH_SECONDS = 1.0
_last_config_refresh = 0.0


def _refresh_live_config() -> None:
    """Refresh admin settings used by the in-process DUAL service."""
    global _last_config_refresh
    now = time.monotonic()
    if now - _last_config_refresh < _CONFIG_REFRESH_SECONDS:
        return
    config_store._load()
    config_module.reload_config()
    _last_config_refresh = now


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RoutingOptions:
    """Per-playback routing overrides; global admin config remains the default."""

    warp_off: bool = False
    proxy_off: bool = False
    forced_proxy: str | None = None
    extractor_name: str = ""

    def proxy_for(self, url: str) -> str | None:
        _refresh_live_config()
        bypass_warp = self.warp_off
        bypass_proxies = self.proxy_off
        if self.extractor_name:
            extractor_key = self.extractor_name.lower().replace("_direct", "").replace("_noproxy", "")
            warp_off_extractors = {
                str(value).lower() for value in config_store.get("warp_off_extractors", [])
            }
            proxy_off_extractors = {
                str(value).lower() for value in config_store.get("proxy_off_extractors", [])
            }
            if extractor_key in warp_off_extractors:
                bypass_warp = True
            if extractor_key in proxy_off_extractors:
                bypass_proxies = True

        # Explicit proxies command over WARP. proxy=off must disable even a
        # forced proxy so the resolver can select WARP instead.
        # ponytail: proxy_exclude_domains drops even explicit ?proxy= (WARP exempt)
        effective_forced = config_module.effective_forced_proxy(url, self.forced_proxy)
        if effective_forced and config_module.is_warp_proxy_url(effective_forced):
            if (
                bypass_warp
                or not config_module._get_dynamic_warp_enabled()
                or config_module._is_warp_excluded(url or "")
            ):
                effective_forced = None
        if effective_forced and not bypass_proxies:
            return effective_forced

        bypass_proxies = bypass_proxies or config_module._is_proxy_excluded(url or "")

        bypass_warp_token = BYPASS_WARP_CONTEXT.set(bypass_warp)
        bypass_proxy_token = BYPASS_PROXIES_CONTEXT.set(bypass_proxies)
        selected_token = SELECTED_PROXY_CONTEXT.set(None)
        strict_token = STRICT_PROXY_CONTEXT.set(False)
        try:
            return get_proxy_for_url(
                url,
                bypass_warp=bypass_warp,
                bypass_proxies=bypass_proxies,
                extractor_name=self.extractor_name,
            )
        finally:
            BYPASS_WARP_CONTEXT.reset(bypass_warp_token)
            BYPASS_PROXIES_CONTEXT.reset(bypass_proxy_token)
            SELECTED_PROXY_CONTEXT.reset(selected_token)
            STRICT_PROXY_CONTEXT.reset(strict_token)


def from_values(*sources: Mapping | None) -> RoutingOptions:
    """Read routing flags from request/query/payload dictionaries."""
    values: dict = {}
    for source in sources:
        if isinstance(source, Mapping):
            values.update(source)

    raw_proxy = values.get("proxy") or values.get("proxy_url") or ""
    raw_proxy = resolve_proxy_alias(unquote(str(raw_proxy).strip()))
    proxy_off = (
        str(raw_proxy).lower() == "off"
        or _as_bool(values.get("proxy_off"))
    )
    forced_proxy = None if proxy_off or not raw_proxy else raw_proxy
    extractor_name = str(
        values.get("extractor_name")
        or values.get("extractor")
        or values.get("extractor_key")
        or ""
    ).strip()
    warp_off = (
        str(values.get("warp") or "").strip().lower() == "off"
        or _as_bool(values.get("warp_off"))
    )
    return RoutingOptions(
        warp_off=warp_off,
        proxy_off=proxy_off,
        forced_proxy=forced_proxy,
        extractor_name=extractor_name,
    )


def as_payload(options: RoutingOptions) -> dict:
    return {
        "warp_off": options.warp_off,
        "proxy_off": options.proxy_off,
        "proxy_url": options.forced_proxy or "",
        "extractor_name": options.extractor_name,
    }


__all__ = ["RoutingOptions", "as_payload", "from_values"]
