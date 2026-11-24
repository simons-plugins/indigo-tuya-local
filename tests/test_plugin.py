import logging

import pytest

from conftest import KEY, STATUS, FakeTuya, Forbidden, _FakeDevice, wait_for


class StubLink:
    def __init__(self, connected):
        self.connected = connected
        self.sent = []
        self.refreshes = 0

    def send(self, dps):
        self.sent.append(dps)

    def request_status(self):
        self.refreshes += 1

    def stop(self, timeout=0):
        pass


@pytest.fixture
def setup(make_plugin):
    dev = _FakeDevice()
    p = make_plugin([dev])
    return p, dev


def test_command_while_disconnected_is_rejected_reported_and_state_untouched(setup, action, caplog):
    p, dev = setup
    link = StubLink(connected=False)
    p._links[dev.id] = link
    dev.states["lastError"] = "Device Unreachable"
    p.actionControlDevice(action(device_action="TurnOn"), dev)
    p.actionSetTargetHumidity(action({"targetHumidity": "50"}), dev)
    assert link.sent == [] and dev.writes == []
    assert caplog.text.count("is not connected") == 2 and "Device Unreachable" in caplog.text


def test_commands_translate_and_send_but_do_not_flip_state(setup, action):
    p, dev = setup
    link = p._links[dev.id] = StubLink(True)
    p.actionControlDevice(action(device_action="TurnOn"), dev)
    p.actionSetTargetHumidity(action({"targetHumidity": "60"}), dev)
    p.actionSetPowerLevel(action({"powerLevel": "600w"}), dev)
    p.actionSetIoniser(action({"value": "off"}), dev)
    p.actionSetDataPoint(action({"dp": "103", "value": '"drying"'}), dev)
    assert link.sent == [{"1": True}, {"2": 60}, {"101": "600w"}, {"10": False}, {"103": "drying"}]
    assert dev.writes == []


def test_toggle_uses_last_known_state_only_when_connected(setup, action):
    p, dev = setup
    link = p._links[dev.id] = StubLink(True)
    dev.onState = True
    p.actionControlDevice(action(device_action="Toggle"), dev)
    assert link.sent == [{"1": False}]


@pytest.mark.parametrize("props,needle", [
    ({"targetHumidity": "52"}, "multiple of 5"), ({"targetHumidity": "80"}, "between 35 and 70"),
    ({"targetHumidity": "abc"}, "not a whole number")])
def test_invalid_humidity_sends_nothing(setup, action, caplog, props, needle):
    p, dev = setup
    link = p._links[dev.id] = StubLink(True)
    p.actionSetTargetHumidity(action(props), dev)
    assert link.sent == [] and needle in caplog.text


@pytest.mark.parametrize("props,needle", [({"dp": "x", "value": "1"}, "positive"), ({"dp": "0", "value": "1"}, "positive"),
                                          ({"dp": "2", "value": "{bad"}, "not valid JSON")])
def test_bad_set_data_point_sends_nothing(setup, action, caplog, props, needle):
    p, dev = setup
    link = p._links[dev.id] = StubLink(True)
    p.actionSetDataPoint(action(props), dev)
    assert link.sent == [] and needle in caplog.text


def test_unknown_enum_choice_is_rejected(setup, action, caplog):
    p, dev = setup
    link = p._links[dev.id] = StubLink(True)
    p.actionSetSwing(action({"swing": "sideways"}), dev)
    assert link.sent == [] and "allowed values" in caplog.text


def test_down_writes_states_then_error_state_last_and_up_clears_it(setup):
    p, dev = setup
    p._on_connection(dev.id, False, "Network Error (code 905)")
    assert dev.writes[-1] == ("error", "Network Error (code 905)")
    assert dev.states["connected"] is False and dev.states["lastError"].startswith("Network")
    p._on_connection(dev.id, True, "")
    assert dev.writes[-1] == ("error", None) and dev.states["connected"] is True and dev.states["lastError"] == ""


