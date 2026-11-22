"""The Sideband location plugin: speeds in km/h (what Sideband's Location sensor expects; its
own gpsd plugin multiplies m/s by 3.6), and a fix that is not current is removed from the
telemetry instead of being re-reported. Sideband exec()s the file with its plugin base class
injected, so the module is loaded the same way, from its file."""

import builtins
import importlib.util
import json
import pathlib
import types

import pytest

PLUGIN = pathlib.Path(__file__).parents[2] / "plugins" / "lhpc_location.py"


@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setattr(builtins, "SidebandTelemetryPlugin",
                        type("S", (), {"start": lambda self: None, "stop": lambda self: None}),
                        raising=False)
    spec = importlib.util.spec_from_file_location("lhpc_location", PLUGIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _nmea(body):
    check = 0
    for ch in body:
        check ^= ord(ch)
    return f"${body}*{check:02X}\n"


def test_nmea_knots_become_kmh(plugin, tmp_path, monkeypatch):
    """RMC field 7 is speed over ground in knots: 10 kn is 18.52 km/h (it was once turned
    into m/s, 3.6 x 1.852 too small)."""
    dev = tmp_path / "nmea"
    dev.write_text(_nmea("GPRMC,123519,A,4807.038,N,01131.000,E,010.0,084.4,230394,003.1,W"))
    src = plugin.NmeaSource.__new__(plugin.NmeaSource)      # the reader without its thread
    src.conf, src.fix, src._run = {"nmea_device": str(dev), "nmea_baud": "9600"}, None, True
    # at the end of the file the reader waits for more: stop it there
    monkeypatch.setattr(plugin, "time", types.SimpleNamespace(
        time=plugin.time.time, sleep=lambda _s: setattr(src, "_run", False)))
    src.read_once()
    assert src.fix.speed == pytest.approx(18.52)
    assert (round(src.fix.lat, 4), round(src.fix.lon, 4)) == (48.1173, 11.5167)


def test_gpsd_metres_per_second_become_kmh(plugin, monkeypatch):
    """gpsd's TPV speed is m/s: 10 m/s is 36 km/h (it was once passed through unchanged)."""
    tpv = json.dumps({"class": "TPV", "lat": 48.1, "lon": 11.5, "speed": 10.0}).encode() + b"\n"

    class Sock:
        def __init__(self):
            self.chunks = [tpv, b""]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def sendall(self, data):
            pass

        def settimeout(self, t):
            pass

        def recv(self, n):
            return self.chunks.pop(0)

    monkeypatch.setattr(plugin, "socket", types.SimpleNamespace(create_connection=lambda *a, **k: Sock()))
    src = plugin.GpsdSource.__new__(plugin.GpsdSource)      # the reader without its thread
    src.conf, src.fix, src._run = {"gpsd_host": "127.0.0.1", "gpsd_port": "2947"}, None, True
    src.read_once()
    assert src.fix.speed == pytest.approx(36.0)


class _Sensor:
    def __init__(self):
        self.updated = []

    def set_update_time(self, at):
        self.updated.append(at)


class _Telemeter:
    def __init__(self):
        self.sensors = {"location": _Sensor()}

    def synthesize(self, name):
        self.sensors.setdefault(name, _Sensor())


def _started(plugin, tmp_path, monkeypatch, **conf):
    path = tmp_path / "location.conf"
    path.write_text("".join(f"{k}={v}\n" for k, v in conf.items()))
    monkeypatch.setenv("LHPC_LOCATION_CONF", str(path))
    p = plugin.LhpcLocationPlugin()
    p.start()
    return p


def test_a_fix_that_is_not_current_is_removed_not_reused(plugin, tmp_path, monkeypatch):
    """Returning early on a missing fix left the previous coordinates in the sensor: a parked
    position kept looking live."""
    p = _started(plugin, tmp_path, monkeypatch, location_source="fixed", fixed_lat="", fixed_lon="")
    telemeter = _Telemeter()
    p.update_telemetry(telemeter)
    assert "location" not in telemeter.sensors


def test_a_current_fix_goes_through_set_update_time(plugin, tmp_path, monkeypatch):
    """Sideband reads the sensor's freshness from set_update_time(); assigning last_update
    left the sensor looking fresh for ever. stale_time is the configured max_age."""
    p = _started(plugin, tmp_path, monkeypatch, location_source="fixed", fixed_lat="48.1",
                 fixed_lon="11.5", max_age="45")
    telemeter = _Telemeter()
    p.update_telemetry(telemeter)
    sensor = telemeter.sensors["location"]
    assert (sensor.latitude, sensor.longitude, sensor.stale_time) == (48.1, 11.5, 45.0)
    assert sensor.updated == [p.source.fix.at]
