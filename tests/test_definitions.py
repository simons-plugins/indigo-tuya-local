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
