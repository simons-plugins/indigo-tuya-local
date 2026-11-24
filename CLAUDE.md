# CLAUDE.md — Tuya Local

> **Part of the [Indigo workspace](../CLAUDE.md)** — see root for cross-project map, standards, and tooling.

## Project Identity

- **Name**: Tuya Local
- **Type**: Indigo plugin (`Tuya Local.indigoPlugin`, `com.simons-plugins.indigo-tuya-local`)
- **Shortcut**: `tuya` / `tuya-local`
- **GitHub**: https://github.com/simons-plugins/indigo-tuya-local
- **Language**: Python 3.10+; depends on [tinytuya](https://github.com/jasonacox/tinytuya) 1.21.1 (MIT), **bundled** in `Contents/Server Plugin/tinytuya/`; no `requirements.txt` on purpose — pip would rebuild `cryptography` from source with Rust on Intel Macs, and Indigo already ships `cryptography`

## Role in the workspace

Controls Tuya Wi-Fi devices over the LAN with no cloud at runtime. First target:
Meaco DD8L Pro dehumidifier (product `vvsrpq0oavmf2vou`, protocol 3.5, on a different
subnet from the Indigo server, so UDP discovery cannot work — IP, device ID, local key
and version are typed in). Design modelled on make-all/tuya-local (MIT).

**Layout**: `tuya_link.py` (one thread per device, no Indigo imports, injectable device
factory and timings) · `definitions.py` + `definitions/*.json` (DP <-> state mapping as
data; pure translators) · `plugin.py` (thin glue).

**Indigo booleans cannot express "unknown".** When the link is down the plugin sets
`connected` False, `lastError` to the reason and puts the device in Indigo's error state,
but `onOffState` and every other state keep their last-seen values. **Automations must
gate on `connected`.**

**Error 914 (wrong key/version) backs off for 10 minutes and never retries sooner or tries
other versions/keys** — repeated failed 3.4/3.5 handshakes can lock a device's LAN access
until it is power-cycled. Do not "improve" this.

**tinytuya logs the local key (and session key) at DEBUG.** `quiet_tinytuya()` pins its
logger to WARNING; never raise it, and never log the key anywhere (the link redacts it).

Gotchas learned live (see `tuya_link.py` docstring): any call can return a pushed status
frame instead of its own reply; every reply goes through one handler; partial frames
merge, never replace the cache.

## Related projects

Standalone — no sibling dependencies in this workspace.

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points for this project:

- **Version bump per PR**: `Info.plist` `PluginVersion` (`YYYY.R.P`); `CFBundleVersion` stays `1.0.0`
- **Testing**: pytest, `indigo` stubbed with real classes in `tests/conftest.py`, fake tinytuya device, no network. Degradation-path tests are the point: see the root "Degradation-path tests" section
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for user go-ahead
- **New products**: add `definitions/<id>.json`; `tests/test_xml.py` checks definition states against `Devices.xml`
