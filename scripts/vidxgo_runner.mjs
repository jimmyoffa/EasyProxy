/**
 * VidXgo TLS-fingerprint runner.
 *
 * The VidXgo CDN (cdn.v1.*.d2b.you) rejects Python HTTP clients (curl_cffi and
 * tls_client included) with HTTP 403. The site token endpoint /t/<id> also
 * hands out URLs only for the exact client that asked. This runner mirrors the
 * easystreams addon: fetch the token with a Firefox 120 TLS fingerprint, then
 * download the master playlist (and variants when deep=true) with the same
 * session.
 *
 * Usage: node vidxgo_runner.mjs <embed-url> [deep]
 * Output: JSON on stdout:
 *   {"destination_url", "request_headers", "captured_manifest", "captured_manifests"}
 * or {"error": "..."}.
 */

import tlsClient from "tls-client";

const { Session } = tlsClient;

const UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0";
const TOKEN_HEADERS = {
  "User-Agent": UA,
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
  "Referer": "https://v.vidxgo.co/",
  "Origin": "https://v.vidxgo.co",
  "Sec-Fetch-Dest": "empty",
  "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Site": "same-origin",
};
const PLAY_HEADERS = {
  "User-Agent": UA,
  "Accept": "*/*",
  "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
  "Referer": "https://v.vidxgo.co/",
  "Origin": "https://v.vidxgo.co",
  "Sec-Fetch-Dest": "empty",
  "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Site": "cross-site",
};
const ORIGIN = "https://v.vidxgo.co";
const MAX_PLAYLISTS = 16;

function fail(message) {
  console.log(JSON.stringify({ error: message }));
  process.exit(1);
}

function buildTokenEndpoint(embedUrl) {
  let parsed;
  try {
    parsed = new URL(embedUrl);
  } catch {
    fail(`invalid embed URL: ${embedUrl}`);
  }
  if (!/(^|\.)vidxgo\.co$/i.test(parsed.hostname)) {
    fail(`unsupported host: ${parsed.hostname}`);
  }
  const parts = parsed.pathname.split("/").filter(Boolean);
  const first = (parts[0] || "").toLowerCase();
  const idMatch = first.match(/^(?:tt)?(\d+)$/i);
  if (!idMatch) {
    fail(`cannot find a numeric VidXgo id in path: ${parsed.pathname}`);
  }
  const numericId = idMatch[1];
  const season = parts[1] && /^\d+$/.test(parts[1]) ? parts[1] : null;
  const episode = parts[2] && /^\d+$/.test(parts[2]) ? parts[2] : null;
  const suffix = season && episode ? `/${season}/${episode}` : "";
  return `${ORIGIN}/t/${numericId}${suffix}`;
}

function normalizeProxy(value) {
  const proxy = String(value || "").trim();
  if (!proxy) return "";
  return proxy.replace(/^socks5h:/i, "socks5:");
}

async function collectVariants(session, masterUrl, masterText, captured, proxy) {
  const seen = new Set([masterUrl]);
  const pending = [[masterUrl, masterText]];
  while (pending.length && seen.size < MAX_PLAYLISTS) {
    const [parentUrl, manifest] = pending.shift();
    let variantNext = false;
    for (const rawLine of manifest.split(/\r?\n/)) {
      const line = rawLine.trim();
      let child = null;
      if (line.startsWith("#EXT-X-STREAM-INF:")) {
        variantNext = true;
        continue;
      }
      if (line.startsWith("#EXT-X-MEDIA:") && line.includes('URI="')) {
        child = line.match(/URI="([^"]+)"/)?.[1] || null;
      } else if (line && !line.startsWith("#")) {
        if (variantNext) child = line;
        variantNext = false;
      }
      if (!child) continue;
      let childUrl;
      try {
        childUrl = new URL(child, parentUrl).href;
      } catch {
        continue;
      }
      if (seen.has(childUrl) || seen.size >= MAX_PLAYLISTS) continue;
      seen.add(childUrl);
      const res = await session.get(childUrl, {
        headers: PLAY_HEADERS,
        proxy: proxy || undefined,
        allowRedirects: true,
        insecureSkipVerify: true,
        timeoutSeconds: 20,
      });
      if (res.status === 200 && res.text.includes("#EXTM3U")) {
        captured[childUrl] = res.text;
        pending.push([childUrl, res.text]);
      }
    }
  }
}

async function main() {
  const embedUrl = process.argv[2];
  const deep = (process.argv[3] || "").toLowerCase() === "deep" ||
    process.env.VIDXGO_CAPTURE_VARIANTS === "1";
  if (!embedUrl) fail("usage: vidxgo_runner.mjs <embed-url> [deep]");

  const proxy = normalizeProxy(process.env.VIDXGO_PROXY);
  const endpoint = buildTokenEndpoint(embedUrl);
  const session = new Session({ clientIdentifier: "firefox_120", randomTlsExtensionOrder: false });
  const requestOptions = {
    proxy: proxy || undefined,
    allowRedirects: true,
    insecureSkipVerify: true,
    timeoutSeconds: 25,
  };

  const tokenResponse = await session.get(endpoint, { ...requestOptions, headers: TOKEN_HEADERS });
  if (tokenResponse.status !== 200) {
    fail(`token endpoint returned HTTP ${tokenResponse.status}`);
  }

  let payload;
  try {
    payload = JSON.parse(tokenResponse.text);
  } catch {
    fail("token endpoint did not return JSON");
  }
  const streamUrl = typeof payload?.url === "string" ? payload.url.trim() : "";
  if (!streamUrl.startsWith("http")) {
    fail("token endpoint returned no stream URL");
  }

  const masterResponse = await session.get(streamUrl, { ...requestOptions, headers: PLAY_HEADERS });
  if (masterResponse.status !== 200 || !masterResponse.text.includes("#EXTM3U")) {
    fail(`CDN returned HTTP ${masterResponse.status} for the master playlist`);
  }

  const captured = { [streamUrl]: masterResponse.text };
  if (deep) {
    await collectVariants(session, streamUrl, masterResponse.text, captured, proxy);
  }

  console.log(JSON.stringify({
    destination_url: streamUrl,
    request_headers: PLAY_HEADERS,
    captured_manifest: masterResponse.text,
    captured_manifests: captured,
  }));
  process.exit(0);
}

main().catch((error) => fail(error?.message || String(error)));
