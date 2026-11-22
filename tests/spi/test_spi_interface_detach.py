"""The direct-SPI interface's detach(), checked as the daemon interface's is: both loops end, the
radio and the bus are released, the RF log closes, the duty ledger stays, and a new interface on
the same section starts again. Built through the constructor with the bus and the chip faked."""

import json
import os
import threading

import pytest
import RNS
from spi_interface import FakeOwner, spi_config

from loraham_rns import interface as interface_mod

SAFETY_S = 10.0


class _Bus:
    path, no_cs_mode = "/dev/spidev0.0", "soft-cs"
    closed = 0

    def __init__(self, *args):
        pass

    def close(self):
        _Bus.closed += 1


class _Chip:
    """The chip under the loops: nothing to hear, every transmission confirmed."""
    notes = ()
    closed = 0

    def __init__(self, bus, profile, chip_path):
        self._idle = threading.Event()

    def configure(self, *args):
        pass

    def start_rx(self):
        pass

    def bitrate(self, sf, bw, cr):
        return 3125

    def time_on_air(self, n, sf, bw, cr, preamble):
        return 0.05

    def wait_irq(self, timeout):
        return self._idle.wait(timeout)

    def poll_rx(self):
        return None

    def transmit(self, data, timeout):
        return True

    def close(self):
        _Chip.closed += 1


@pytest.fixture
def spi(monkeypatch, tmp_path):
    monkeypatch.setattr(RNS.Interfaces.Interface.Interface, "__init__",
                        lambda self: setattr(self, "txb", 0) or setattr(self, "rxb", 0))
    monkeypatch.setattr(interface_mod, "SpiBus", _Bus)
    monkeypatch.setitem(interface_mod._DRIVERS, "sx1276", _Chip)
    _Bus.closed = _Chip.closed = 0

    def make():
        before = set(threading.enumerate())
        iface = interface_mod.LoRaSPIInterface(
            FakeOwner(), spi_config(tmp_path, rf_log="yes", rf_log_path=str(tmp_path / "rf.log")))
        return iface, set(threading.enumerate()) - before
    return make


def _open_fds_to(path):
    target, found = os.path.realpath(path), []
    for name in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{name}") == target:
                found.append(name)
        except OSError:
            pass
    return found


def test_detach_ends_both_loops_releases_the_radio_and_keeps_the_ledger(spi, tmp_path):
    iface, threads = spi()
    sent = threading.Event()
    real_transmit = _Chip.transmit
    iface.radio.transmit = lambda data, timeout: sent.set() or real_transmit(iface.radio, data, timeout)
    iface.process_outgoing(b"hello")
    assert sent.wait(SAFETY_S)
    assert len(threads) == 2 and _open_fds_to(tmp_path / "rf.log")
    iface.detach()
    for t in threads:
        t.join(SAFETY_S)
    assert [t for t in threads if t.is_alive()] == []
    assert (_Chip.closed, _Bus.closed, iface.online) == (1, 1, False)
    assert _open_fds_to(tmp_path / "rf.log") == []
    assert len(json.loads((tmp_path / "airtime.json").read_text())["entries"]) == 1


def test_a_new_attach_of_the_same_section_starts_again(spi):
    first, threads = spi()
    first.detach()
    for t in threads:
        t.join(SAFETY_S)
    second, _ = spi()
    assert second.online
    second.detach()
