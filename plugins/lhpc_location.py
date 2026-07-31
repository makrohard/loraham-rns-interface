# Sideband telemetry plugin: supply a position from a configurable source.
#
# Sideband's own Location sensor only has a GPS backend on Android
# (sense.py: `if is_android(): from plyer import gps`), so on a Linux box it
# never produces a fix. This plugin fills that gap.
#
# It is deliberately SOURCE-AGNOSTIC: the position may come from gpsd, from a
# raw NMEA stream, or from a fixed station position. gpsd is the default because
# it is itself the hardware abstraction — USB, serial, HAT or network GPS all
# look the same through it — but nothing here assumes gpsd is present.
#
# Sideband exec()s this file with the plugin base classes injected and no
# __file__, and it runs inside Sideband's OWN virtualenv, so this module must be
# self-contained: standard library only, no imports from loraham_rns.
#
# Configuration: KEY=value file named by $LHPC_LOCATION_CONF (lhpc sets that in
# the component's run environment and generates the file).

import json
import math
import os
import socket
import threading
import time

DEFAULTS = {
    "location_source": "gpsd",        # gpsd | nmea | fixed | off
    "gpsd_host": "127.0.0.1",
    "gpsd_port": "2947",
    "nmea_device": "/dev/ttyACM0",
    "nmea_baud": "9600",
    "fixed_lat": "",
    "fixed_lon": "",
    "fixed_alt": "",
    "max_age": "30",                  # seconds; older fixes are treated as none
}


def _load_conf():
    conf = dict(DEFAULTS)
    path = os.environ.get("LHPC_LOCATION_CONF", "")
    if path and os.path.isfile(path):
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    conf[key.strip()] = val.strip()
        except OSError:
            pass
    return conf


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_position(lat, lon):
    """A position must be finite and on the planet. A malformed sentence or a bogus
    fixed value would otherwise be published as telemetry — 0,0 or NaN included."""
    if lat is None or lon is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _nmea_checksum_ok(line):
    """NMEA carries `*HH` — an XOR of everything between `$` and `*`. Without this a
    corrupted sentence (common on a noisy serial line) is parsed as a real fix."""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, given = line[1:].rpartition("*")
    given = given.strip()[:2]
    if len(given) != 2:
        return False
    check = 0
    for ch in body:
        check ^= ord(ch)
    try:
        return check == int(given, 16)
    except ValueError:
        return False


class Fix:
    __slots__ = ("lat", "lon", "alt", "speed", "bearing", "accuracy", "at")

    def __init__(self, lat, lon, alt=None, speed=None, bearing=None, accuracy=None):
        self.lat, self.lon, self.alt = lat, lon, alt
        self.speed, self.bearing, self.accuracy = speed, bearing, accuracy
        self.at = time.time()


class Source:
    """Base: a background reader that keeps the most recent fix."""

    def __init__(self, conf):
        self.conf = conf
        self.fix = None
        self._run = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while self._run:
            try:
                self.read_once()
            except Exception:
                time.sleep(2)          # never let a source fault kill telemetry

    def read_once(self):
        time.sleep(1)

    def current(self):
        fix, max_age = self.fix, _f(self.conf["max_age"]) or 30.0
        if fix is None or (time.time() - fix.at) > max_age:
            return None
        return fix

    def stop(self):
        self._run = False


class GpsdSource(Source):
    """gpsd's line-delimited JSON protocol. Plain TCP, no client library."""

    def read_once(self):
        host = self.conf["gpsd_host"]
        port = int(_f(self.conf["gpsd_port"]) or 2947)
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = b""
            sock.settimeout(15)
            while self._run:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if msg.get("class") != "TPV":
                        continue
                    lat, lon = _f(msg.get("lat")), _f(msg.get("lon"))
                    if not _valid_position(lat, lon):
                        continue          # no fix yet, or an implausible one
                    # gpsd reports speed in m/s; Sideband's Location sensor expects
                    # km/h (its own gpsd plugin multiplies by 3.6).
                    spd = _f(msg.get("speed"))
                    self.fix = Fix(lat, lon, _f(msg.get("alt")),
                                   spd * 3.6 if spd is not None else None,
                                   _f(msg.get("track")), _f(msg.get("eph")))


