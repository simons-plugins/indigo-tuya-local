"""Tuya Local - control Tuya Wi-Fi devices over the LAN, no cloud at runtime.

Thin glue: the protocol lives in tuya_link.py, the DP <-> state mapping in
definitions.py. Neither imports Indigo.

Indigo booleans cannot express "unknown". When the link is down the plugin sets
``connected`` False, ``lastError`` to the reason and puts the device into
Indigo's error state, but stale values such as ``onOffState`` stay as last
seen: automations MUST gate on ``connected``.
"""

import ipaddress
import json

import indigo

import definitions as defs
import tuya_link

RELAY_TYPE = "tuyaDehumidifier"
GENERIC_TYPE = "tuyaGeneric"
VERSIONS = ("3.3", "3.4", "3.5")


def _truthy(value):
    return str(value).strip().lower() in ("true", "1", "yes", "on")


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs, **kwargs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = _truthy(pluginPrefs.get("showDebugInfo", False))
        self._links = {}
        self._ui_text = {}  # dev.id -> last State-column text written
        self._bad_display = {}  # dev.id -> DPs whose display value is wrong-type (warned once)
        self._definitions = {}
        self._device_factory = tuya_link.default_device_factory
        self._timings = None

    # ---------------------------------------------------------------- lifecycle
    def startup(self):
        tuya_link.quiet_tinytuya()  # tinytuya logs the key at DEBUG
        folder = f"{self.pluginFolderPath}/Contents/Server Plugin/definitions"
        self._definitions, problems = defs.load_definitions(folder)
        for name, problem in problems:
            self.logger.error(f"Definition file {name} ignored: {problem}")
        self.logger.info(f"Loaded {len(self._definitions)} product definition(s)")

    def shutdown(self):
        for link in list(self._links.values()):
            link.stop()
        self._links.clear()

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.debug = _truthy(valuesDict.get("showDebugInfo", False))

    def deviceStartComm(self, dev):
        super().deviceStartComm(dev)
        try:
            dev.stateListOrDisplayStateIdChanged()  # must precede the first state write
        except Exception as exc:
            self.logger.error(f'"{dev.name}": could not refresh the state list: {exc}')
        old = self._links.pop(dev.id, None)
        if old:
            old.stop()
        self._forget_display(dev.id)  # after stop(): a callback in flight cannot re-add it
        props = dev.pluginProps
        if dev.deviceTypeId == RELAY_TYPE and self._definition_for(dev) is None:
            self._mark_down(dev, f"Product definition {props.get('definitionId')!r} not found")
            self.logger.error(f'"{dev.name}": product definition {props.get("definitionId")!r} is not available.')
            return
        self._mark_down(dev, "Connecting")
        link = tuya_link.TuyaLink(
            name=dev.name, address=props.get("address", ""), device_id=props.get("deviceId", ""),
            local_key=props.get("localKey", ""), version=props.get("protocolVersion", "3.5"),
            on_dps=lambda dps, i=dev.id: self._on_dps(i, dps),
            on_connection=lambda ok, why, i=dev.id: self._on_connection(i, ok, why),
            on_command_error=lambda dps, why, i=dev.id: self._on_command_error(i, dps, why),
            device_factory=self._device_factory, timings=self._timings, logger=self.logger)
        self._links[dev.id] = link
        link.start()

    def deviceStopComm(self, dev):
        super().deviceStopComm(dev)
        link = self._links.pop(dev.id, None)
        if link:
            link.stop()
        self._forget_display(dev.id)  # after stop(): a callback in flight cannot re-add it
        try:
            self._write_connection(indigo.devices[dev.id], False, "Device stopped")
        except Exception as exc:  # the device may already be deleted
            self.logger.debug(f"deviceStopComm state write skipped: {exc}")

    # ------------------------------------------------------------ link callbacks
    def _device(self, dev_id):
        try:
            return indigo.devices[dev_id]
        except KeyError:
            return None

    def _on_dps(self, dev_id, dps):
        dev = self._device(dev_id)
        if dev is None:
            return
        defn = None
        if dev.deviceTypeId == RELAY_TYPE:
            defn = self._definition_for(dev)
            updates = defs.dps_to_states(defn, dps) if defn else []
        else:
            updates = [{"key": "dps", "value": json.dumps(dps, sort_keys=True)}]
        changed = [u for u in updates if dev.states.get(u["key"]) != u["value"]]
        text = None
        if dev.deviceTypeId == RELAY_TYPE and defn:
            try:
                changed, text = self._with_display_text(dev, defn, dps, updates, changed)
            except Exception:  # a display bug must not block the real state updates
                self.logger.exception(f'"{dev.name}": could not work out the State column text')
                changed = [u for u in updates if dev.states.get(u["key"]) != u["value"]]
        if changed:
            dev.updateStatesOnServer(changed)
            if text is not None:  # remembered only once the write has succeeded
                self._ui_text[dev.id] = text

    def _forget_display(self, dev_id):
        self._ui_text.pop(dev_id, None)
        self._bad_display.pop(dev_id, None)

    def _track_display_value(self, dev, defn, dps):
        """True while the display state's value is current; False after a wrong-type value
        for its DP, until a good one arrives. Warns once per device and DP."""
        bad = self._bad_display.setdefault(dev.id, set())
        rejected = defs.rejected_display_value(defn, dps)
        if rejected:
            dp, raw = rejected
            if dp not in bad:
                bad.add(dp)
                self.logger.warning(f'"{dev.name}": data point {dp} sent {raw!r}, which is the wrong type '
                                    f"for {defn.display_state}; the State column shows only \"on\" until it sends a valid value")
        else:
            bad.discard(defs.display_dp(defn))
        return not bad

    def _with_display_text(self, dev, defn, dps, updates, changed):
        """Return ``(changed, text)``: ``changed`` with the State-column text (display-only
        uiValue on onOffState) attached when it differs from what was last written, and
        that text (None if nothing was attached; the caller memoises it after the write).

        The real onOffState boolean is never replaced. The memo is what was last
        written, so a humidity-only change still refreshes the column.
        """
        merged = dict(dev.states)
        merged.update({u["key"]: u["value"] for u in updates})
        current = self._track_display_value(dev, defn, dps) if defn.display_state else True
        text = defs.display_text(defn, merged, current)
        if text is None or self._ui_text.get(dev.id) == text:
            return changed, None
        entry = next((u for u in changed if u["key"] == "onOffState"), None)
        if entry is None:
            if merged.get("onOffState") is None:
                return changed, None  # no boolean to carry the text yet
            entry = {"key": "onOffState", "value": merged["onOffState"]}
            changed = changed + [dict(entry, uiValue=text)]
        else:
            changed = [dict(u, uiValue=text) if u is entry else u for u in changed]
        return changed, text

    def _on_connection(self, dev_id, connected, reason):
        dev = self._device(dev_id)
        if dev is None:
            return
        if not connected:
            # The error state may replace the text. Not popped on connect: the first
            # status() reaches _on_dps before _on_connection(True).
            self._ui_text.pop(dev_id, None)
        was_up = dev.states.get("connected") is True
        self._write_connection(dev, connected, reason)
        if connected and not was_up:
            self.logger.info(f'"{dev.name}" connected')

    def _on_command_error(self, dev_id, dps, reason):
        dev = self._device(dev_id)
        name = dev.name if dev else dev_id
        self.logger.error(f'"{name}": command {dps} not carried out: {reason}')

    def _mark_down(self, dev, reason):
        self._write_connection(dev, False, reason)

    def _write_connection(self, dev, connected, reason):
        # State writes first: updateStateOnServer clears the error state by default,
        # so the error state must be set (or cleared) last.
        dev.updateStatesOnServer([{"key": "connected", "value": connected},
                                  {"key": "lastError", "value": "" if connected else reason}])
        dev.setErrorStateOnServer(None if connected else reason)

    # ----------------------------------------------------------------- commands
    def _definition_for(self, dev):
        if dev.deviceTypeId != RELAY_TYPE:
            return None
        return self._definitions.get(dev.pluginProps.get("definitionId", ""))

    def _live_link(self, dev, what):
        link = self._links.get(dev.id)
        if link is None or not link.connected:
            self.logger.error(f'"{dev.name}" is not connected ({dev.states.get("lastError", "")}); '
                              f"not sending: {what}")
            return None
        return link

    def _send_raw(self, dev, dps, what):
        link = self._live_link(dev, what)
        if link is None:
            return False
        try:
            link.send(dps)
        except (tuya_link.LinkDownError, ValueError) as exc:
            self.logger.error(f'"{dev.name}": {what} not sent: {exc}')
            return False
        # Sent, not done: state changes only when the device reports it.
        self.logger.info(f'sent "{dev.name}" {what}')
        return True

    def _send_state(self, dev, state, value, what):
        defn = self._definition_for(dev)
        if defn is None:
            self.logger.error(f'"{dev.name}": no product definition; cannot {what}')
            return False
        try:
            dps = defs.state_to_dps(defn, state, value)
        except ValueError as exc:
            self.logger.error(f'"{dev.name}": {what} rejected: {exc}')
            return False
        return self._send_raw(dev, dps, what)

    def actionControlDevice(self, action, dev):
        if dev.deviceTypeId != RELAY_TYPE:
            return
        kind = action.deviceAction
        if kind == indigo.kDeviceAction.TurnOn:
            self._send_state(dev, "onOffState", True, "turn on")
        elif kind == indigo.kDeviceAction.TurnOff:
            self._send_state(dev, "onOffState", False, "turn off")
        elif kind == indigo.kDeviceAction.Toggle:
            if self._live_link(dev, "toggle") is not None:
                self._send_state(dev, "onOffState", not dev.onState, "toggle")

    def actionControlUniversal(self, action, dev):
        if action.deviceAction != indigo.kUniversalAction.RequestStatus:
            return
        link = self._live_link(dev, "status request")
        if link is not None:
            link.request_status()
            self.logger.info(f'sent "{dev.name}" status request')

    def actionSetTargetHumidity(self, action, dev):
        raw = str(action.props.get("targetHumidity", "")).strip()
        try:
            value = int(raw)
        except ValueError:
            self.logger.error(f'"{dev.name}": target humidity {raw!r} is not a whole number')
            return
        self._send_state(dev, "targetHumidity", value, f"set target humidity {value}%")

    def _enum_action(self, action, dev, state):
        value = action.props.get(state, "")
        self._send_state(dev, state, value, f"set {state} {value}")

    def actionSetMode(self, action, dev):
        self._enum_action(action, dev, "mode")

    def actionSetPowerLevel(self, action, dev):
        self._enum_action(action, dev, "powerLevel")

    def actionSetSwing(self, action, dev):
        self._enum_action(action, dev, "swing")

    def actionSetTimer(self, action, dev):
        self._enum_action(action, dev, "timer")

    def _bool_action(self, action, dev, state):
        on = action.props.get("value", "on") == "on"
        self._send_state(dev, state, on, f"set {state} {'on' if on else 'off'}")

    def actionSetIoniser(self, action, dev):
        self._bool_action(action, dev, "ioniser")

    def actionSetChildLock(self, action, dev):
        self._bool_action(action, dev, "childLock")

    @staticmethod
    def _parse_dp(props):
        """Return (dp_str, value); raises ValueError naming the problem."""
        raw_dp = str(props.get("dp", "")).strip()
        if not raw_dp.isdigit() or int(raw_dp) < 1:
            raise ValueError(f"data point {raw_dp!r} must be a positive whole number")
        try:
            value = json.loads(str(props.get("value", "")))
        except ValueError as exc:
            raise ValueError(f"value is not valid JSON ({exc}); strings need double quotes") from exc
        return str(int(raw_dp)), value

    def actionSetDataPoint(self, action, dev):
        try:
            dp, value = self._parse_dp(action.props)
        except ValueError as exc:
            self.logger.error(f'"{dev.name}": {exc}')
            return
        self._send_raw(dev, {dp: value}, f"set data point {dp} to {json.dumps(value)}")

    # ------------------------------------------------------------ config UI bits
    def definitionList(self, filter="", valuesDict=None, typeId="", targetId=0):
        items = sorted((d.id, d.name) for d in self._definitions.values() if d.kind == filter)
        return items or [("", "No product definitions available")]

    def choiceList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Menu for an enum state. Uses the target device's definition when Indigo tells us
        which device; otherwise the union across definitions (execution re-validates)."""
        dev = self._device(targetId) if targetId else None
        defn = self._definition_for(dev) if dev else None
        pool = [defn] if defn else list(self._definitions.values())
        seen, items = set(), []
        for d in pool:
            for ident, label in d.choices(filter):
                if ident not in seen:
                    seen.add(ident)
                    items.append((ident, label))
        return items or [("", "No choices available")]

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errors = indigo.Dict()
        address = str(valuesDict.get("address", "")).strip()
        try:
            ipaddress.ip_address(address)
        except ValueError:
            errors["address"] = "Enter a valid IP address, e.g. 192.168.1.50."
        if not str(valuesDict.get("deviceId", "")).strip():
            errors["deviceId"] = "Enter the Tuya device ID."
        key = str(valuesDict.get("localKey", ""))
        if len(key) != 16:
            errors["localKey"] = f"The local key must be exactly 16 characters (this one is {len(key)})."
        if valuesDict.get("protocolVersion") not in VERSIONS:
            errors["protocolVersion"] = f"Choose one of {', '.join(VERSIONS)}."
        if typeId == RELAY_TYPE and valuesDict.get("definitionId") not in self._definitions:
            errors["definitionId"] = "Choose a product."
        if errors:
            return False, valuesDict, errors
        valuesDict["address"] = address
        valuesDict["deviceId"] = str(valuesDict["deviceId"]).strip()
        return True, valuesDict

    def validateActionConfigUi(self, valuesDict, typeId, deviceId):
        errors = indigo.Dict()
        if typeId == "setTargetHumidity":
            try:
                value = int(str(valuesDict.get("targetHumidity", "")).strip())
            except ValueError:
                errors["targetHumidity"] = "Enter a whole number."
            else:
                dev = self._device(deviceId) if deviceId else None
                defn = self._definition_for(dev) if dev else None
                if defn:
                    try:
                        defs.state_to_dps(defn, "targetHumidity", value)
                    except ValueError as exc:
                        errors["targetHumidity"] = str(exc)
        elif typeId == "setDataPoint":
            try:
                self._parse_dp(valuesDict)
            except ValueError as exc:
                errors["dp" if "data point" in str(exc) else "value"] = str(exc)
        elif typeId in ("setMode", "setPowerLevel", "setSwing", "setTimer"):
            field = {"setMode": "mode", "setPowerLevel": "powerLevel", "setSwing": "swing", "setTimer": "timer"}[typeId]
            if not valuesDict.get(field):
                errors[field] = "Choose a value."
        if errors:
            return False, valuesDict, errors
        return True, valuesDict
