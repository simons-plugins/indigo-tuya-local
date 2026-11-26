import json

import pytest

import definitions as defs
from conftest import SERVER_PLUGIN_DIR, STATUS


@pytest.fixture(scope="module")
def meaco():
    found, errors = defs.load_definitions(SERVER_PLUGIN_DIR / "definitions")
    assert errors == []
    return found["meaco_dd8l_pro"]


def states(defn, dps):
    return {u["key"]: u["value"] for u in defs.dps_to_states(defn, dps)}


def test_module_is_the_file_not_the_directory():
    assert defs.__file__.endswith("definitions.py")


def test_real_status_translates(meaco):
    s = states(meaco, STATUS)
    assert s["onOffState"] is True and s["targetHumidity"] == 55 and s["humidity"] == 56
    assert s["mode"] == "dehumidify" and s["powerLevel"] == "auto" and s["swing"] == "medium"
    assert s["timer"] == "cancel" and s["timerRemaining"] == "cancel" and s["ioniser"] is True


def test_unknown_dp_only_in_unknown_dps(meaco):
    s = states(meaco, STATUS)
    assert json.loads(s["unknownDps"]) == {"103": "drying"}
    known = {str(sp.dp) for sp in meaco.dps}
    assert not (set(json.loads(s["unknownDps"])) & known)
    assert json.loads(states(meaco, {"1": True})["unknownDps"]) == {}


def test_ui_value_for_percent(meaco):
    h = [u for u in defs.dps_to_states(meaco, {"6": 56}) if u["key"] == "humidity"][0]
    assert h["uiValue"] == "56%"


@pytest.mark.parametrize("value,flags", [
    (8, dict(tankFull=False, cleanFilter=False, cableTidy=False, coolingDown=False)),
    (0, dict(tankFull=False, cleanFilter=False, cableTidy=False, coolingDown=False)),
    (1, dict(tankFull=True, cleanFilter=False, cableTidy=False, coolingDown=False)),
    (1 | 2 | 16, dict(tankFull=True, cleanFilter=True, cableTidy=False, coolingDown=True)),
    (4 | 8, dict(tankFull=False, cleanFilter=False, cableTidy=True, coolingDown=False)),
])
def test_fault_bitfield(meaco, value, flags):
    s = states(meaco, {"19": value})
    assert {k: s[k] for k in flags} == flags and s["faultCode"] == value


def test_wrong_type_is_surfaced_not_coerced_or_dropped(meaco):
    s = states(meaco, {"2": "fifty", "19": "x", "1": 1})
    assert "targetHumidity" not in s and "faultCode" not in s and "onOffState" not in s
    assert json.loads(s["unknownDps"]) == {"1": 1, "2": "fifty", "19": "x"}


def test_unrecognised_enum_value_passes_through_raw(meaco):
    assert states(meaco, {"5": "Sleep"})["mode"] == "Sleep"


def test_int_dp_keys_accepted(meaco):
    assert states(meaco, {2: 50})["targetHumidity"] == 50


@pytest.mark.parametrize("state,value,expected", [
    ("onOffState", True, {"1": True}), ("targetHumidity", 35, {"2": 35}), ("targetHumidity", 70, {"2": 70}),
    ("powerLevel", "200w", {"101": "200w"}), ("swing", "off", {"105": "n_swing"}), ("mode", "drying", {"5": "drying"}),
    ("timer", "cancel", {"17": "cancel"}), ("ioniser", False, {"10": False}), ("childLock", True, {"16": True}),
])
def test_valid_commands(meaco, state, value, expected):
    assert defs.state_to_dps(meaco, state, value) == expected


@pytest.mark.parametrize("state,value,needle", [
    ("targetHumidity", 30, "between 35 and 70"), ("targetHumidity", 75, "between 35 and 70"),
    ("targetHumidity", 52, "multiple of 5"), ("targetHumidity", "55", "whole number"),
    ("targetHumidity", True, "whole number"), ("targetHumidity", 55.0, "whole number"),
    ("powerLevel", "turbo", "allowed values: 200w, 400w, 600w, auto"),
    ("swing", "200w", "allowed values"), ("mode", "Dehumidify", "allowed values"),
    ("onOffState", "on", "True or False"), ("onOffState", 1, "True or False"),
    ("humidity", 50, "read-only"), ("faultCode", 0, "read-only"), ("nonsense", 1, "not a state"),
])
def test_invalid_commands_raise_naming_the_problem(meaco, state, value, needle):
    with pytest.raises(ValueError, match=needle):
        defs.state_to_dps(meaco, state, value)


def test_choices(meaco):
    assert [i for i, _ in meaco.choices("powerLevel")] == ["200w", "400w", "600w", "auto"]
    assert meaco.choices("humidity") == [] and meaco.choices("nope") == []


