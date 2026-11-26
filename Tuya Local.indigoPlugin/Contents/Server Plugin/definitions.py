"""Device definitions as data, and the two pure translators.

A definition is a JSON file describing which Tuya data point (DP) maps to which
Indigo state. ``dps_to_states`` turns a DP dict into state updates;
``state_to_dps`` validates a command and turns it into a DP dict. Neither
touches Indigo or the network.

Adding a product = a new JSON file in ``definitions/``. A new *kind* of device
additionally needs a Devices.xml entry.

No ``__file__`` here: Indigo exec()s plugin code, so the caller passes the
directory (plugin.py uses ``self.pluginFolderPath``).
"""

import json
import os

VALID_TYPES = ("bool", "int", "enum", "string", "bitfield")
UNKNOWN_DPS_STATE = "unknownDps"


class DefinitionError(ValueError):
    """A definition file is malformed."""


def is_valid_state_id(key):
    """Indigo's undocumented rule: ASCII letter first, then ASCII letters/digits only."""
    if not key or not key[0].isascii() or not key[0].isalpha():
        return False
    return all(c.isascii() and c.isalnum() for c in key)


class EnumValue:
    def __init__(self, raw, ident, label):
        self.raw, self.id, self.label = raw, ident, label


class DpSpec:
    def __init__(self, entry):
        try:
            self.dp = str(int(entry["dp"]))
            self.state = entry["state"]
            self.type = entry["type"]
        except (KeyError, TypeError, ValueError) as exc:
            raise DefinitionError(f"entry {entry!r}: needs integer 'dp', 'state' and 'type' ({exc})") from exc
        if self.type not in VALID_TYPES:
            raise DefinitionError(f"dp {self.dp}: unknown type {self.type!r}; use one of {VALID_TYPES}")
        if not is_valid_state_id(self.state):
            raise DefinitionError(f"dp {self.dp}: state id {self.state!r} must be camelCase ASCII, no underscores")
        self.settable = bool(entry.get("settable", False))
        self.unit = entry.get("unit")
        self.min, self.max, self.step = entry.get("min"), entry.get("max"), entry.get("step")
        self.values = tuple(EnumValue(v["raw"], v["id"], v.get("label", v["id"])) for v in entry.get("values", []))
        self.bits = {}
        for bit, name in entry.get("bits", {}).items():
            if not is_valid_state_id(name):
                raise DefinitionError(f"dp {self.dp}: bit state id {name!r} must be camelCase ASCII")
            self.bits[int(bit)] = name
        if self.type == "enum":
            if not self.values:
                raise DefinitionError(f"dp {self.dp}: enum needs 'values'")
            ids = [v.id for v in self.values]
            if len(set(ids)) != len(ids):
                raise DefinitionError(f"dp {self.dp}: duplicate enum ids")
        if self.type == "bitfield" and self.settable:
            raise DefinitionError(f"dp {self.dp}: bitfields are read-only")
        if self.type == "bitfield" and not self.bits:
            raise DefinitionError(f"dp {self.dp}: bitfield needs 'bits'")

    def state_ids(self):
        return [self.state] + list(self.bits.values())


class Definition:
    def __init__(self, data):
        try:
            self.id, self.name, self.kind = data["id"], data["name"], data["kind"]
        except KeyError as exc:
            raise DefinitionError(f"missing key {exc}") from exc
        self.product_ids = tuple(data.get("productIds", []))
        self.dps = tuple(DpSpec(e) for e in data.get("dps", []))
        self.by_dp, self.by_state = {}, {}
        for spec in self.dps:
            if spec.dp in self.by_dp:
                raise DefinitionError(f"dp {spec.dp} defined twice")
            self.by_dp[spec.dp] = spec
            for sid in spec.state_ids():
                if sid in self.by_state or sid == UNKNOWN_DPS_STATE:
                    raise DefinitionError(f"state {sid!r} defined twice or reserved")
            self.by_state[spec.state] = spec
        self.display_state = data.get("displayState")
        if self.display_state is not None and (
                not isinstance(self.display_state, str) or self.display_state not in self.by_state):
            raise DefinitionError(f"displayState {self.display_state!r} is not a state this definition maps")
        self.off_display = data.get("offDisplay")
        if self.off_display is not None:
            self._check_display_override("offDisplay", self.off_display, ("state", "minutesState"))
        self.on_display = data.get("onDisplay")
        if self.on_display is not None:
            self._check_display_override("onDisplay", self.on_display, ("state",))

    def _check_display_override(self, name, od, state_keys):
        if self.display_state is None:
            raise DefinitionError(f"{name} needs displayState (the State column text it overrides)")
        if not isinstance(od, dict) or "state" not in od or not isinstance(od.get("text"), str) or not od["text"]:
            raise DefinitionError(f"{name} needs 'state' and non-empty 'text'")
        # Bit names (coolingDown, tankFull) are states but not in by_state, so check state_ids().
        known = set(self.state_ids()) - {UNKNOWN_DPS_STATE}
        for key in state_keys:
            if key in od and (not isinstance(od[key], str) or od[key] not in known):
                raise DefinitionError(f"{name} {key} {od[key]!r} is not a state this definition maps")

    def state_ids(self):
        """Every state id this definition writes, including bit flags and unknownDps."""
        out = [UNKNOWN_DPS_STATE]
        for spec in self.dps:
            out.extend(spec.state_ids())
        return out

    def choices(self, state):
        """[(id, label)] of settable values for an enum state; [] if not an enum."""
        spec = self.by_state.get(state)
        if spec is None or spec.type != "enum" or not spec.settable:
            return []
        return [(v.id, v.label) for v in spec.values]


