"""Shared fixtures. ``indigo`` is a real-class stub, not a MagicMock: a MagicMock base
class turns ``class Plugin(indigo.PluginBase)`` into a MagicMock and every test passes
without running plugin code (see indigo-unifi-protect/tests/conftest.py)."""

import logging
import sys
import threading
import time
import types
from pathlib import Path

import pytest

BUNDLE = Path(__file__).parent.parent / "Tuya Local.indigoPlugin"
SERVER_PLUGIN_DIR = BUNDLE / "Contents" / "Server Plugin"
sys.path.insert(0, str(SERVER_PLUGIN_DIR))

KEY = "0123456789abcdef"
STATUS = {"1": True, "2": 55, "5": "dehumidify", "6": 56, "10": True, "16": False, "17": "cancel",
          "18": 0, "19": 8, "101": "auto", "102": "cancel", "103": "drying", "105": "m_swing"}


class Forbidden(BaseException):
    """Raised by fakes when code does something it must not. BaseException so no
    ``except Exception`` in the code under test can swallow it."""


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class FakeTuya:
    """Stands in for tinytuya.Device. Scripted replies; nothing touches a network."""

    def __init__(self, status=None, receives=None, heartbeats=None, forbid_writes=False):
        self.status_replies = list(status if status is not None else [{"dps": dict(STATUS)}])
        self.receives = list(receives or [])
        self.heartbeats = list(heartbeats or [])
        self.forbid_writes = forbid_writes
        self.written = []
        self.closed = False
        self.socket = None
        self.status_calls = 0
        self.write_reply = None
        self.lock = threading.Lock()

    def set_socketTimeout(self, s):
        pass

    def _next(self, seq, default=None):
        with self.lock:
            return seq.pop(0) if seq else default

    def status(self, nowait=False):
        self.status_calls += 1
        if len(self.status_replies) > 1:
            return self._next(self.status_replies)
        return self.status_replies[0] if self.status_replies else None

    def heartbeat(self, nowait=True):
        return self._next(self.heartbeats)

    def receive(self):
        item = self._next(self.receives, "idle")
        if item == "idle":
            time.sleep(0.01)
            return None
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        if callable(item):
            return item(self)
        return item

    def _write(self, payload):
        if self.forbid_writes:
            raise Forbidden(f"write attempted: {payload}")
        self.written.append(payload)
        return self.write_reply if self.write_reply is not None else {"dps": dict(payload)}

    def set_value(self, dp, value, nowait=False):
        return self._write({str(dp): value})

    def set_multiple_values(self, data, nowait=False):
        return self._write(dict(data))

    def close(self):
        self.closed = True


class Recorder:
    def __init__(self):
        self.dps, self.conn, self.cmd_err = [], [], []

    def on_dps(self, dps):
        self.dps.append(dict(dps))

    def on_connection(self, ok, reason):
        self.conn.append((ok, reason))

    def on_command_error(self, dps, reason):
        self.cmd_err.append((dps, reason))


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def make_link(recorder):
    from tuya_link import LinkTimings, TuyaLink
    links = []

    def _make(devices, key=KEY, **timing_overrides):
        seq = list(devices)
        calls = []

        def factory(dev_id, address, local_key, version):
            calls.append((dev_id, address, version))
            if not seq:
                raise Forbidden("factory called again after it must not be")
            item = seq.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        t = dict(heartbeat_interval=0.05, status_interval=0.5, backoff_initial=0.02, backoff_max=0.05,
                 key_error_backoff=30.0, idle_pause=0.001)
        t.update(timing_overrides)
        link = TuyaLink("Test", "192.168.1.9", "devid", key, "3.5", recorder.on_dps, recorder.on_connection,
                        recorder.on_command_error, device_factory=factory, timings=LinkTimings(**t))
        link.factory_calls = calls
        links.append(link)
        return link

    yield _make
    for link in links:
        link.stop(2)


# ---------------------------------------------------------------- fake indigo
class _FakeDevice:
    def __init__(self, dev_id=1, name="Dehumidifier", type_id="tuyaDehumidifier", props=None):
        self.id, self.name, self.deviceTypeId = dev_id, name, type_id
        self.pluginProps = dict(props or {"address": "192.168.1.9", "deviceId": "x", "localKey": KEY,
                                          "protocolVersion": "3.5", "definitionId": "meaco_dd8l_pro"})
        self.states = {}
        self.writes = []
        self.error_state = "unset"
        self.onState = False

    def updateStatesOnServer(self, states):
        self.writes.append(("states", [dict(s) for s in states]))
        for s in states:
            self.states[s["key"]] = s["value"]
        self.error_state = None  # real Indigo clears the error state on a state write (clearErrorState default)

    def setErrorStateOnServer(self, text):
        self.writes.append(("error", text))
        self.error_state = text

    def stateListOrDisplayStateIdChanged(self):
        pass


class _FakeDevices:
    def __init__(self):
        self.d = {}

    def __getitem__(self, key):
        return self.d[key]

    def __contains__(self, key):
        return key in self.d


class _PluginBase:
    class StopThread(Exception):
        pass

    def __init__(self, plugin_id, display, version, prefs):
        self.pluginId, self.pluginDisplayName, self.pluginVersion, self.pluginPrefs = plugin_id, display, version, prefs
        self.logger = logging.getLogger("Plugin")
        self.pluginFolderPath = str(BUNDLE)

    def deviceStartComm(self, dev):
        pass

    def deviceStopComm(self, dev):
        pass


class _Enum:
    TurnOn, TurnOff, Toggle, RequestStatus = "TurnOn", "TurnOff", "Toggle", "RequestStatus"


def _install():
    fake = types.ModuleType("indigo")
    fake.PluginBase = _PluginBase
    fake.Dict = dict
    fake.List = list
    fake.devices = _FakeDevices()
    fake.kDeviceAction = _Enum
    fake.kUniversalAction = _Enum
    sys.modules["indigo"] = fake
    return fake


_install()


class _Action:
    def __init__(self, props=None, device_action=None):
        self.props = props or {}
        self.deviceAction = device_action


@pytest.fixture
def action():
    return _Action


@pytest.fixture
def fake_devices():
    sys.modules["indigo"].devices = _FakeDevices()
    return sys.modules["indigo"].devices


@pytest.fixture
def make_plugin(fake_devices):
    import plugin as plugin_module

    created = []

    def _make(devices=None, timings=None):
        p = plugin_module.Plugin("com.simons-plugins.indigo-tuya-local", "Tuya Local", "1", {})
        p.startup()
        for dev in (devices or []):
            fake_devices.d[dev.id] = dev
        created.append(p)
        return p

    yield _make
    for p in created:
        p.shutdown()
