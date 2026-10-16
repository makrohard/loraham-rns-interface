"""The SPI chip-select mode, the same four rows as the daemon's: no kernel chip selects /
CS 8 / CS 7 / any other CS. The kernel's choice is read from the controller's cs-gpios."""

import importlib
import sys
import types

import pytest


class _Spi:
    opened = None

    def open(self, bus, dev):
        type(self).opened = (bus, dev)

    def xfer2(self, data):
        return [0] * len(data)


class _Lines:
    claimed: list = []  # noqa: RUF012 - reset per test by the fixture

    def get_value(self, pin):
        return 0

    def set_value(self, pin, value):
        pass


def _request_lines(chip, consumer, config):
    _Lines.claimed += list(config)
    return _Lines()


@pytest.fixture
def spibus(tmp_path, monkeypatch):
    gpiod = types.ModuleType("gpiod")
    gpiod.request_lines = _request_lines
    gpiod.LineSettings = lambda **kw: kw
    line = types.ModuleType("gpiod.line")
    line.Direction = types.SimpleNamespace(OUTPUT="out")
    line.Value = types.SimpleNamespace(ACTIVE=1, INACTIVE=0)
    spidev = types.ModuleType("spidev")
    spidev.SpiDev = _Spi
    for name, mod in (("gpiod", gpiod), ("gpiod.line", line), ("spidev", spidev)):
        monkeypatch.setitem(sys.modules, name, mod)
    mod = importlib.import_module("loraham_rns.spibus")
    mod = importlib.reload(mod)
    _Spi.opened, _Lines.claimed = None, []
    (tmp_path / "run").mkdir(mode=0o700)
    return mod, tmp_path


def _cs_gpios(tmp_path, monkeypatch, mod, pins):
    f = tmp_path / "cs-gpios"
    f.write_bytes(b"".join((1).to_bytes(4, "big") + p.to_bytes(4, "big") + (1).to_bytes(4, "big")
                           for p in pins))
    monkeypatch.setattr(mod, "CS_GPIOS", str(f), raising=False)   # absent before kernel CS


def _bus(mod, tmp_path, cs):
    return mod.SpiBus("/dev/spidev0.0", cs, "/dev/gpiochip0", tmp_path / "run")


def test_soft_cs_keeps_spidev00_and_drives_cs_as_a_gpio(spibus, monkeypatch):
    mod, tmp = spibus
    _cs_gpios(tmp, monkeypatch, mod, [])
    monkeypatch.setattr(mod.SpiBus, "_enforce_no_kernel_cs", lambda self: None)
    bus = _bus(mod, tmp, 7)
    assert _Spi.opened == (0, 0) and _Lines.claimed == [7] and bus._cs is not None


def test_an_unreadable_cs_gpios_keeps_todays_soft_cs_path(spibus, monkeypatch):
    """No proof of kernel chip selects -> exactly the pre-change path (its own checks decide)."""
    mod, tmp = spibus
    monkeypatch.setattr(mod, "CS_GPIOS", str(tmp / "absent"), raising=False)
    monkeypatch.setattr(mod.SpiBus, "_enforce_no_kernel_cs", lambda self: None)
    bus = _bus(mod, tmp, 8)
    assert _Spi.opened == (0, 0) and _Lines.claimed == [8] and bus._cs is not None


@pytest.mark.parametrize("cs, dev", [(8, 0), (7, 1)])
def test_kernel_cs_uses_the_ce_spidev_and_claims_no_cs_line(spibus, monkeypatch, cs, dev):
    mod, tmp = spibus
    _cs_gpios(tmp, monkeypatch, mod, [8, 7])
    bus = _bus(mod, tmp, cs)
    assert _Spi.opened == (0, dev) and _Lines.claimed == [] and bus.no_cs_mode == "kernel-cs"
    assert bus.xfer([0x42, 0x00]) == [0, 0]


def test_kernel_cs_refuses_a_cs_off_ce0_ce1(spibus, monkeypatch):
    mod, tmp = spibus
    _cs_gpios(tmp, monkeypatch, mod, [8, 7])
    with pytest.raises(mod.SpiBusError, match="CS is GPIO 21"):
        _bus(mod, tmp, 21)
    assert _Spi.opened is None


@pytest.mark.parametrize("pins", [[8], [8, 24], [7, 8]])
def test_an_unsupported_chip_select_layout_is_refused(spibus, monkeypatch, pins):
    mod, tmp = spibus
    _cs_gpios(tmp, monkeypatch, mod, pins)
    with pytest.raises(mod.SpiBusError, match="unsupported SPI chip-select"):
        _bus(mod, tmp, 8)
