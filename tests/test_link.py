"""Link behaviour against a fake tinytuya device. The recurring question: when could
this report something that looks fine and be wrong?"""

import logging
import time

import pytest

from conftest import KEY, STATUS, FakeTuya, Forbidden, wait_for
from tuya_link import LinkDownError


def up(rec):
    return any(ok for ok, _ in rec.conn)


def test_happy_path_reports_full_status_then_connected(make_link, recorder):
    link = make_link([FakeTuya()])
    link.start()
    assert wait_for(lambda: up(recorder))
    assert recorder.dps[0] == STATUS  # 13 DPs, string keys, reported before "connected"
    assert link.connected


@pytest.mark.parametrize("reply", [{"Error": "Network Error: Device Unreachable", "Err": "905", "Payload": None},
                                   None, {}, {"dps": {}}])
def test_bad_status_is_failure_not_an_empty_valid_state(make_link, recorder, reply):
    link = make_link([FakeTuya(status=[reply]), FakeTuya(status=[reply])] * 20, backoff_initial=0.01)
    link.start()
    assert wait_for(lambda: recorder.conn)
    assert recorder.conn[0][0] is False
    assert not up(recorder) and recorder.dps == [] and not link.connected


def test_drop_mid_session_reports_down_then_rereads_full_status(make_link, recorder):
    first = FakeTuya(receives=[{"Error": "Network Error: Unable to Connect", "Err": "901", "Payload": None}])
    second = FakeTuya()
    link = make_link([first, second])
    link.start()
    assert wait_for(lambda: [c[0] for c in recorder.conn][:3] == [True, False, True])
    assert first.closed and second.status_calls >= 1
    down_index = [c[0] for c in recorder.conn].index(False)
    assert "901" in recorder.conn[down_index][1]


def test_no_dps_reported_while_down(make_link, recorder):
    first = FakeTuya(receives=[{"Err": "901", "Error": "x"}])
    bad = {"Err": "905", "Error": "Network Error: Device Unreachable"}
    devs = [first] + [FakeTuya(status=[bad]) for _ in range(500)]
    link = make_link(devs, backoff_initial=0.01)
    link.start()
    assert wait_for(lambda: [c[0] for c in recorder.conn][:2] == [True, False])
    n = len(recorder.dps)
    time.sleep(0.15)
    assert len(recorder.dps) == n and not link.connected


def test_error_914_one_attempt_then_nothing_inside_the_backoff(make_link, recorder):
    bad = FakeTuya(status=[{"Error": "Check device key or version", "Err": "914", "Payload": None}])
    link = make_link([bad], key_error_backoff=30.0, backoff_initial=0.01)  # 2nd factory call raises Forbidden
    link.start()
    assert wait_for(lambda: recorder.conn)
    time.sleep(0.3)  # 30 normal-backoff periods: any early retry would hit the fatal factory
    assert len(link.factory_calls) == 1
    assert bad.status_calls == 1 and link.is_alive()
    assert "914" in recorder.conn[0][1] and not link.connected
    t0 = time.monotonic()
    link.stop()
    assert time.monotonic() - t0 < 1 and not link.is_alive()


def test_914_during_a_live_session_also_gets_the_long_backoff(make_link, recorder):
    dev = FakeTuya(receives=[{"Error": "Check device key or version", "Err": "914"}])
    link = make_link([dev], key_error_backoff=30.0)
    link.start()
    assert wait_for(lambda: [c[0] for c in recorder.conn][:2] == [True, False])
    time.sleep(0.2)
    assert len(link.factory_calls) == 1


def test_partial_push_merges_and_never_wipes(make_link, recorder):
    link = make_link([FakeTuya(receives=[{"dps": {"6": 54}}])])
    link.start()
    assert wait_for(lambda: len(recorder.dps) >= 2)
    assert recorder.dps[1]["6"] == 54 and recorder.dps[1]["2"] == 55 and len(recorder.dps[1]) == 13


def test_heartbeat_reply_carrying_dps_is_merged_not_a_failure(make_link, recorder):
    dev = FakeTuya(heartbeats=[{"protocol": 4, "data": {"dps": {"6": 55}}, "dps": {"6": 55}}])
    link = make_link([dev])
    link.start()
    assert wait_for(lambda: any(d.get("6") == 55 for d in recorder.dps))
    assert link.connected and [c[0] for c in recorder.conn] == [True]


def test_heartbeat_error_is_a_failure(make_link, recorder):
    dev = FakeTuya(heartbeats=[{"Err": "902", "Error": "Timeout Waiting for Device"}])
    link = make_link([dev, FakeTuya()])
    link.start()
    assert wait_for(lambda: [c[0] for c in recorder.conn][:2] == [True, False])
    assert "902" in recorder.conn[1][1]


def test_command_echo_is_merged_and_written_as_one_frame(make_link, recorder):
    dev = FakeTuya()
    link = make_link([dev])
    link.start()
    assert wait_for(lambda: link.connected)
    link.send({"2": 60})
    link.send({"1": True, "10": False})
    assert wait_for(lambda: len(dev.written) == 2)
    assert dev.written == [{"2": 60}, {"1": True, "10": False}]
    assert wait_for(lambda: any(d.get("2") == 60 for d in recorder.dps))


