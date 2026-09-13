"""RNode air framing: the header byte and split rules of the official RNode
firmware (Framing.h / RNode_Firmware.ino), and the two places the interface
applies them — TX after the duty reservation, RX after the RF log and before
RNS sees the packet. Behaviour only, through the fake radio; the default (off)
must leave every existing test untouched."""

import pytest
import RNS

from loraham_rns.framing import (FLAG_SPLIT, FRAME_PAYLOAD, FRAMED_MTU, Reassembler,
                                 frame, rnode_preamble)
from loraham_rns.interface import LoRaSPIInterface
from loraham_rns.radio import MAX_PAYLOAD
from test_rflog import _Owner, _Radio, _bare_interface, _config

# ---- the header byte -----------------------------------------------------------

def test_a_short_packet_is_one_frame_with_a_random_sequence_and_no_split_flag():
    frames = frame(b"\x01\x00\x9c" + bytes(177), header=0xC7)   # low nibble is never copied
    assert len(frames) == 1
    assert frames[0][0] == 0xC0 and frames[0][1:] == b"\x01\x00\x9c" + bytes(177)


def test_the_sequence_is_random_per_packet():
    seen = {frame(b"x")[0][0] >> 4 for _ in range(200)}
    assert len(seen) > 1 and all(f[0] & 0x0F == 0 for f in (frame(b"x")[0] for _ in range(20)))


def test_a_400_byte_packet_is_two_frames_sharing_one_header_with_the_split_flag_on_both():
    data = bytes(range(256)) + bytes(range(144))
    frames = frame(data, header=0x50)
    assert [len(f) for f in frames] == [255, 147]
    assert frames[0][0] == frames[1][0] == 0x50 | FLAG_SPLIT   # the firmware writes the SAME byte on every frame
    assert frames[0][1:] + frames[1][1:] == data


def test_exactly_one_frame_of_payload_is_not_split():
    frames = frame(bytes(FRAME_PAYLOAD), header=0x30)
    assert len(frames) == 1 and frames[0][0] == 0x30 and len(frames[0]) == MAX_PAYLOAD


def test_over_the_firmware_mtu_is_refused():
    with pytest.raises(ValueError, match="508"):
        frame(bytes(FRAMED_MTU + 1))


# ---- the receive rules ---------------------------------------------------------

def test_a_whole_frame_delivers_its_payload_without_the_header():
    r = Reassembler()
    assert r.feed(b"\xc0\x01\x00\x9c") == b"\x01\x00\x9c"
    assert r.dropped == 0


def test_two_fragments_with_the_same_sequence_reassemble():
    data = bytes(range(256)) + bytes(range(144))
    r = Reassembler()
    first, second = frame(data, header=0x50)
    assert r.feed(first) is None
    assert r.feed(second) == data
    assert r.dropped == 0


def test_an_interleaved_foreign_sequence_is_not_merged():
    """A second split packet with another sequence replaces the pending half — the
    firmware's rule — and the two are never glued together."""
    a = frame(bytes([1]) * 300, header=0x50)
    b = frame(bytes([2]) * 300, header=0x60)
    r = Reassembler()
    assert r.feed(a[0]) is None
    assert r.feed(b[0]) is None                 # replaces a's half
    assert r.feed(b[1]) == bytes([2]) * 300
    assert r.dropped == 1                        # a's first half


def test_an_orphan_fragment_is_dropped_by_the_next_whole_packet():
    a = frame(bytes([1]) * 300, header=0x50)
    r = Reassembler()
    assert r.feed(a[0]) is None
    assert r.feed(b"\x70hello") == b"hello"     # the firmware clears its buffer here
    assert r.dropped == 1
    assert r.feed(a[1]) is None                 # the late second half starts a new buffer, alone
    assert r.dropped == 1


def test_a_stale_half_is_not_completed_by_a_later_fragment_with_the_same_sequence():
    """The one rule the firmware lacks: an old first half must not be glued onto an
    unrelated packet that happens to reuse the 4-bit sequence."""
    now = [0.0]
    r = Reassembler(max_age=10.0, clock=lambda: now[0])
    a = frame(bytes([1]) * 300, header=0x50)
    b = frame(bytes([2]) * 300, header=0x50)    # same sequence, a minute later
    assert r.feed(a[0]) is None
    now[0] = 61.0
    assert r.feed(b[0]) is None                 # a is dropped, b's half is pending
    assert r.feed(b[1]) == bytes([2]) * 300
    assert r.dropped == 1