def test_bad_definitions_are_reported_not_swallowed(tmp_path):
    (tmp_path / "bad_json.json").write_text("{nope")
    (tmp_path / "bad_state.json").write_text(json.dumps(
        {"id": "x", "name": "x", "kind": "k", "dps": [{"dp": 1, "state": "snake_case", "type": "bool"}]}))
    (tmp_path / "dup.json").write_text(json.dumps(
        {"id": "y", "name": "y", "kind": "k", "dps": [{"dp": 1, "state": "a", "type": "bool"},
                                                       {"dp": 1, "state": "b", "type": "bool"}]}))
    found, errors = defs.load_definitions(tmp_path)
    assert found == {} and {n for n, _ in errors} == {"bad_json.json", "bad_state.json", "dup.json"}


def test_missing_directory_is_an_error_not_an_empty_success(tmp_path):
    found, errors = defs.load_definitions(tmp_path / "missing")
    assert found == {} and errors


# ------------------------------------------------------------ display_text
def test_display_text_on_shows_humidity_with_unit(meaco):
    assert defs.display_text(meaco, {"onOffState": True, "humidity": 56}) == "56%"


def test_display_text_off_wins_even_with_a_humidity_value(meaco):
    assert defs.display_text(meaco, {"onOffState": False, "humidity": 56}) == "off"


@pytest.mark.parametrize("states", [{"onOffState": True}, {"onOffState": True, "humidity": None}])
def test_display_text_on_without_usable_humidity_says_on(meaco, states):
    assert defs.display_text(meaco, states) == "on"


def test_display_text_is_none_without_display_state():
    defn = defs.Definition({"id": "x", "name": "x", "kind": "k", "dps": [{"dp": 1, "state": "onOffState", "type": "bool"}]})
    assert defs.display_text(defn, {"onOffState": True}) is None


def test_display_state_naming_an_unmapped_state_is_rejected_at_load(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps(
        {"id": "x", "name": "x", "kind": "k", "displayState": "nope",
         "dps": [{"dp": 1, "state": "onOffState", "type": "bool"}]}))
    found, errors = defs.load_definitions(tmp_path)
    assert found == {} and len(errors) == 1 and "nope" in errors[0][1]


# ------------------------------------------------------------ offDisplay (cool-down text)
def test_display_text_off_and_cooling_shows_minutes(meaco):
    states = {"onOffState": False, "coolingDown": True, "timerMinutesLeft": 11, "humidity": 56}
    assert defs.display_text(meaco, states) == "cooling 11 min"


@pytest.mark.parametrize("minutes", [0, -3, None, True, "11", 11.0])
def test_display_text_off_and_cooling_without_usable_minutes_says_cooling(meaco, minutes):
    assert defs.display_text(meaco, {"onOffState": False, "coolingDown": True, "timerMinutesLeft": minutes}) == "cooling"


def test_display_text_off_and_cooling_with_no_minutes_state_says_cooling(meaco):
    assert defs.display_text(meaco, {"onOffState": False, "coolingDown": True}) == "cooling"


@pytest.mark.parametrize("cooling", [False, None])
def test_display_text_off_not_cooling_says_off(meaco, cooling):
    assert defs.display_text(meaco, {"onOffState": False, "coolingDown": cooling, "timerMinutesLeft": 11}) == "off"


def test_display_text_on_ignores_cooling_flag(meaco):
    states = {"onOffState": True, "coolingDown": True, "timerMinutesLeft": 11, "humidity": 56}
    assert defs.display_text(meaco, states) == "56%"


_OD_DPS = [{"dp": 1, "state": "onOffState", "type": "bool"},
           {"dp": 18, "state": "minutesLeft", "type": "int", "settable": False},
           {"dp": 19, "state": "faultCode", "type": "bitfield", "settable": False, "bits": {"4": "coolingDown"}}]


def _load_od(tmp_path, **extra):
    data = {"id": "x", "name": "x", "kind": "k", "displayState": "onOffState", "dps": _OD_DPS}
    data.update(extra)
    data = {k: v for k, v in data.items() if v is not _DROP}
    (tmp_path / "bad.json").write_text(json.dumps(data))
    return defs.load_definitions(tmp_path)


_DROP = object()
_OK = {"state": "coolingDown", "text": "cooling", "minutesState": "minutesLeft"}


def test_valid_off_display_loads(tmp_path):
    found, errors = _load_od(tmp_path, offDisplay=_OK)
    assert errors == [] and "x" in found


@pytest.mark.parametrize("od, named", [
    ({"state": "nope", "text": "cooling"}, "state"),
    ({"state": "unknownDps", "text": "cooling"}, "state"),
    ({"state": ["coolingDown"], "text": "cooling"}, "state"),
    ({**_OK, "minutesState": "nope"}, "minutesState"),
    ({**_OK, "minutesState": ["minutesLeft"]}, "minutesState"),
    ({"state": "coolingDown"}, "text"),
    ({"text": "cooling"}, "state"),
    ({**_OK, "text": ""}, "text"),
    ({**_OK, "text": 5}, "text"),
    ("cooling", "offDisplay"),
    (["coolingDown"], "offDisplay"),
])
def test_bad_off_display_is_a_reported_error_not_a_crash(tmp_path, od, named):
    found, errors = _load_od(tmp_path, offDisplay=od)
    assert found == {} and len(errors) == 1 and "offDisplay" in errors[0][1] and named in errors[0][1]


