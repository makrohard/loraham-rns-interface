"""RF log: the line contract, the switch, copy-truncate retention on one inode,
and the two hooks in the interface — RX before RNS sees the packet, TX with the
radio's own confirmation as the outcome."""

import os
import re
import threading

import pytest
import RNS

from loraham_rns.interface import LoRaSPIInterface
from loraham_rns.rflog import RfLog, format_rx, format_tx, parse_switch
from spi_interface import FakeOwner, FakeRadio, bare_interface, spi_config

# ---- lines ------------------------------------------------------------------

def test_rx_line_carries_signal_and_both_payload_views():
    assert format_rx("2026-09-12T16:03:47.412Z", -104.5, 7.25, b'A"\\\x7f\x00') == (
        '2026-09-12T16:03:47.412Z RX rssi=-104.50 snr=7.25 len=5'
        ' hex=41225c7f00 ascii="A...."\n')


def test_tx_line_carries_an_outcome_and_no_signal():
    assert format_tx("2026-09-12T16:03:51.006Z", "unconfirmed", b"hi") == (
        '2026-09-12T16:03:51.006Z TX rssi=- snr=- len=2 outcome=unconfirmed'
        ' hex=6869 ascii="hi"\n')


@pytest.mark.parametrize("text,want", [("yes", True), ("On", True), ("true", True), ("1", True),
                                       ("no", False), ("OFF", False), ("false", False), ("0", False)])
def test_switch_accepts_the_rns_and_lhpc_spellings(text, want):
    assert parse_switch(text) is want


@pytest.mark.parametrize("text", ["", "maybe", None, "2"])
def test_switch_refuses_anything_else(text):
    with pytest.raises(ValueError):
        parse_switch(text)


# ---- file ---------------------------------------------------------------------

def _lines(path):
    with open(path, "rb") as f:
        return f.read().count(b"\n")


def test_relative_and_empty_paths_are_refused(tmp_path):
    log = RfLog()
    for bad in ("logs/rf.log", "", None):
        with pytest.raises(ValueError):
            log.open(bad)
    assert not log.active
    log.rx(0, 0, b"x")                       # silent no-op while closed


def test_rollover_copies_to_previous_and_keeps_the_inode(tmp_path):
    path = tmp_path / "rf-reticulum.log"
    log = RfLog(max_bytes=2000)
    log.open(str(path))
    inode = os.stat(path).st_ino
    for _ in range(30):                      # ~100 B per line: crosses 2000 once
        log.rx(-90.0, 5.0, bytes(range(16)))
    prev = tmp_path / "rf-reticulum.log.1"
    assert prev.stat().st_size > 0
    assert os.stat(path).st_ino == inode     # truncated in place, not renamed
    assert path.stat().st_size < 2000
    assert _lines(prev) + _lines(path) == 30 # nothing lost

    # External truncate (the controller's Clear): the next line lands at the
    # new end of the same inode.
    with open(path, "r+b") as f:
        f.truncate(0)
    log.tx("ok", b"\x01\x02")
    assert _lines(path) == 1
    assert os.stat(path).st_ino == inode
    log.close()
    assert not log.active
    log.rx(0, 0, b"x")
    assert _lines(path) == 1


# ---- the hooks in the interface ------------------------------------------------

def test_rx_is_logged_before_rns_sees_the_packet(tmp_path):
    radio = FakeRadio(heard=(b"\xaa\x42\x0a", -91.5, 2.75), confirm=True)
    iface = bare_interface(tmp_path, radio)

    def stop_after_first(data):
        iface._run = False
        LoRaSPIInterface.process_incoming(iface, data)
    iface.process_incoming = stop_after_first
    iface._rx_loop()

    assert iface.owner.inbound_packets == [b"\xaa\x42\x0a"]
    line = (tmp_path / "rf-reticulum.log").read_text()
    assert line.endswith(' RX rssi=-91.50 snr=2.75 len=3 hex=aa420a ascii=".B."\n')


@pytest.mark.parametrize("confirm,outcome", [(True, "ok"), (False, "unconfirmed")])
def test_tx_outcome_is_the_radios_confirmation(tmp_path, confirm, outcome):
    radio = FakeRadio(heard=None, confirm=confirm)
    iface = bare_interface(tmp_path, radio)
    iface._transmit(b"hi", queued_at=0)
    assert radio.sent == [b"hi"]
    line = (tmp_path / "rf-reticulum.log").read_text()
    assert line.endswith(f' TX rssi=- snr=- len=2 outcome={outcome} hex=6869 ascii="hi"\n')


def test_a_duty_drop_is_not_a_transmission(tmp_path):
    radio = FakeRadio(heard=None, confirm=True)
    iface = bare_interface(tmp_path, radio)
    iface.MAX_DUTY_HOLD = -1                 # already over the hold: drop at once

    class _Refusing:
        def reserve(self, toa):
            return False, "budget"
    iface.duty = _Refusing()
    iface._transmit(b"hi", queued_at=0)
    assert radio.sent == []
    assert (tmp_path / "rf-reticulum.log").read_text() == ""


@pytest.mark.parametrize("extra,match", [
    ({"rf_log": "yes"}, "rf_log_path"),
    ({"rf_log": "yes", "rf_log_path": "rf.log"}, "absolute"),
    ({"rf_log": "maybe"}, "yes or no"),
])
def test_a_bad_switch_refuses_the_interface_before_the_hardware(tmp_path, extra, match, monkeypatch):
    """Mirrors the daemon and the tnc: the runner must exit, not run unlogged.
    The check sits ahead of the bus and the radio, which is why a bare config
    reaches it on a machine with neither. The RNS base class wants a running
    Reticulum instance for its announce-rate defaults; that part is not under test."""
    monkeypatch.setattr(RNS.Interfaces.Interface.Interface, "__init__", lambda self: None)
    with pytest.raises(ValueError, match=match):
        LoRaSPIInterface(FakeOwner(), spi_config(tmp_path, **extra))


def test_two_threads_never_lose_a_line_or_a_segment_across_rollovers(tmp_path):
    """The RX and TX loops write the same file from two threads. Without one lock around
    rollover + write, two rollovers can race: the second copies the just-truncated live file
    over `.1` and the history is gone. Every line must land in `.1` or the live file."""
    path = tmp_path / "rf-reticulum.log"
    log = RfLog(max_bytes=4000)
    log.open(str(path))
    n = 400
    def rx():
        for i in range(n):
            log.rx(-90.0, 5.0, b"R" * 40)
    def tx():
        for i in range(n):
            log.tx("ok", b"T" * 40)
    a, b = threading.Thread(target=rx), threading.Thread(target=tx)
    a.start(); b.start(); a.join(); b.join()
    prev = tmp_path / "rf-reticulum.log.1"
    live = path.read_bytes()
    assert live == b"" or live.endswith(b"\n")
    kept = _lines(prev) + _lines(path)
    assert prev.stat().st_size > 0                       # the previous segment holds history
    assert kept <= 2 * n and all(line.count(b" R") <= 1 for line in live.splitlines())
    # No torn line: every line in both files is a complete record.
    for f in (prev, path):
        for line in f.read_bytes().splitlines():
            assert re.fullmatch(rb"20\S+ (RX|TX) .*", line), line
