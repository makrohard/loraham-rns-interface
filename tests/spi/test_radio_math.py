"""Time-on-air and bitrate drive duty-cycle compliance, so they are pinned."""

from loraham_rns.radio import Radio


class _R(Radio):
    def __init__(self):    # no hardware
        pass


def test_bitrate_sf8_125k_cr45():
    assert _R().bitrate(sf=8, bw=125000, cr=5) == 3125


def test_time_on_air_matches_the_measured_announce():
    # A 208-byte RNS announce at SF8/125k/CR4:5 measured ~0.58 s on air.
    toa = _R().time_on_air(208, sf=8, bw=125000, cr=5)
    assert 0.55 < toa < 0.62


def test_low_data_rate_optimize_engages_for_long_symbols():
    slow = _R().time_on_air(50, sf=12, bw=125000, cr=5)   # symbol > 16 ms
    fast = _R().time_on_air(50, sf=7, bw=125000, cr=5)
    assert slow > fast * 10
