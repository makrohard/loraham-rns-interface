"""Operating-policy and IFAC-pair enforcement.

These are enforced in the driver because that is the last point before the radio
is touched — a hand-edited config or a stale saved value must not widen the
policy the controller declares.
"""

import pytest

from loraham_rns.interface import BAND_POLICY, PERMITTED_SEGMENTS, duty_ceiling


def check(band, freq, power):
    lo, hi, cap = BAND_POLICY[band]
    return lo <= freq <= hi and power <= cap


def test_band_policy_matches_the_srd_limits():
    assert BAND_POLICY["433"][2] == 10      # EN 300 220: 10 mW ERP
    assert BAND_POLICY["868"][2] == 14      # 25 mW ERP


def test_a_433_stack_cannot_transmit_on_868():
    assert not check("433", 868_500_000, 10)
    assert not check("868", 434_500_000, 14)


def test_defaults_are_inside_their_own_band():
    assert check("433", 434_500_000, 10)
    assert check("868", 868_500_000, 14)


def test_power_above_the_band_ceiling_is_refused():
    assert not check("433", 434_500_000, 14)     # 868's ceiling on 433
    assert not check("868", 868_500_000, 17)


@pytest.mark.parametrize("netname,passphrase,ok", [
    ("", "", True),               # both absent -> IFAC off
    ("net", "secret", True),      # both present -> IFAC on
    ("net", "", False),           # name only -> IFAC from a PUBLIC string
    ("", "secret", False),        # secret only -> half configured
])
def test_ifac_must_be_configured_as_a_pair(netname, passphrase, ok):
    # Reticulum enables IFAC if EITHER value is present, so a leftover network
    # name silently yields authentication anyone can reproduce.
    half = bool(netname) != bool(passphrase)
    assert (not half) == ok


# ---- permitted segments: the whole occupied bandwidth inside one, anything else refused ----

@pytest.mark.parametrize("band,freq,expect", [
    ("433", 433_500_000, 10.0),     # LPD433
    ("868", 864_000_000, 0.1),      # 863-865 is 0.1 %, NOT 1 %
    ("868", 866_000_000, 1.0),
    ("868", 868_500_000, 1.0),      # the channel we actually run on
    ("868", 869_000_000, 0.1),
    ("868", 869_500_000, 10.0),
    ("868", 869_800_000, 1.0),
])
def test_ceiling_follows_the_segment(band, freq, expect):
    assert duty_ceiling(band, freq, 0) == expect


@pytest.mark.parametrize("freq", [868_650_000, 869_300_000, 869_675_000])
def test_alarm_only_gaps_are_refused(freq):
    """868.6-868.7, 869.2-869.4 and 869.65-869.7 MHz carry no generic allocation.
    They must not resolve to any ceiling — the caller refuses on None."""
    assert duty_ceiling("868", freq, 0) is None


def test_the_whole_occupied_bandwidth_must_fit():
    """A 500 kHz channel centred at 869.5 MHz looks legal by centre frequency but
    spills outside the 250 kHz-wide 10 % segment."""
    assert duty_ceiling("868", 869_500_000, 125_000) == 10.0
    assert duty_ceiling("868", 869_500_000, 500_000) is None


@pytest.mark.parametrize("band,freq", [("433", 434_790_000), ("868", 870_000_000)])
def test_a_real_channel_at_the_band_edge_is_refused(band, freq):
    """The old band test accepted exactly these edges while the ceiling lookup
    excluded them, and the mismatch fell back to a 100 % duty limit. Any real
    channel centred on the edge spills over it."""
    assert duty_ceiling(band, freq, 125_000) is None


def test_segments_are_ordered_and_sane():
    """Deliberately NOT contiguous — the alarm-only gaps are legally required."""
    for _band, ranges in PERMITTED_SEGMENTS.items():
        for (lo, hi), pct in ranges:
            assert lo < hi and 0 < pct <= 100
        for ((_, hi), _), ((lo, _), _) in zip(ranges, ranges[1:]):
            assert hi <= lo, "segments must not overlap"
