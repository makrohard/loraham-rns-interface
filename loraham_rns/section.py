"""The daemon interface's section: every key with its range and default, checked once
(docs/daemon-interface.md, "The section"), and the CONF lines it becomes.

The interface's constructor and the pre-check CLI run this same code, so a controller that
checks a section first never sees the interface refuse it for its values.
"""

from dataclasses import dataclass

from RNS.vendor.configobj import ConfigObj, ConfigObjError, DuplicateError

from .framing import rnode_preamble
from .interface import BAND_POLICY, duty_ceiling
from .rflog import parse_switch

# Hz -> the daemon's BW spelling in kHz (conf-protocol.md, CONFIG parameters).
BANDWIDTHS = {7800: "7.8", 10400: "10.4", 15600: "15.6", 20800: "20.8", 31250: "31.25",
              41700: "41.7", 62500: "62.5", 125000: "125", 250000: "250", 500000: "500"}
# The controller's own defaults for the hourly limit; never above the channel's ceiling.
LONG_LIMIT_DEFAULT = {"433": 10.0, "868": 1.0}
MIN_TXPOWER = 2                    # the lowest POWER both chip families accept


class SectionError(ValueError):
    def __init__(self, key, message):
        super().__init__(f"{key}: {message}")
        self.key = key


@dataclass(frozen=True)
class Settings:
    band: str
    conf_socket: str
    data_socket: str
    frequency: int
    bandwidth: int
    sf: int
    cr: int
    txpower: int
    preamble: int
    syncword: int
    crc: bool
    ldro: str
    txmode: str
    cadidle: int
    cadwait: int
    cadrssi: int
    cadmonitor: bool
    cadtxaftertimeout: bool
    rnode_framing: bool
    airtime_short: float
    airtime_long: float
    state_dir: str
    rf_log_path: str | None
    ifac: bool


def _text(c, key):
    value = c.get(key)
    return None if value is None else str(value).strip()


def _int(c, key, default, lo, hi, base=10):
    text = _text(c, key)
    if not text:
        return default
    try:
        value = int(text, base)
    except ValueError:
        raise SectionError(key, f"{text!r} is not a whole number") from None
    if not lo <= value <= hi:
        raise SectionError(key, f"{value} is outside {lo}-{hi}")
    return value


def _switch(c, key, default):
    text = _text(c, key)
    if not text:
        return default
    try:
        return parse_switch(text, key)
    except ValueError as exc:
        raise SectionError(key, str(exc)) from None


def _percent(c, key, default, limit):
    text = _text(c, key)
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise SectionError(key, f"{text!r} is not a number") from None
    if not 0 < value <= limit:
        raise SectionError(key, f"{value} is outside 0-{limit}")
    return value


def _path(c, key):
    text = _text(c, key)
    if not text:
        raise SectionError(key, "is required")
    if not text.startswith("/"):
        raise SectionError(key, f"{text!r} is not an absolute path")
    return text