def load_definitions(directory):
    """Return ({id: Definition}, [(filename, error)]). Bad files are reported, not skipped silently."""
    found, errors = {}, []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError as exc:
        return found, [(str(directory), f"cannot read definitions directory: {exc}")]
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                defn = Definition(json.load(handle))
            if defn.id in found:
                raise DefinitionError(f"duplicate definition id {defn.id!r}")
            found[defn.id] = defn
        except (OSError, ValueError) as exc:  # json.JSONDecodeError and DefinitionError are ValueErrors
            errors.append((name, str(exc)))
    return found, errors


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _split_dps(defn, dps):
    """DP dict -> (state updates, {dp: raw} of DPs that did not translate)."""
    updates, unknown = [], {}
    for key, raw in dps.items():
        dp = str(key)
        spec = defn.by_dp.get(dp)
        if spec is None:
            unknown[dp] = raw
            continue
        if spec.type == "bool" and isinstance(raw, bool):
            updates.append({"key": spec.state, "value": raw})
        elif spec.type == "int" and _is_int(raw):
            entry = {"key": spec.state, "value": raw}
            if spec.unit:
                entry["uiValue"] = f"{raw}{spec.unit}"
            updates.append(entry)
        elif spec.type == "string" and isinstance(raw, str):
            updates.append({"key": spec.state, "value": raw})
        elif spec.type == "enum" and isinstance(raw, str):
            match = next((v.id for v in spec.values if v.raw == raw), raw)
            updates.append({"key": spec.state, "value": match})
        elif spec.type == "bitfield" and _is_int(raw):
            updates.append({"key": spec.state, "value": raw})
            for bit, name in spec.bits.items():
                updates.append({"key": name, "value": bool((raw >> bit) & 1)})
        else:
            unknown[dp] = raw
    return updates, unknown


def dps_to_states(defn, dps):
    """DP dict -> list of ``{"key", "value"[, "uiValue"]}`` state updates.

    Always ends with an ``unknownDps`` entry (JSON string). DPs the definition
    does not know, and known DPs whose value has the wrong type, are placed
    there verbatim rather than dropped or coerced; the known state is left
    untouched in the latter case. An enum value outside the definition is
    written as the raw string.
    """
    updates, unknown = _split_dps(defn, dps)
    updates.append({"key": UNKNOWN_DPS_STATE, "value": json.dumps(unknown, sort_keys=True)})
    return updates


def display_dp(defn):
    """The data point (str) behind the definition's display state, or None."""
    return defn.by_state[defn.display_state].dp if defn.display_state else None


def rejected_display_value(defn, dps):
    """``(dp, raw)`` if this batch carries the display state's DP with a value that did
    not translate (wrong type), else None. The display state is then stale, not current."""
    dp = display_dp(defn)
    if dp is None:
        return None
    unknown = _split_dps(defn, dps)[1]
    return (dp, unknown[dp]) if dp in unknown else None


def display_text(defn, states, display_current=True):
    """Text for the State column of a relay device, or None to leave Indigo's default.

    ``states`` is state id -> value. Off -> "off" (or the definition's ``offDisplay`` text, with minutes, while
    its flag state is True); on -> the definition's display
    state with its unit (e.g. "56%"); on but no usable value yet, or the value is
    known to be stale (``display_current`` False) -> "on". While on, the definition's ``onDisplay``
    text (e.g. "tank full") wins over all of those when its flag state is True.
    """
    if defn.display_state is None:
        return None
    if states.get("onOffState") is False:
        od = defn.off_display
        if od and states.get(od["state"]) is True:
            minutes = states.get(od.get("minutesState"))
            if _is_int(minutes) and minutes > 0:
                return f"{od['text']} {minutes} min"
            return od["text"]
        return "off"
    if defn.on_display and states.get(defn.on_display["state"]) is True:
        return defn.on_display["text"]
    value = states.get(defn.display_state)
    if not display_current or value is None or value == "":
        return "on"
    return f"{value}{defn.by_state[defn.display_state].unit or ''}"


def state_to_dps(defn, state, value):
    """Validate a command and return ``{dp: raw}``. Raises ValueError, sends nothing."""
    spec = defn.by_state.get(state)
    if spec is None:
        settable = sorted(s for s, sp in defn.by_state.items() if sp.settable)
        raise ValueError(f"{state!r} is not a state of {defn.name}; settable: {', '.join(settable)}")
    if not spec.settable:
        raise ValueError(f"{state!r} is read-only")
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{state!r} needs True or False, got {value!r}")
        return {spec.dp: value}
    if spec.type == "int":
        if not _is_int(value):
            raise ValueError(f"{state!r} needs a whole number, got {value!r}")
        if (spec.min is not None and value < spec.min) or (spec.max is not None and value > spec.max):
            raise ValueError(f"{state!r} must be between {spec.min} and {spec.max}, got {value}")
        if spec.step and spec.min is not None and (value - spec.min) % spec.step:
            raise ValueError(f"{state!r} must be a multiple of {spec.step} starting at {spec.min}, got {value}")
        return {spec.dp: value}
    if spec.type == "enum":
        for entry in spec.values:
            if entry.id == value:
                return {spec.dp: entry.raw}
        raise ValueError(f"{value!r} is not valid for {state!r}; allowed values: "
                         f"{', '.join(v.id for v in spec.values)}")
    raise ValueError(f"{state!r} cannot be set (type {spec.type})")