def test_a_full_mtu_packet_is_two_frames_and_the_firmwares_trailing_header_is_ignored():
    """For exactly 508 bytes the firmware's write loop closes the second frame at 255
    bytes and then ends a THIRD frame holding only the header byte. We send two —
    its receiver completes on the second — and on receive that lone header must
    neither start a buffer nor disturb the next packet."""
    data = bytes(508)
    frames = frame(data, header=0x90)
    assert [len(f) for f in frames] == [255, 255]
    r = Reassembler()
    assert r.feed(frames[0]) is None and r.feed(frames[1]) == data
    assert r.feed(bytes([0x91])) is None            # the firmware's trailing header-only frame
    nxt = frame(bytes(300), header=0x90)             # same sequence, right after
    assert r.feed(nxt[0]) is None and r.feed(nxt[1]) == bytes(300)
    assert r.dropped == 1


def test_the_trailer_after_a_lost_second_half_discards_the_first_half():
    """Auditor R1: 508-byte packet, second data frame lost, firmware trailer arrives,
    then a 300-byte packet reuses sequence 9. The stale first half must go with the
    trailer, never be glued onto the new packet."""
    r = Reassembler(max_age=5.0, clock=lambda: 0.0)
    assert r.feed(b"\x91" + b"A" * 254) is None
    assert r.feed(b"\x91") is None
    assert r.feed(b"\x91" + b"B" * 254) is None
    assert r.feed(b"\x91" + b"B" * 46) == b"B" * 300
    assert r.dropped == 2                        # the stale half and the trailer


def test_a_header_with_nothing_behind_it_delivers_nothing():
    r = Reassembler()
    assert r.feed(b"\xc0") is None and r.feed(b"") is None
    assert r.dropped == 2


# ---- the interface, framing OFF: bytes on the air are the RNS packet ---------------

def test_off_is_the_default_and_leaves_the_air_bytes_bare(tmp_path):
    radio = _Radio(heard=(b"\xc0\x01\x00", -80.0, 1.0), confirm=True)
    iface = _bare_interface(tmp_path, radio)
    iface._transmit(b"\x71\x00\x05", queued_at=0)
    assert radio.sent == [b"\x71\x00\x05"]

    def stop_after_first(data):
        iface._run = False
        LoRaSPIInterface.process_incoming(iface, data)
    iface.process_incoming = stop_after_first
    iface._rx_loop()
    assert iface.owner.inbound_packets == [b"\xc0\x01\x00"]   # the 0xc0 is NOT stripped


# ---- the interface, framing ON ------------------------------------------------------

def _framed_interface(tmp_path, radio):
    iface = _bare_interface(tmp_path, radio)
    iface.rnode_framing, iface._reassembler = True, Reassembler(max_age=5.0)
    return iface


def test_tx_prepends_the_header_and_the_rf_log_shows_the_framed_bytes(tmp_path):
    radio = _Radio(heard=None, confirm=True)
    iface = _framed_interface(tmp_path, radio)
    iface._transmit(b"\x71\x00\x05", queued_at=0)
    [sent] = radio.sent
    assert sent[0] & 0x0F == 0 and sent[1:] == b"\x71\x00\x05"
    line = (tmp_path / "rf-reticulum.log").read_text()
    assert f" TX rssi=- snr=- len=4 outcome=ok hex={sent.hex()} " in line
    assert iface.txb == 3                                    # RNS bytes, not air bytes


def test_tx_of_a_400_byte_packet_is_two_frames_each_charged_and_logged(tmp_path):
    radio = _Radio(heard=None, confirm=True)
    iface = _framed_interface(tmp_path, radio)
    charged = []
    class _Duty:
        def reserve(self, toa):
            charged.append(toa)
            return True, ""
    iface.duty = _Duty()
    data = bytes(range(256)) + bytes(range(144))
    iface._transmit(data, queued_at=0)
    assert [len(f) for f in radio.sent] == [255, 147]
    assert radio.sent[0][0] == radio.sent[1][0] and radio.sent[0][0] & FLAG_SPLIT
    assert radio.sent[0][1:] + radio.sent[1][1:] == data
    assert charged == [0.2]                                  # two frames at 0.1 s each, one reservation
    assert (tmp_path / "rf-reticulum.log").read_text().count(" TX ") == 2


def test_an_unconfirmed_first_frame_does_not_stop_the_second(tmp_path):
    radio = _Radio(heard=None, confirm=False)
    iface = _framed_interface(tmp_path, radio)
    iface._transmit(bytes(300), queued_at=0)
    assert len(radio.sent) == 2
    assert (tmp_path / "rf-reticulum.log").read_text().count("outcome=unconfirmed") == 2