def check(c):
    """The Settings of section `c` (a mapping of key -> value), or SectionError naming the key."""
    band = _text(c, "band")
    if band not in BAND_POLICY:
        raise SectionError("band", f"{band!r} is not 433 or 868")
    if not _text(c, "frequency"):
        raise SectionError("frequency", "is required")
    frequency = _int(c, "frequency", None, 1, 10**10)
    bandwidth = _int(c, "bandwidth", 125000, 1, 10**7)
    if bandwidth not in BANDWIDTHS:
        raise SectionError("bandwidth", f"{bandwidth} is not one of {', '.join(map(str, BANDWIDTHS))} Hz")
    ceiling = duty_ceiling(band, frequency, bandwidth)
    if ceiling is None:
        raise SectionError("frequency", f"{frequency / 1e6:.3f} MHz with {bandwidth / 1e3:g} kHz is not inside "
                                        f"a permitted {band} MHz segment")
    cap = BAND_POLICY[band][2]
    sf = _int(c, "spreadingfactor", 8, 7, 12)
    cr = _int(c, "codingrate", 5, 5, 8)
    rnode_framing = _switch(c, "rnode_framing", False)
    ldro = (_text(c, "ldro") or "auto").upper()
    if ldro not in ("AUTO", "0", "1"):
        raise SectionError("ldro", f"{ldro!r} is not auto, 0 or 1")
    txmode = (_text(c, "txmode") or "managed").upper()
    if txmode not in ("MANAGED", "DIRECT"):
        raise SectionError("txmode", f"{txmode!r} is not managed or direct")
    # IFAC is ONE setting: a name without its passphrase is authentication derived from a
    # public string, worse than none (the same rule as the direct-SPI interface).
    netname, passphrase = _text(c, "networkname"), _text(c, "passphrase")
    if bool(netname) != bool(passphrase):
        raise SectionError("passphrase" if netname else "networkname",
                           "IFAC needs both networkname and passphrase, or neither")
    rf_log_path = _path(c, "rf_log_path") if _switch(c, "rf_log", False) else None
    return Settings(
        band=band, conf_socket=_path(c, "conf_socket"), data_socket=_path(c, "data_socket"),
        frequency=frequency, bandwidth=bandwidth, sf=sf, cr=cr,
        txpower=_int(c, "txpower", cap, MIN_TXPOWER, cap),
        preamble=_int(c, "preamble", rnode_preamble(sf, bandwidth, cr) if rnode_framing else 8, 6, 512),
        syncword=_int(c, "syncword", 0x12, 0, 0xFF, base=0),
        crc=_switch(c, "crc", True), ldro=ldro, txmode=txmode,
        cadidle=_int(c, "cadidle", 50, 0, 2000), cadwait=_int(c, "cadwait", 1500, 50, 5000),
        cadrssi=_int(c, "cadrssi", -90, -130, 0), cadmonitor=_switch(c, "cadmonitor", False),
        cadtxaftertimeout=_switch(c, "cadtxaftertimeout", False), rnode_framing=rnode_framing,
        airtime_short=_percent(c, "airtime_limit_short", 33.0, 100.0),
        airtime_long=_percent(c, "airtime_limit_long", min(LONG_LIMIT_DEFAULT[band], ceiling), ceiling),
        state_dir=_text(c, "state_dir") or ".", rf_log_path=rf_log_path, ifac=bool(netname))


def conf_lines(s):
    """The CONF lines the interface sends at connect and on READY, in order."""
    on = {False: "0", True: "1"}
    return ["SET TXRESULT=1",
            f"SET TXMODE={s.txmode}",
            f"SET CADIDLE={s.cadidle}",
            f"SET CADWAIT={s.cadwait}",
            f"SET CADRSSI={s.cadrssi}",
            f"SET CADMONITOR={on[s.cadmonitor]}",
            f"SET CADTXAFTERTIMEOUT={on[s.cadtxaftertimeout]}",
            (f"SET MODE=LORA FREQ={s.frequency / 1e6:.6f} BW={BANDWIDTHS[s.bandwidth]} SF={s.sf} CR={s.cr} "
             f"POWER={s.txpower} PREAMBLE={s.preamble} SYNC=0x{s.syncword:02X} CRC={on[s.crc]} LDRO={s.ldro}")]


def parse(text):
    """One section as the node reads it: with RNS's own config reader (its vendored ConfigObj), so
    the pre-check and the node never read a line differently. The `[[...]]` header is optional; a
    second section, an unreadable line or a key given twice is a SectionError."""
    lines = text.splitlines()
    nested = any(line.strip().startswith("[[") for line in lines)
    try:
        # Where RNS keeps it; stop at the first error (several are otherwise one error without a line).
        config = ConfigObj((["[interfaces]"] if nested else []) + lines, raise_errors=True)
    except DuplicateError as exc:
        raise SectionError(exc.line.partition("=")[0].strip(), "is given twice") from None
    except ConfigObjError as exc:
        raise SectionError(f"line {exc.line_number - nested}", "is not `key = value`") from None
    parent = config["interfaces"] if nested else config
    if parent.sections[nested:] or (nested and parent.scalars):
        raise SectionError("section", "one section only")
    return dict(parent[parent.sections[0]] if nested else parent)


def main(argv=None, stdin=None):
    """loraham-rns-check-section [FILE]: exit 0 when the daemon interface accepts the section (FILE,
    stdin when absent), 1 with one line naming the key and its limit, 2 on a usage error or an
    unreadable FILE."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="loraham-rns-check-section",
                                     description="Check one LoRaDaemonInterface section.")
    parser.add_argument("file", nargs="?", help="the section; stdin when absent")
    path = parser.parse_args(argv).file
    try:
        if path is None:
            text = (stdin or sys.stdin).read()
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
    except (OSError, UnicodeDecodeError) as exc:   # not a refusal: exit 1 is about the section's values
        parser.error(f"cannot read the section: {exc}")
    try:
        section = parse(text)
        if section.get("type") != "LoRaDaemonInterface":
            raise SectionError("type", f"{section.get('type')!r} is not LoRaDaemonInterface")
        check(section)
    except SectionError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
