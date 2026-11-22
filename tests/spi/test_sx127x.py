"""The SX127x driver's own refusals."""

import pytest


def test_sx127x_refuses_sf6():
    from loraham_rns.sx127x import SX127x
    radio = SX127x.__new__(SX127x)
    with pytest.raises(ValueError, match="SF7-12"):
        radio.configure(868_500_000, 125_000, 6, 5, 14)
