"""Profiles must agree with loraham_daemon/hardware_profile.cpp and must never
guess a wiring for a board/band combination that does not exist."""

import pytest

from loraham_rns.profiles import ProfileError, resolve


def test_loraham_bands_match_the_daemon():
    p433, p868 = resolve("loraham", "433"), resolve("loraham", "868")
    assert (p433.cs, p433.irq, p433.reset) == (8, 25, 5)
    assert (p868.cs, p868.irq, p868.reset) == (7, 16, 6)
    # 433 is an SX1278, 868 an RFM95 — register-identical, but labelled right.
    assert (p433.chip, p868.chip) == ("sx1278", "sx1276")


def test_uputronics_has_no_reset_line():
    for band, cs, irq in (("433", 8, 25), ("868", 7, 16)):
        p = resolve("uputronics", band)
        assert (p.cs, p.irq) == (cs, irq)
        assert p.reset == -1, "Uputronics does not route RESET"


def test_waveshare_is_sx1262_on_both_bands():
    # 433 must NOT be inferred as SX1268: the authoritative profile says SX1262.
    for band in ("433", "868"):
        p = resolve(f"waveshare-{band}", band)
        assert p.chip == "sx1262"
        assert (p.cs, p.irq, p.reset, p.busy, p.txen) == (21, 16, 18, 20, 6)
        assert p.tcxo_voltage == 1.8 and p.dio2_rf_switch


def test_refuses_unknown_setup_and_wrong_band():
    with pytest.raises(ProfileError):
        resolve("nonesuch", "868")
    with pytest.raises(ProfileError):
        resolve("uputronics-433", "868")      # board does not serve that band
    with pytest.raises(ProfileError):
        resolve("waveshare-868", "433")