def test_dps_write_only_changes_and_unknown_dps_lands(setup):
    p, dev = setup
    p._on_dps(dev.id, STATUS)
    assert dev.states["humidity"] == 56 and dev.states["unknownDps"] == '{"103": "drying"}'
    dev.writes.clear()
    p._on_dps(dev.id, STATUS)
    assert dev.writes == []


def test_generic_device_gets_raw_dps_json(make_plugin):
    dev = _FakeDevice(2, type_id="tuyaGeneric")
    p = make_plugin([dev])
    p._on_dps(2, {"2": 1, "1": True})
    assert dev.states["dps"] == '{"1": true, "2": 1}'


def test_missing_definition_marks_device_down_and_starts_no_link(make_plugin):
    dev = _FakeDevice(props={"address": "192.168.1.9", "deviceId": "x", "localKey": KEY,
                             "protocolVersion": "3.5", "definitionId": "gone"})
    p = make_plugin([dev])
    p.deviceStartComm(dev)
    assert dev.id not in p._links and dev.states["connected"] is False and "not found" in dev.error_state


def test_start_stop_comm_with_fake_device_end_to_end(make_plugin, caplog):
    caplog.set_level(logging.DEBUG)
    dev = _FakeDevice()
    p = make_plugin([dev])
    fake = FakeTuya()
    p._device_factory = lambda *a: fake
    p.deviceStartComm(dev)
    assert wait_for(lambda: dev.states.get("connected") is True)
    assert dev.states["humidity"] == 56 and dev.error_state is None
    p.deviceStopComm(dev)
    assert fake.closed and dev.states["connected"] is False and dev.id not in p._links
    assert KEY not in caplog.text


@pytest.mark.parametrize("field,value,msg", [
    ("address", "999.1.1.1", "valid IP"), ("address", "my-host", "valid IP"), ("deviceId", "  ", "device ID"),
    ("localKey", "short", "16 characters"), ("protocolVersion", "3.1", "Choose one")])
def test_config_validation_errors(setup, field, value, msg):
    p, dev = setup
    values = dict(dev.pluginProps)
    values[field] = value
    ok, _, errors = p.validateDeviceConfigUi(values, "tuyaDehumidifier", dev.id)
    assert ok is False and msg in errors[field]
    assert KEY not in str(errors)


def test_config_validation_ok_and_requires_definition_for_dehumidifier(setup):
    p, dev = setup
    assert p.validateDeviceConfigUi(dict(dev.pluginProps), "tuyaDehumidifier", 1)[0] is True
    values = dict(dev.pluginProps, definitionId="")
    assert p.validateDeviceConfigUi(values, "tuyaDehumidifier", 1)[2]["definitionId"]
    values.pop("definitionId")
    assert p.validateDeviceConfigUi(values, "tuyaGeneric", 1)[0] is True


def test_action_validation_and_lists(setup, fake_devices):
    p, dev = setup
    assert p.validateActionConfigUi({"targetHumidity": "52"}, "setTargetHumidity", 1)[2]["targetHumidity"]
    assert p.validateActionConfigUi({"targetHumidity": "55"}, "setTargetHumidity", 1)[0] is True
    assert "dp" in p.validateActionConfigUi({"dp": "q", "value": "1"}, "setDataPoint", 1)[2]
    assert "value" in p.validateActionConfigUi({"dp": "1", "value": "x"}, "setDataPoint", 1)[2]
    assert p.definitionList("dehumidifier")[0][0] == "meaco_dd8l_pro"
    assert [c[0] for c in p.choiceList("powerLevel", None, "", 1)] == ["200w", "400w", "600w", "auto"]


def test_status_request_refreshes_only_when_connected(setup, action, caplog):
    p, dev = setup
    link = p._links[dev.id] = StubLink(False)
    p.actionControlUniversal(action(device_action="RequestStatus"), dev)
    assert link.refreshes == 0 and "not connected" in caplog.text
    link.connected = True
    p.actionControlUniversal(action(device_action="RequestStatus"), dev)
    assert link.refreshes == 1
