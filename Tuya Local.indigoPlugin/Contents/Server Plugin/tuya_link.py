"""One connection worker per Tuya device. No Indigo imports.

Modelled on make-all/tuya-local: one persistent socket, a full status() on
connect, pushed frames via receive(), a heartbeat every ~10 s, a full status()
every ~30 s. tinytuya reports failures as dicts ({"Err": "9xx", ...}) rather
than exceptions, so every reply goes through ``_process`` which treats both.

Live-verified (Meaco DD8L Pro, protocol 3.5, tinytuya 1.21.1): ANY call can
return a pushed status frame instead of its own reply (heartbeat() returned a
humidity change), and a set_value() reply is the device's echo with the new DP
in top-level ``dps``. Hence one handler for every reply: a ``dps`` dict is
merged into the cache and reported; ``Err`` is a failure; nothing else is
inspected. A status() reply is full, pushes/echoes are partial: always merge,
never replace.

Error 914 (wrong key or version) backs off for a long fixed time and never
retries sooner or with other versions/keys: repeated failed 3.4/3.5 handshakes
can lock a device's LAN access until it is power-cycled.

The local key is never logged; every string leaving this module is redacted.
"""

import logging
import queue
import threading
import time

ERR_PAYLOAD = "904"
ERR_KEY_OR_VER = "914"
# Link-level tinytuya errors that mean the session is unusable.
_LINK_ERRORS = {"901", "902", "905", "906", "914", "904"}
_HINTS = {
    "904": " - unexpected payload; check the local key and protocol version",
    "914": " - local key or protocol version rejected",
}


class LinkDownError(ConnectionError):
    """A command was refused because the link is not connected."""


class _Failure(Exception):
    def __init__(self, reason, key_error=False):
        super().__init__(reason)
        self.reason = reason
        self.key_error = key_error


class LinkTimings:
    """All waits, injectable so tests run in milliseconds."""

    def __init__(self, heartbeat_interval=10.0, status_interval=30.0, backoff_initial=5.0,
                 backoff_max=60.0, key_error_backoff=600.0, op_timeout=3.0, idle_timeout=1.0,
                 idle_pause=0.05):
        self.heartbeat_interval = heartbeat_interval
        self.status_interval = status_interval
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.key_error_backoff = key_error_backoff
        self.op_timeout = op_timeout      # connect / status / command
        self.idle_timeout = idle_timeout  # receive() block, bounds command latency
        self.idle_pause = idle_pause      # stops a hot spin if receive() returns instantly


def quiet_tinytuya():
    """tinytuya logs the local key and session key at DEBUG. Keep it at WARNING+, always."""
    logging.getLogger("tinytuya").setLevel(logging.WARNING)


def default_device_factory(device_id, address, local_key, version):
    """Build a tinytuya device. Constructing it opens no socket."""
    import tinytuya  # imported lazily: Indigo installs it from requirements.txt
    quiet_tinytuya()
    dev = tinytuya.Device(device_id, address, local_key, version=float(version))
    dev.set_socketPersistent(True)
    dev.set_socketRetryLimit(1)  # never let tinytuya sleep 5 s in-line between retries
    dev.set_socketTimeout(3.0)
    return dev


