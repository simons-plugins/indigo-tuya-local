# Changelog

## 2026.0.1

- First version. Tuya Dehumidifier (Meaco DD8L Pro definition) and Tuya Generic devices over the LAN via tinytuya 1.21.1 (bundled; no requirements.txt, so nothing is compiled at install).
- Per-device connection thread: persistent socket, heartbeat, periodic full status, reconnect with backoff, 10-minute backoff on error 914.
- `connected` / `lastError` states and Indigo error state while the link is down.
- Actions: target humidity, mode, power level, swing, off-timer, ioniser, child lock, set data point.
- Not yet verified against hardware.
