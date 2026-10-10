# Phase 3D.3C ChromeDriver Cache Repair

Status: **PASS / GO for a newly approved canary**.

## Root cause

The runtime user is `wsl` (`uid=1000`). The original cache directory and driver
are owned by `wsl:wsl` with ordinary `0755` modes, but `/` is mounted read-only
in the managed execution environment. The failure was therefore a filesystem
restriction, not incorrect Unix ownership or permissions.

Installed `undetected-chromedriver` 3.5.5 exposes a documented
`driver_executable_path` binary override, but no cache-directory argument. Its
installed `Patcher` source does not consult `XDG_DATA_HOME` or `XDG_CACHE_HOME`;
on Linux it computes the cache as
`os.path.expanduser("~/.local/share/undetected_chromedriver")` at import time.
A binary override was not used because it would bypass the package's normal
download/version-management path.

## Repair

No application code, system Chrome installation, or filesystem permissions
were changed. Maps browser processes must be launched with this process-scoped
environment assignment:

```bash
HOME=/home/wsl/GitHub/GMapsScraper/data/phase_3d3_canary/browser_cache \
  .venv/bin/python maps.py [...new approved canary arguments...]
```

This makes the package cache resolve to:

`/home/wsl/GitHub/GMapsScraper/data/phase_3d3_canary/browser_cache/.local/share/undetected_chromedriver`

The cache root is writable and covered by `/data/` in `.gitignore`. This keeps
legacy and authoritative Maps code paths identical while isolating all HOME-
relative browser support files beneath the canary cache root.

## Verification

One real `GoogleMaps.get_or_create_driver()` smoke test ran headless with the
isolated `HOME` and did not navigate to Google Maps, Google Search, or any other
site. Chrome 151.0.7922.169 initialized successfully using the isolated driver,
then `quit_driver()` cleared the worker reference. No cache-scoped Chrome or
ChromeDriver process remained. The first sandboxed attempt stopped at blocked
DNS before browser creation; the authorized network-enabled retry succeeded.

Production and shadow registry SHA-256 values remain
`ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22` and
`ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
All 65 protected CSV/XLSX hashes matched. The failed Phase 3D.3B run remains
`PARTIAL`; no database, historical, lead, or outreach data was modified.

## Remaining risk and recommendation

The `HOME` assignment is an operational requirement and must appear on every
approved Maps invocation in this managed environment. A future package version
could change Linux cache resolution, so upgrades require re-verification.

Recommendation: **GO** for one newly approved Phase 3D.3B canary with a new run
ID and the exact isolated `HOME` prefix above. Do not launch it automatically.