class TuyaLink(threading.Thread):
    def __init__(self, name, address, device_id, local_key, version, on_dps, on_connection,
                 on_command_error=None, device_factory=default_device_factory, timings=None,
                 logger=None, clock=time.monotonic):
        super().__init__(name=f"TuyaLink-{name}", daemon=True)
        self._label = name
        self._address, self._device_id, self._local_key, self._version = address, device_id, local_key, version
        self._on_dps, self._on_connection, self._on_command_error = on_dps, on_connection, on_command_error
        self._factory = device_factory
        self._t = timings or LinkTimings()
        self._log = logger or logging.getLogger("Plugin")
        self._clock = clock
        self._halt = threading.Event()
        self._commands = queue.Queue()
        self._cache = {}
        self._connected = False
        self._last_report = None
        self._last_logged = None
        self._refresh = threading.Event()
        self._session_was_up = False

    # ------------------------------------------------------------------ public
    @property
    def connected(self):
        return self._connected

    def send(self, dps):
        """Queue one frame. Raises LinkDownError (and queues nothing) if not connected."""
        if not dps:
            raise ValueError("no data points to send")
        if not self._connected:
            raise LinkDownError(f"{self._label} is not connected")
        self._commands.put({str(k): v for k, v in dps.items()})

    def request_status(self):
        """Ask for a full status() on the next loop turn."""
        self._refresh.set()

    def stop(self, timeout=10.0):
        self._halt.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    # --------------------------------------------------------------- internals
    def _redact(self, text):
        text = str(text)
        return text.replace(self._local_key, "****") if self._local_key else text

    def _callback(self, fn, *args):
        if self._halt.is_set() or fn is None:
            return
        try:
            fn(*args)
        except Exception as exc:  # a broken callback must not kill the link
            self._log.error(f"{self._label}: callback failed: {self._redact(exc)}")

    def _report(self, connected, reason):
        self._connected = connected
        if self._last_report != (connected, reason):
            self._last_report = (connected, reason)
            self._callback(self._on_connection, connected, reason)

    def _emit(self):
        self._callback(self._on_dps, dict(self._cache))

    def _process(self, result, what, allow_none=True, need_dps=False):
        """The single place every reply goes through. Returns True if it carried DPs."""
        if result is None:
            if need_dps or not allow_none:
                raise _Failure(f"{what}: no response")
            return False
        if not isinstance(result, dict):
            raise _Failure(f"{what}: unexpected reply type {type(result).__name__}")
        if "Err" in result or "Error" in result:
            code = str(result.get("Err", ""))
            text = self._redact(result.get("Error", "error"))
            raise _Failure(f"{what}: {text} (code {code}){_HINTS.get(code, '')}",
                           key_error=(code == ERR_KEY_OR_VER))
        dps = result.get("dps")
        if isinstance(dps, dict) and dps:
            self._cache.update({str(k): v for k, v in dps.items()})
            self._emit()
            return True
        if need_dps:
            raise _Failure(f"{what}: reply had no data points")
        return False

    def _call(self, what, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            raise _Failure(f"{what}: {type(exc).__name__}: {self._redact(exc)}") from None

    def _timeout(self, dev, seconds):
        self._call("set timeout", dev.set_socketTimeout, seconds)

    def _drain(self, reason):
        while True:
            try:
                dps = self._commands.get_nowait()
            except queue.Empty:
                return
            self._callback(self._on_command_error, dps, reason)

    def _run_command(self, dev, dps):
        self._timeout(dev, self._t.op_timeout)
        if len(dps) == 1:
            (dp, value), = dps.items()
            result = self._call("command", dev.set_value, dp, value)
        else:
            result = self._call("command", dev.set_multiple_values, dps)
        try:
            self._process(result, "command")
        except _Failure as fail:
            code_text = fail.reason
            if fail.key_error or any(f"(code {c})" in code_text for c in _LINK_ERRORS):
                self._callback(self._on_command_error, dps, fail.reason)
                raise
            self._callback(self._on_command_error, dps, fail.reason)  # device refused; link still fine

    def _session(self):
        self._drain("link was down")
        self._session_was_up = False
        dev = self._call("connect", self._factory, self._device_id, self._address,
                                           self._local_key, self._version)
        try:
            self._timeout(dev, self._t.op_timeout)
            self._process(self._call("status", dev.status), "status", need_dps=True)
            self._session_was_up = True
            self._report(True, "")
            now = self._clock()
            next_hb, next_status = now + self._t.heartbeat_interval, now + self._t.status_interval
            sock = getattr(dev, "socket", None)
            while not self._halt.is_set():
                try:
                    dps = self._commands.get_nowait()
                except queue.Empty:
                    dps = None
                if dps is not None:
                    self._run_command(dev, dps)
                now = self._clock()
                if now >= next_status or self._refresh.is_set():
                    self._refresh.clear()
                    self._timeout(dev, self._t.op_timeout)
                    self._process(self._call("status", dev.status), "status", need_dps=True)
                    next_status = self._clock() + self._t.status_interval
                    next_hb = self._clock() + self._t.heartbeat_interval
                elif now >= next_hb:
                    self._timeout(dev, self._t.op_timeout)
                    self._process(self._call("heartbeat", dev.heartbeat, False), "heartbeat")
                    next_hb = self._clock() + self._t.heartbeat_interval
                if dps is not None:
                    continue
                self._timeout(dev, self._t.idle_timeout)
                result = self._call("receive", dev.receive)
                if isinstance(result, dict) and str(result.get("Err")) == ERR_PAYLOAD:
                    result = None  # 3.4/3.5 devices answer an idle receive() with 904 (tuya-local does the same)
                self._process(result, "receive")
                new_sock = getattr(dev, "socket", None)
                if sock is not None and new_sock is not None and new_sock is not sock:
                    next_status = 0  # tinytuya silently reconnected inside a call: re-read everything
                sock = new_sock if new_sock is not None else sock
                if result is None:
                    self._halt.wait(self._t.idle_pause)
        finally:
            try:
                dev.close()
            except Exception:  # best effort teardown
                pass

    def run(self):
        delay = self._t.backoff_initial
        while not self._halt.is_set():
            failure = None
            try:
                self._session()
            except _Failure as fail:
                failure = fail
            except Exception as exc:
                failure = _Failure(f"unexpected {type(exc).__name__}: {self._redact(exc)}")
            if self._halt.is_set():
                break
            self._drain("link dropped before the command was sent")
            if self._session_was_up:
                delay = self._t.backoff_initial
            wait = self._t.key_error_backoff if failure.key_error else delay
            if not failure.key_error:
                delay = min(delay * 2, self._t.backoff_max)
            self._report(False, failure.reason)
            if self._last_logged != failure.reason:
                self._last_logged = failure.reason
                self._log.warning(f"{self._label}: link down: {failure.reason}. Retrying in {wait:g}s"
                                  + (" (not sooner: repeated failed handshakes can lock the device)"
                                     if failure.key_error else ""))
            else:
                self._log.debug(f"{self._label}: still down: {failure.reason}")
            self._halt.wait(wait)
