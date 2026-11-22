"""The daemon interface's section (docs/daemon-interface.md, "The section"): the defaults a missing
key takes, every refusal naming its key, and the CONF lines it becomes, byte for byte."""

import pytest

from loraham_rns.section import SectionError, check, conf_lines

BASE = {"band": "868", "frequency": "868500000", "conf_socket": "/run/loraham/loraconf-r1.sock",
        "data_socket": "/run/loraham/loraf-r1.sock"}


def _section(**extra):
    return {**BASE, **{k: str(v) for k, v in extra.items()}}


def test_a_bare_section_takes_the_reticulum_defaults():
    s = check(_section())
    assert (s.bandwidth, s.sf, s.cr, s.preamble, s.syncword, s.crc, s.ldro) == (125000, 8, 5, 8, 0x12, True, "AUTO")
    assert (s.txmode, s.cadidle, s.cadwait, s.cadrssi, s.cadmonitor, s.cadtxaftertimeout) == \
        ("MANAGED", 50, 1500, -90, False, False)
    assert (s.airtime_short, s.airtime_long, s.rnode_framing, s.rf_log_path, s.ifac) == (33.0, 1.0, False, None, False)


@pytest.mark.parametrize("band,frequency,power,long_limit", [
    ("868", 868_500_000, 14, 1.0),      # the band cap and the controller's hourly default
    ("433", 433_775_000, 10, 10.0),
    ("868", 864_000_000, 14, 0.1),      # the default never above the segment's ceiling
])
def test_power_and_hourly_limit_default_to_the_band(band, frequency, power, long_limit):
    s = check(_section(band=band, frequency=frequency))
    assert (s.txpower, s.airtime_long) == (power, long_limit)


def test_rnode_framing_implies_the_firmware_preamble_unless_set():
    assert check(_section(rnode_framing="yes")).preamble == 18
    assert check(_section(rnode_framing="yes", preamble=12)).preamble == 12


@pytest.mark.parametrize("extra,key", [
    ({"band": "915"}, "band"),
    ({"frequency": ""}, "frequency"),
    ({"frequency": "868.5"}, "frequency"),
    ({"frequency": "868650000"}, "frequency"),            # an alarm-only gap
    ({"frequency": "869500000", "bandwidth": "500000"}, "frequency"),   # spills out of its segment
    ({"bandwidth": "100000"}, "bandwidth"),
    ({"txpower": "15"}, "txpower"),                       # over the 868 cap
    ({"band": "433", "frequency": "433775000", "txpower": "11"}, "txpower"),
    ({"txpower": "1"}, "txpower"),
    ({"spreadingfactor": "6"}, "spreadingfactor"),
    ({"codingrate": "9"}, "codingrate"),
    ({"preamble": "5"}, "preamble"),
    ({"syncword": "0x100"}, "syncword"),
    ({"ldro": "2"}, "ldro"),
    ({"txmode": "fast"}, "txmode"),
    ({"cadidle": "2001"}, "cadidle"),
    ({"cadwait": "49"}, "cadwait"),
    ({"cadrssi": "1"}, "cadrssi"),
    ({"cadmonitor": "maybe"}, "cadmonitor"),
    ({"rnode_framing": "maybe"}, "rnode_framing"),
    ({"airtime_limit_short": "0"}, "airtime_limit_short"),
    ({"airtime_limit_long": "1.5"}, "airtime_limit_long"),   # over 868.5's 1 % ceiling
    ({"networkname": "lab"}, "passphrase"),
    ({"passphrase": "secret"}, "networkname"),
    ({"rf_log": "yes"}, "rf_log_path"),
    ({"rf_log": "yes", "rf_log_path": "rf.log"}, "rf_log_path"),
    ({"conf_socket": ""}, "conf_socket"),
    ({"data_socket": "loraf-r1.sock"}, "data_socket"),
])
def test_a_value_outside_its_range_is_refused_naming_the_key(extra, key):
    with pytest.raises(SectionError) as err:
        check(_section(**extra))
    assert err.value.key == key


def test_the_section_becomes_these_conf_lines():
    s = check(_section(bandwidth=31250, spreadingfactor=9, codingrate=6, txpower=10, preamble=18,
                       syncword="0x2b", crc="no", ldro=1, txmode="direct", cadidle=0, cadwait=800,
                       cadrssi=-100, cadmonitor="yes", cadtxaftertimeout="yes"))
    assert conf_lines(s) == [
        "SET TXRESULT=1", "SET TXMODE=DIRECT", "SET CADIDLE=0", "SET CADWAIT=800", "SET CADRSSI=-100",
        "SET CADMONITOR=1", "SET CADTXAFTERTIMEOUT=1",
        "SET MODE=LORA FREQ=868.500000 BW=31.25 SF=9 CR=6 POWER=10 PREAMBLE=18 SYNC=0x2B CRC=0 LDRO=1"]