def test_command_refused_by_device_keeps_link_but_is_reported(make_link, recorder):
    dev = FakeTuya()
    dev.write_reply = {"Err": "903", "Error": "Specified Value Out of Range"}
    link = make_link([dev])
    link.start()
    assert wait_for(lambda: link.connected)
    link.send({"2": 60})
    assert wait_for(lambda: recorder.cmd_err)
    assert "903" in recorder.cmd_err[0][1] and link.connected


def test_command_link_error_drops_the_link(make_link, recorder):
    dev = FakeTuya()
    dev.write_reply = {"Err": "902", "Error": "Timeout Waiting for Device"}
    link = make_link([dev, FakeTuya()])
    link.start()
    assert wait_for(lambda: link.connected)
    link.send({"1": False})
    assert wait_for(lambda: False in [c[0] for c in recorder.conn])
    assert recorder.cmd_err


def test_send_while_disconnected_is_rejected_and_writes_nothing(make_link):
    dev = FakeTuya(forbid_writes=True)
    link = make_link([dev])  # never started: not connected
    with pytest.raises(LinkDownError):
        link.send({"1": True})
    assert dev.written == [] and link._commands.empty()


def test_empty_command_rejected(make_link):
    with pytest.raises(ValueError):
        make_link([]).send({})


def test_queued_command_is_not_replayed_after_reconnect(make_link, recorder):
    first = FakeTuya(receives=[lambda d: time.sleep(0.05) or {"Err": "901", "Error": "x"}])
    second = FakeTuya(forbid_writes=True)  # a replayed stale command would hit this
    link = make_link([first, second])
    link.start()
    assert wait_for(lambda: link.connected)
    link._commands.put({"1": False})  # simulate a command queued in the instant before the drop
    assert wait_for(lambda: len([c for c in recorder.conn if c[0]]) >= 2)
    time.sleep(0.1)
    assert second.written == [] and link.is_alive()


def test_stop_joins_quickly_while_in_backoff(make_link, recorder):
    bad = {"Err": "905", "Error": "Network Error: Device Unreachable"}
    link = make_link([FakeTuya(status=[bad])], backoff_initial=30.0, backoff_max=30.0)
    link.start()
    assert wait_for(lambda: recorder.conn)
    t0 = time.monotonic()
    link.stop()
    assert time.monotonic() - t0 < 1 and not link.is_alive()


def test_stop_closes_the_device_and_emits_nothing_more(make_link, recorder):
    dev = FakeTuya()
    link = make_link([dev])
    link.start()
    assert wait_for(lambda: link.connected)
    link.stop()
    n = len(recorder.conn)
    assert dev.closed and len(recorder.conn) == n


def test_receive_904_is_idle_noise_but_status_904_is_failure(make_link, recorder):
    noisy = FakeTuya(receives=[{"Err": "904", "Error": "Unexpected Payload from Device"}] * 3)
    link = make_link([noisy])
    link.start()
    assert wait_for(lambda: link.connected)
    time.sleep(0.1)
    assert [c[0] for c in recorder.conn] == [True]
    bad = FakeTuya(status=[{"Err": "904", "Error": "Unexpected Payload from Device"}])
    link2 = make_link([bad, bad, bad, bad, bad], backoff_initial=0.01)
    link2.start()
    assert wait_for(lambda: len(recorder.conn) >= 2 and recorder.conn[-1][0] is False)
    assert "key" in recorder.conn[-1][1]


def test_silent_socket_swap_inside_tinytuya_forces_a_full_status(make_link, recorder):
    dev = FakeTuya()
    dev.socket = object()

    def swap(d):
        d.socket = object()
        return None

    dev.receives = [swap]
    link = make_link([dev], status_interval=30.0)
    link.start()
    assert wait_for(lambda: dev.status_calls >= 2)


def test_key_never_appears_in_logs_or_reasons(make_link, recorder, caplog):
    caplog.set_level(logging.DEBUG)
    boom = RuntimeError(f"cannot connect with key {KEY}")
    payload = {"Err": "904", "Error": f"Unexpected Payload from Device {KEY}", "Payload": KEY}
    devs = [boom, FakeTuya(status=[payload]), FakeTuya(status=[{"Err": "914", "Error": f"bad {KEY}"}])]
    link = make_link(devs, backoff_initial=0.01)
    link.start()
    assert wait_for(lambda: len(link.factory_calls) == 3 and len(recorder.conn) >= 3)
    assert KEY not in caplog.text
    assert all(KEY not in reason for _, reason in recorder.conn)
    assert "****" in recorder.conn[0][1]


def test_callback_exception_does_not_kill_the_link(make_link, recorder):
    link = make_link([FakeTuya()])
    link._on_dps = lambda d: (_ for _ in ()).throw(RuntimeError("boom"))
    link.start()
    assert wait_for(lambda: link.connected)


def test_real_tinytuya_factory_configures_without_touching_the_network():
    import tinytuya  # bundled in Server Plugin/
    from tuya_link import default_device_factory
    dev = default_device_factory("devid", "192.0.2.1", KEY, "3.5")  # TEST-NET, and construction opens no socket
    assert dev.socketPersistent is True and dev.socketRetryLimit == 1
    assert dev.version == 3.5 and dev.socket is None
    assert logging.getLogger("tinytuya").level == logging.WARNING
