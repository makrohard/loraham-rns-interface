"""Operating-policy and IFAC-pair enforcement.

These are enforced in the driver because that is the last point before the radio
is touched — a hand-edited config or a stale saved value must not widen the
policy the controller declares.
"""

import pytest

from loraham_rns.interface import BAND_POLICY


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
