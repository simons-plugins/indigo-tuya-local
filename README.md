# Tuya Local

An [Indigo](https://www.indigodomo.com) plugin that controls Tuya Wi-Fi devices over your LAN. The cloud is not used at runtime.

Status: first version, running against one real Meaco DD8L Pro dehumidifier (protocol 3.5) on Indigo 2026.1. No other hardware has been tested. More products can be added as definition files.

## Setup

1. Get the device's **local key** and **device ID** once (e.g. `python -m tinytuya wizard`; this is the only cloud step and is not done by this plugin).
2. Install the plugin by double-clicking `Tuya Local.indigoPlugin`. There is nothing else to install.
3. Create a **Tuya Dehumidifier** device and enter the IP address, device ID, local key, protocol version (3.5 for the Meaco) and product. Use **Tuya Generic** for any other device: it exposes the raw data points as JSON in the `dps` state and the **Set Data Point** action.

There is no auto-discovery: UDP broadcast does not cross subnets. Give the device a static IP.

## Always gate on `connected`

Indigo booleans cannot express "unknown". When the link is down, the device is put in Indigo's error state, `connected` is false and `lastError` says why, but `onOffState`, `humidity` and the rest keep their last-seen values. **Automations must check `connected` first**, or a dead link reads as "dehumidifier is off".

Commands sent while disconnected are refused and logged. The plugin never flips a state optimistically: states change only when the device reports them. "sent" in the log means sent, not done.

## Wrong key or version

Error 914 (key or version rejected) is retried only every 10 minutes, and never with other versions or keys. Repeated failed 3.4/3.5 handshakes can lock a device's LAN access until it is power-cycled. Fix the key/version, then disable and re-enable the device.

## Dependencies

[tinytuya](https://github.com/jasonacox/tinytuya) 1.21.1 (MIT, pure Python, the protocol implementation) is bundled in `Contents/Server Plugin/tinytuya/` with its licence. There is deliberately **no `requirements.txt`**: tinytuya needs `cryptography` for protocol 3.5 (AES-GCM), Indigo already ships `cryptography`, and letting pip install it again pulls the newest release, which has no prebuilt package for Intel Macs and so compiles from source with Rust (about 2.5 minutes, seen live on an Intel server on Python 3.13). To update tinytuya, replace the bundled folder with the package from the matching PyPI release.

## Local key storage and logging

`secure="true"` on the local-key field only masks it in the UI; Indigo stores it in clear in its database. The plugin never logs it. tinytuya itself logs keys at DEBUG, so the plugin pins tinytuya's logger to WARNING.

## Adding a product

Drop a JSON file in `Server Plugin/definitions/` mapping DPs to state ids (see `meaco_dd8l_pro.json`). A new *kind* of device also needs a `Devices.xml` entry. `python -m pytest` checks that definition states and `Devices.xml` agree.

## Credits

The Meaco DP map and the connection design (persistent socket, heartbeat, periodic full poll) follow [make-all/tuya-local](https://github.com/make-all/tuya-local) (MIT) and Tuya's published product model. Protocol implementation: [tinytuya](https://github.com/jasonacox/tinytuya) by Jason Cox (MIT).

## Development

`python -m pytest tests -q`. No network, no hardware, no Indigo needed.