class NmeaSource(Source):
    """Raw NMEA from a character device or FIFO, for boxes without gpsd."""

    @staticmethod
    def _dm_to_deg(value, hemi):
        # NMEA is ddmm.mmmm — degrees and DECIMAL MINUTES, not decimal degrees.
        raw = _f(value)
        if raw is None:
            return None
        degrees = int(raw / 100)
        deg = degrees + (raw - degrees * 100) / 60.0
        return -deg if hemi in ("S", "W") else deg

    @staticmethod
    def _configure_port(fd, baud):
        """Set the line speed and raw 8N1 mode.

        Opening the device as a text file inherits whatever the port was last left in,
        so `nmea_baud` was accepted and then ignored — it only worked if something else
        had already configured the port. A FIFO or plain file has no termios; that is
        not an error, so the ENOTTY case is simply skipped.
        """
        import termios

        speed = getattr(termios, f"B{int(baud)}", None)
        if speed is None:
            raise ValueError(f"unsupported nmea_baud {baud}")
        try:
            attrs = termios.tcgetattr(fd)
        except termios.error:
            return                     # not a tty (FIFO/regular file) — nothing to set
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        iflag &= ~(termios.IXON | termios.IXOFF | termios.IXANY | termios.INLCR
                   | termios.IGNCR | termios.ISTRIP | termios.BRKINT)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHOE | termios.ECHONL
                   | termios.ICANON | termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        cc[termios.VMIN], cc[termios.VTIME] = 0, 10
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])

    def read_once(self):
        with open(self.conf["nmea_device"], "r", errors="replace") as dev:
            self._configure_port(dev.fileno(), self.conf["nmea_baud"])
            while self._run:
                line = dev.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                if not _nmea_checksum_ok(line):
                    continue              # corrupted sentence — not a position
                parts = line.strip().split(",")
                if not parts or parts[0][-3:] not in ("GGA", "RMC"):
                    continue
                try:
                    if parts[0][-3:] == "GGA" and len(parts) > 9:
                        if parts[6] in ("", "0"):
                            continue                      # fix quality 0 = no fix
                        lat = self._dm_to_deg(parts[2], parts[3])
                        lon = self._dm_to_deg(parts[4], parts[5])
                        if _valid_position(lat, lon):
                            self.fix = Fix(lat, lon, _f(parts[9]))
                    elif len(parts) > 8 and parts[2] == "A":  # RMC, A = valid
                        lat = self._dm_to_deg(parts[3], parts[4])
                        lon = self._dm_to_deg(parts[5], parts[6])
                        if _valid_position(lat, lon):
                            # RMC field 7 is speed over ground in KNOTS; Sideband wants
                            # km/h, so 1 kn = 1.852 km/h (not 0.514 m/s).
                            speed = _f(parts[7])
                            self.fix = Fix(lat, lon, None,
                                           speed * 1.852 if speed is not None else None,
                                           _f(parts[8]))
                except (IndexError, ValueError):
                    continue


class FixedSource(Source):
    """A station that does not move. Never stale."""

    def __init__(self, conf):
        self.conf = conf
        lat, lon = _f(conf["fixed_lat"]), _f(conf["fixed_lon"])
        self.fix = Fix(lat, lon, _f(conf["fixed_alt"])) if _valid_position(lat, lon) else None
        self._run = False

    def current(self):
        if self.fix is not None:
            self.fix.at = time.time()      # a fixed position never goes stale
        return self.fix

    def stop(self):
        pass


def make_source(conf):
    kind = (conf.get("location_source") or "off").strip().lower()
    if kind == "gpsd":
        return GpsdSource(conf)
    if kind == "nmea":
        return NmeaSource(conf)
    if kind == "fixed":
        return FixedSource(conf)
    return None


class LhpcLocationPlugin(SidebandTelemetryPlugin):        # noqa: F821 (injected)
    plugin_name = "lhpc_location"

    def start(self):
        self.conf = _load_conf()
        self.source = make_source(self.conf)
        super().start()

    def stop(self):
        if getattr(self, "source", None) is not None:
            self.source.stop()
        super().stop()

    def update_telemetry(self, telemeter):
        if telemeter is None or getattr(self, "source", None) is None:
            return
        fix = self.source.current()
        if fix is None:
            # No CURRENT fix: drop the synthesized sensor instead of returning early.
            # Leaving it in place kept re-reporting the last coordinates, which never
            # went stale — a parked position looks like a live one.
            try:
                telemeter.sensors.pop("location", None)
            except Exception:
                pass
            return
        telemeter.synthesize("location")
        sensor = telemeter.sensors.get("location")
        if sensor is None:
            return
        sensor.latitude = fix.lat
        sensor.longitude = fix.lon
        sensor.altitude = fix.alt
        sensor.speed = fix.speed
        sensor.bearing = fix.bearing
        sensor.accuracy = fix.accuracy
        # Sideband reads _last_update, which is only written by set_update_time();
        # assigning last_update directly left the sensor looking permanently fresh.
        sensor.stale_time = _f(self.conf["max_age"]) or 30.0
        if hasattr(sensor, "set_update_time"):
            sensor.set_update_time(fix.at)
        else:                                    # older Sideband: best effort
            sensor.last_update = fix.at


plugin_class = LhpcLocationPlugin