def test_rx_strips_the_header_logs_the_framed_bytes_and_reassembles_before_rns(tmp_path):
    data = bytes(range(256)) + bytes(range(144))
    first, second = frame(data, header=0xA0)
    heard = [(first, -70.0, 9.0), (second, -71.0, 8.5)]

    class _TwoFrames(_Radio):
        def poll_rx(self):
            return heard.pop(0) if heard else None
    radio = _TwoFrames(heard=None, confirm=True)
    iface = _framed_interface(tmp_path, radio)

    def stop_after_first(data):
        iface._run = False
        LoRaSPIInterface.process_incoming(iface, data)
    iface.process_incoming = stop_after_first
    iface._rx_loop()
    assert iface.owner.inbound_packets == [data]             # one RNS packet, header gone
    assert iface.rxb == 400
    log = (tmp_path / "rf-reticulum.log").read_text()
    assert log.count(" RX ") == 2 and f"hex={first.hex()} " in log   # the air bytes, as heard


def test_rx_of_the_bench_announce_hands_rns_the_bare_packet(tmp_path):
    """The announce the box logged as c0 01 00 9c…: with framing on, RNS gets 01 00 9c…"""
    air = bytes.fromhex("c001009c9fb5d4dc")
    radio = _Radio(heard=(air, -69.0, 12.0), confirm=True)
    iface = _framed_interface(tmp_path, radio)

    def stop_after_first(data):
        iface._run = False
        LoRaSPIInterface.process_incoming(iface, data)
    iface.process_incoming = stop_after_first
    iface._rx_loop()
    assert iface.owner.inbound_packets == [bytes.fromhex("01009c9fb5d4dc")]


# ---- configuration -------------------------------------------------------------------

@pytest.mark.parametrize("value,want", [("yes", True), ("no", False), (None, False)])
def test_the_switch_sets_the_mtu_before_the_hardware(tmp_path, value, want, monkeypatch):
    """HW_MTU is decided from the switch: 508 (the RNode's own figure) framed, 255 bare.
    Read before the bus is opened, so a bare config reaches it on a machine with neither."""
    monkeypatch.setattr(RNS.Interfaces.Interface.Interface, "__init__", lambda self: None)
    class _Stop(Exception):
        pass
    def _no_bus(*a, **k):
        raise _Stop()
    monkeypatch.setattr("loraham_rns.interface.SpiBus", _no_bus)
    extra = {} if value is None else {"rnode_framing": value}
    iface = LoRaSPIInterface.__new__(LoRaSPIInterface)
    with pytest.raises(_Stop):
        iface.__init__(_Owner(), _config(tmp_path, **extra))
    assert (iface.HW_MTU, iface.rnode_framing) == (FRAMED_MTU if want else MAX_PAYLOAD, want)


@pytest.mark.parametrize("sf,bw,cr,want", [(8, 125000, 5, 18), (7, 125000, 5, 24), (8, 250000, 5, 24),
                                           (7, 250000, 5, 47), (7, 500000, 5, 94), (12, 125000, 5, 18)])
def test_the_firmware_preamble_rule(sf, bw, cr, want):
    """updateBitrate() in RNode_Firmware 1.86: 24 ms target, 6 ms above 30 kbps, min 18."""
    assert rnode_preamble(sf, bw, cr) == want


@pytest.mark.parametrize("extra,want", [({}, 8), ({"rnode_framing": "yes"}, 18),
                                        ({"rnode_framing": "yes", "spreadingfactor": 7}, 24),
                                        ({"rnode_framing": "yes", "spreadingfactor": 7, "bandwidth": 250000, "frequency": 866000000}, 47),
                                        ({"rnode_framing": "yes", "preamble": 12}, 12),
                                        ({"rnode_framing": "no", "preamble": 18}, 18)])
def test_framing_implies_the_rnode_preamble_unless_set(tmp_path, extra, want, monkeypatch):
    """An SX127x receiver only locks when its own preamble setting is at least the
    transmitter's; framing on → the firmware's value for these settings, explicit wins."""
    monkeypatch.setattr(RNS.Interfaces.Interface.Interface, "__init__", lambda self: None)
    class _Stop(Exception):
        pass
    def _no_bus(*a, **k):
        raise _Stop()
    monkeypatch.setattr("loraham_rns.interface.SpiBus", _no_bus)
    iface = LoRaSPIInterface.__new__(LoRaSPIInterface)
    with pytest.raises(_Stop):
        iface.__init__(_Owner(), _config(tmp_path, **extra))
    assert iface.preamble == want


def test_a_bad_switch_refuses_the_interface(tmp_path, monkeypatch):
    monkeypatch.setattr(RNS.Interfaces.Interface.Interface, "__init__", lambda self: None)
    with pytest.raises(ValueError, match="rnode_framing must be yes or no"):
        LoRaSPIInterface(_Owner(), _config(tmp_path, rnode_framing="maybe"))
