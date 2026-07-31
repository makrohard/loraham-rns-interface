"""Hardware profile table: (lhpc hardware setup, band) -> radio wiring.

This mirrors loraham_daemon/hardware_profile.cpp so the two agree on every
board. Pins are NOT user-configurable: an operator picks the hardware setup in
lhpc (`lhpc hardware`) and the band, and everything else follows. Free-form
pin/TCXO/PA combinations are how you damage a PA, so they are simply not
offered.
"""

from dataclasses import dataclass

NC = -1          # not connected on this board


@dataclass(frozen=True)
class Profile:
    name: str
    chip: str          # "sx1276" | "sx1278" | "sx1262"
    cs: int            # NSS — always driven by us via libgpiod (soft CS)
    irq: int           # SX127x: DIO0.  SX126x: DIO1.
    reset: int = NC
    busy: int = NC     # SX126x only
    txen: int = NC
    rxen: int = NC
    tcxo_voltage: float = 0.0      # SX126x: DIO3-powered TCXO, 0 = none
    dio2_rf_switch: bool = False   # SX126x: DIO2 drives the antenna switch


# Keyed by the daemon's preset name, so the two tables can be diffed by eye.
_PRESETS = {
    # LoRaHAM_Pi dual-module wiring. 433 is an SX1278, 868 an RFM95 (SX1276);
    # register-identical for LoRa, but the label must be right.
    ("loraham", "433"): Profile("loraham-433", "sx1278", cs=8, irq=25, reset=5),
    ("loraham", "868"): Profile("loraham-868", "sx1276", cs=7, irq=16, reset=6),

    # Uputronics expansion boards: RESET and DIO1 are NOT routed.
    ("uputronics-ce0", "433"): Profile("uputronics-ce0", "sx1278", cs=8, irq=25),
    ("uputronics-ce1", "868"): Profile("uputronics-ce1", "sx1276", cs=7, irq=16),

    # Waveshare SX1262 LoRaWAN Node HAT. LF and HF boards are pin-identical and
    # BOTH are SX1262 per the authoritative profile — 433 does NOT imply SX1268.
    ("waveshare-sx1262", "433"): Profile("waveshare-sx1262", "sx1262", cs=21, irq=16,
                                         reset=18, busy=20, txen=6,
                                         tcxo_voltage=1.8, dio2_rf_switch=True),
    ("waveshare-sx1262", "868"): Profile("waveshare-sx1262", "sx1262", cs=21, irq=16,
                                         reset=18, busy=20, txen=6,
                                         tcxo_voltage=1.8, dio2_rf_switch=True),
}

# lhpc hardware setup -> per-band preset (lhpc/core/config.py HW_SETUPS).
_SETUPS = {
    "loraham":         {"433": "loraham",          "868": "loraham"},
    "uputronics":      {"433": "uputronics-ce0",   "868": "uputronics-ce1"},
    "uputronics-433":  {"433": "uputronics-ce0"},
    "uputronics-868":  {"868": "uputronics-ce1"},
    "waveshare-433":   {"433": "waveshare-sx1262"},
    "waveshare-868":   {"868": "waveshare-sx1262"},
}


class ProfileError(ValueError):
    pass


def resolve(setup: str, band: str) -> Profile:
    """(hardware setup, band) -> Profile, or ProfileError. Never guesses."""
    bands = _SETUPS.get(setup)
    if bands is None:
        raise ProfileError(f"unknown hardware setup {setup!r} "
                           f"(known: {', '.join(sorted(_SETUPS))})")
    preset = bands.get(str(band))
    if preset is None:
        raise ProfileError(f"hardware setup {setup!r} does not serve band {band!r} "
                           f"(serves: {', '.join(sorted(bands))})")
    prof = _PRESETS.get((preset, str(band)))
    if prof is None:
        raise ProfileError(f"no wiring for preset {preset!r} on band {band!r}")
    return prof