def test_off_display_without_display_state_is_rejected(tmp_path):
    found, errors = _load_od(tmp_path, offDisplay=_OK, displayState=_DROP)
    assert found == {} and len(errors) == 1 and "displayState" in errors[0][1]


def test_unhashable_display_state_is_a_reported_error(tmp_path):
    found, errors = _load_od(tmp_path, displayState=["onOffState"])
    assert found == {} and len(errors) == 1 and "displayState" in errors[0][1]


def test_display_text_without_off_display_is_plain_off_or_value(tmp_path):
    found, errors = _load_od(tmp_path, displayState="minutesLeft")
    assert errors == []
    d = found["x"]
    assert d.off_display is None
    assert defs.display_text(d, {"onOffState": False, "minutesLeft": 5}) == "off"
    assert defs.display_text(d, {"onOffState": True, "minutesLeft": 5}) == "5"


# ------------------------------------------------------------ onDisplay (tank full text)
_ON_OK = {"state": "tankFull", "text": "tank full"}
_ON_DPS = _OD_DPS + [{"dp": 20, "state": "humidity", "type": "int", "unit": "%", "settable": False},
                     {"dp": 21, "state": "faults", "type": "bitfield", "settable": False, "bits": {"0": "tankFull"}}]


def _load_on(tmp_path, **extra):
    data = {"id": "x", "name": "x", "kind": "k", "displayState": "humidity", "dps": _ON_DPS}
    data.update(extra)
    data = {k: v for k, v in data.items() if v is not _DROP}
    (tmp_path / "bad.json").write_text(json.dumps(data))
    return defs.load_definitions(tmp_path)


def _on_defn(tmp_path, **extra):
    found, errors = _load_on(tmp_path, **extra)
    assert errors == []
    return found["x"]


def test_display_text_on_and_tank_full_shows_text(tmp_path):
    d = _on_defn(tmp_path, onDisplay=_ON_OK)
    assert defs.display_text(d, {"onOffState": True, "tankFull": True, "humidity": 52}) == "tank full"


@pytest.mark.parametrize("flag", [False, None, "missing"])
def test_display_text_on_and_tank_not_full_shows_humidity(tmp_path, flag):
    d = _on_defn(tmp_path, onDisplay=_ON_OK)
    states = {"onOffState": True, "humidity": 52}
    if flag != "missing":
        states["tankFull"] = flag
    assert defs.display_text(d, states) == "52%"


@pytest.mark.parametrize("humidity", [52, None])
def test_display_text_tank_full_wins_over_stale_or_missing_value(tmp_path, humidity):
    d = _on_defn(tmp_path, onDisplay=_ON_OK)
    states = {"onOffState": True, "tankFull": True, "humidity": humidity}
    assert defs.display_text(d, states, display_current=False) == "tank full"


def test_display_text_off_and_tank_full_still_says_off(tmp_path):
    d = _on_defn(tmp_path, onDisplay=_ON_OK, offDisplay=_OK)
    assert defs.display_text(d, {"onOffState": False, "tankFull": True}) == "off"
    assert defs.display_text(d, {"onOffState": False, "tankFull": True, "coolingDown": True,
                                 "minutesLeft": 4}) == "cooling 4 min"


def test_definition_without_on_display_is_unchanged(tmp_path):
    d = _on_defn(tmp_path)
    assert d.on_display is None
    assert defs.display_text(d, {"onOffState": True, "tankFull": True, "humidity": 52}) == "52%"


@pytest.mark.parametrize("od, named", [
    ({"text": "tank full"}, "state"),
    ({"state": "nope", "text": "tank full"}, "state"),
    ({"state": ["tankFull"], "text": "tank full"}, "state"),
    ({"state": "tankFull", "text": ""}, "text"),
    ({"state": "tankFull"}, "text"),
    ("tank full", "onDisplay"),
    (["tankFull"], "onDisplay"),
])
def test_bad_on_display_is_a_reported_error_not_a_crash(tmp_path, od, named):
    found, errors = _load_on(tmp_path, onDisplay=od)
    assert found == {} and len(errors) == 1 and "onDisplay" in errors[0][1] and named in errors[0][1]


def test_on_display_without_display_state_is_rejected(tmp_path):
    found, errors = _load_on(tmp_path, onDisplay=_ON_OK, displayState=_DROP)
    assert found == {} and len(errors) == 1 and "onDisplay" in errors[0][1] and "displayState" in errors[0][1]


def test_meaco_on_with_tank_full_says_tank_full(meaco):
    states = {"onOffState": True, "tankFull": True, "humidity": 52}
    assert defs.display_text(meaco, states) == "tank full"
    assert defs.display_text(meaco, {**states, "tankFull": False}) == "52%"
    assert defs.display_text(meaco, {**states, "onOffState": False}) == "off"
