import aiohttp
from config import resolve_proxy_alias
import services.proxy_shared as _shared
from services.proxy_shared import (
    get_ssl_setting_for_url,
    logger,
    hex_to_b64url,
    parse_clearkey_params,
    web,
)


class HLSProxyLicenseHandlerMixin:

    async def handle_license_request(self, request):
        """✅ NUOVO: Gestisce le richieste di licenza DRM (ClearKey e Proxy)"""
        try:
            # 1. Modalità ClearKey Statica
            clearkey_param = parse_clearkey_params(request)
            if clearkey_param:
                logger.debug("🔐 Static ClearKey license request")
                try:
                    # Support multiple keys separated by comma
                    # Format: KID1:KEY1,KID2:KEY2
                    key_pairs = clearkey_param.split(",")
                    keys_jwk = []

                    for pair in key_pairs:
                        if ":" in pair:
                            kid_hex, key_hex = pair.split(":")
                            keys_jwk.append(
                                {
                                    "kty": "oct",
                                    "k": hex_to_b64url(key_hex),
                                    "kid": hex_to_b64url(kid_hex),
                                    "type": "temporary",
                                }
                            )

                    if not keys_jwk:
                        raise ValueError("No valid keys found")

                    jwk_response = {"keys": keys_jwk, "type": "temporary"}

                    logger.info(
                        f"🔐 Serving static ClearKey license with {len(keys_jwk)} keys"
                    )
                    return web.json_response(jwk_response)
                except Exception as e:
                    logger.error(f"❌ Error generating static ClearKey license: {e}")
                    return web.Response(text="Invalid ClearKey format", status=400)

            # 2. Modalità Proxy Licenza
            license_url = request.query.get("url")
            if not license_url:
                return web.Response(text="Missing url parameter", status=400)

            # aiohttp already decodes query parameters once.
            # Avoid unquoting again or embedded encoded URLs may break.

            # Ricostruisce gli headers
            headers = {}
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:].replace("_", "-")
                    if header_name.lower() in {"x-direct-connection", "x-force-direct"}:
                        continue
                    headers[header_name] = param_value

            # Aggiunge headers specifici della richiesta originale (es. content-type per il body)
            if request.headers.get("Content-Type"):
                headers["Content-Type"] = request.headers.get("Content-Type")

            # Legge il body della richiesta (challenge DRM), max 100KB
            body = await request.read(100000)

            logger.info(f"🔐 Proxying License Request to: {license_url}")

            # ✅ Use pooled session for better performance
            _lic_proxy_token = None
            raw_proxy = request.query.get("proxy") or None
            forced_proxy = resolve_proxy_alias(raw_proxy)
            if raw_proxy and raw_proxy.lower() in {"off", "false", "0", "direct", "none"}:
                forced_proxy = None
                _lic_proxy_token = _shared.BYPASS_PROXIES_CONTEXT.set(True)
            bypass_warp = request.query.get("warp", "").lower() in {"off", "false", "0", "direct"}
            extractor_key = request.query.get("extractor_key")
            if extractor_key:
                ext_warp_off, ext_proxy_off = _shared.get_extractor_routing_overrides(extractor_key)
                if ext_warp_off:
                    bypass_warp = True
                if ext_proxy_off:
                    forced_proxy = None
                    if _lic_proxy_token is None:
                        _lic_proxy_token = _shared.BYPASS_PROXIES_CONTEXT.set(True)
            if hasattr(self, "_discard_disabled_warp_route"):
                forced_proxy = self._discard_disabled_warp_route(forced_proxy, bypass_warp)
            elif bypass_warp and forced_proxy and _shared.is_warp_proxy_url(forced_proxy):
                forced_proxy = None

            disable_ssl = (
                request.query.get("disable_ssl", "").lower() in {"1", "true", "yes", "on"}
                or get_ssl_setting_for_url(license_url)
            )
            # Widevine license requests are part of the same playback as the
            # rewritten MPD. Reuse that playback's isolated pool instead of
            # falling back to the shared proxy session.
            stream_key = request.query.get("stream_key")
            session = None
            proxy_used = None
            try:
                if hasattr(self, "_should_force_direct_from_query") and self._should_force_direct_from_query(request):
                    session = await self._get_session(url=license_url)
                else:
                    session, proxy_used = await self._get_proxy_session(
                        license_url,
                        bypass_warp=bypass_warp,
                        forced_proxy=forced_proxy,
                        session_key=stream_key,
                    )
                async with session.request(
                    request.method, license_url, headers=headers, data=body,
                    ssl=not disable_ssl,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    response_body = await resp.read()
                    logger.info(
                        f"✅ License response: {resp.status} ({len(response_body)} bytes)"
                    )

                    response_headers = {
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "*",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    }
                    # Copia alcuni headers utili dalla risposta originale
                    if "Content-Type" in resp.headers:
                        response_headers["Content-Type"] = resp.headers["Content-Type"]

                    return web.Response(
                        body=response_body, status=resp.status, headers=response_headers
                    )
            finally:
                if _lic_proxy_token is not None:
                    _shared.BYPASS_PROXIES_CONTEXT.reset(_lic_proxy_token)
                if proxy_used and session and not session.closed:
                    await session.close()

        except Exception as e:
            logger.error(f"❌ License proxy error: {str(e)}")
            return web.Response(text=f"License error: {str(e)}", status=500)
