# Operator notes (personal branch)

Operational memo for this branch. Not upstream documentation: it records
the manual settings without which the branch does not work as intended.

## WARP Exclude Domains: mandatory TikTok domains

While Player 1 (dlstreams `/stream/`) serves segments from the TikTok
CDN, the EasyProxy admin panel entry **WARP Exclude Domains** must have
these in its custom list:

```
tiktokcdn-us.com
tiktokcdn.com
```

Bare domains also cover all subdomains (`p16-common-sign.tiktokcdn-us.com`,
etc.: the matcher accepts the `*.` prefix too, and always suffix-matches
the host name).

### Why it is required

- The WARP egress is banned by the TikTok CDN (403) while the server IP
  is clean (200): P1 segments must go out direct.
- All split-routing in this branch keys off `_is_warp_excluded()`:
  playback route (`proxy_core`), probe route
  (`dlstreams._probe_session_for`), and therefore the anti-flip
  hysteresis. Without these two domains in the list, probes go through
  WARP, take intermittent 403s, and the player keeps hopping sources.
- The panel saves to the `warp_exclude_domains_custom` key, which is
  empty by default (shipped defaults cover only debrid hosts).

### When to re-enter them

Only on a **from-scratch setup without a config restore**: the runtime
config (`config.json`) is gitignored and does not follow the branch.
Normal redeploys are covered by Persistent storage (`/data` in the
container) and by backup, so the list survives; but a restart without
a restore brings back an empty custom list and the domains must be
re-entered by hand.

### Verification

In the logs, once a P1 extraction has happened, this must appear:

```
probing pXX-common-sign.tiktokcdn-us.com direct (warp-excluded)
```

If it is missing, the custom list is not active and probes are going
through WARP: re-enter the domains and restart the extraction.
