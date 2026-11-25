import xml.etree.ElementTree as ET

import definitions as defs
from conftest import SERVER_PLUGIN_DIR

DEFN_DIR = SERVER_PLUGIN_DIR / "definitions"


def parse(name):
    return ET.parse(SERVER_PLUGIN_DIR / name).getroot()


def device_states(type_id):
    for dev in parse("Devices.xml").findall("Device"):
        if dev.get("id") == type_id:
            return [s.get("id") for s in dev.find("States").findall("State")]
    raise AssertionError(type_id)


def test_all_xml_parses():
    for name in ("Devices.xml", "Actions.xml", "PluginConfig.xml"):
        parse(name)


def test_state_ids_are_camel_case_ascii_no_underscores():
    for dev in parse("Devices.xml").findall("Device"):
        ids = [s.get("id") for s in dev.find("States").findall("State")]
        assert len(ids) == len(set(ids))
        for sid in ids:
            assert defs.is_valid_state_id(sid) and "_" not in sid, sid


def test_definition_states_and_xml_agree_both_ways():
    found, errors = defs.load_definitions(DEFN_DIR)
    assert errors == []
    xml_states = set(device_states("tuyaDehumidifier")) - {"connected", "lastError"}
    for defn in found.values():
        mapped = set(defn.state_ids()) - {"onOffState"}  # onOffState is the relay's built-in state
        assert mapped == xml_states, (mapped ^ xml_states)


def test_device_types_and_filters():
    types = {d.get("id"): d.get("type") for d in parse("Devices.xml").findall("Device")}
    assert types == {"tuyaDehumidifier": "relay", "tuyaGeneric": "custom"}
    assert {"connected", "lastError", "dps"} == set(device_states("tuyaGeneric"))
    for act in parse("Actions.xml").findall("Action"):
        assert act.get("deviceFilter", "").startswith("self"), act.get("id")


def test_action_callbacks_exist_on_plugin():
    import plugin
    for act in parse("Actions.xml").findall("Action"):
        assert hasattr(plugin.Plugin, act.find("CallbackMethod").text)


def test_requirements_and_plist():
    # No requirements.txt on purpose: pip would rebuild cryptography from source (Rust) on
    # Intel Macs. tinytuya is bundled; cryptography comes from Indigo.
    assert not (SERVER_PLUGIN_DIR / "requirements.txt").exists()
    assert "version_tuple = (1, 21, 1)" in (SERVER_PLUGIN_DIR / "tinytuya" / "core" / "core.py").read_text()
    assert (SERVER_PLUGIN_DIR / "tinytuya" / "LICENSE").exists()
    plist = (SERVER_PLUGIN_DIR.parent / "Info.plist").read_text()
    for needle in ("com.simons-plugins.indigo-tuya-local", "2026.2.0", "<string>3.4</string>", "indigo-tuya-local"):
        assert needle in plist
