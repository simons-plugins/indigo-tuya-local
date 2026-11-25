# Changelog

## 2026.2.0

- While the dehumidifier runs its cool-down after being switched off, the State column shows `cooling 11 min` (the countdown from the device) instead of `off`; just `cooling` when no minutes are reported. Display only: `onOffState` stays `False`. Definitions opt in with `offDisplay` (`state`, `text`, optional `minutesState`), validated at load.

## 2026.1.0

- The dehumidifier's State column shows the current humidity while running (e.g. `56%`) and `off` when switched off. Display only: `onOffState` keeps its real boolean value, and the device stays a relay (icon, native on/off and triggers unchanged). Definitions opt in with `displayState`.
- Hardened that text: it is remembered only after the write succeeds, a failure working it out never blocks real state updates, a wrong-type humidity value shows `on` with one warning, and a reconnect no longer re-emits readings from before the drop.
- Verified on Indigo 2026.1 with a Meaco DD8L Pro: the State column reads `60%` while `onOffState` is still `True`.

## 2026.0.1

- First version. Tuya Dehumidifier (Meaco DD8L Pro definition) and Tuya Generic devices over the LAN via tinytuya 1.21.1 (bundled; no requirements.txt, so nothing is compiled at install).
- Per-device connection thread: persistent socket, heartbeat, periodic full status, reconnect with backoff, 10-minute backoff on error 914.
- `connected` / `lastError` states and Indigo error state while the link is down.
- Actions: target humidity, mode, power level, swing, off-timer, ioniser, child lock, set data point.
- Not yet verified against hardware.
