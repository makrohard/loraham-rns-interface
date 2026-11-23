"""LoRaDaemonInterface against a scripted daemon, through its public paths only: the constructor,
RNS's process_outgoing, the owner's inbound and the daemon's lines and frames. Connect and READY
send the section and nothing else; one frame at a time; a frame refused before the air is tried
again as a new split and charged once; a lost result is charged and never repeated."""

import json
import re
import struct
import types

import pytest
import RNS
from fake_daemon import CHANNEL_BUSY, LATE, NO_RESULT, RADIO_NOT_READY

from loraham_rns import daemon as daemon_mod
from loraham_rns.framing import FLAG_SPLIT, frame
from loraham_rns.section import check, conf_lines


def _settings_sent(daemon):
    return [line for line in daemon.lines if line != "GET STATUS"]


def _charged(tmp_path):
    return sum(a for _, a in json.loads((tmp_path / "airtime.json").read_text())["entries"])


def _rf_log(tmp_path):
    return (tmp_path / "rf.log").read_text().splitlines()


def _outcomes(tmp_path):
    return [line.split(" outcome=")[1].split()[0] for line in _rf_log(tmp_path)]


@pytest.fixture
def refusal_lines(monkeypatch):
    """The node log's (RNS.log) per-packet lines: a sent packet's report and the drop line, with or without
    refusal counts, so a report on a packet that met no refusal is seen too."""
    seen = []
    monkeypatch.setattr(RNS, "log", lambda msg, level=RNS.LOG_NOTICE, *a, **k: seen.append((level, str(msg))))
    return lambda: [(level, msg) for level, msg in seen if re.search(r" sent \d+ bytes|refused tries", msg)]


def test_connect_sends_the_section_and_nothing_else(daemon, interface):
    """No CADIDLE in the section: the interface's 50 ms goes out, never the daemon's own 250."""
    iface = interface()
    daemon.wait(lambda: iface.online, "online")
    assert daemon.lines[0] == "GET STATUS"
    assert _settings_sent(daemon) == conf_lines(check(daemon.section()))
    assert "SET CADIDLE=50" in daemon.lines


def test_a_radio_not_ready_at_connect_stays_offline_until_ready(daemon, interface):
    daemon.radio = "UNPLUGGED"
    iface = interface()
    daemon.wait(lambda: daemon.lines == ["GET STATUS"], "the status query")
    assert not iface.online and _settings_sent(daemon) == []
    daemon.broadcast("RADIO=READY")
    daemon.wait(lambda: iface.online, "online")
    assert _settings_sent(daemon) == conf_lines(check(daemon.section()))


def test_every_return_to_ready_sends_the_settings_again(daemon, interface):
    iface = interface()
    daemon.wait(lambda: iface.online, "online")
    daemon.broadcast("RADIO=UNPLUGGED")
    daemon.wait(lambda: not iface.online, "offline")
    daemon.broadcast("RADIO=READY")
    daemon.wait(lambda: iface.online, "online again")
    assert _settings_sent(daemon) == 2 * conf_lines(check(daemon.section()))


def test_a_refused_setting_keeps_the_interface_offline_and_it_reconnects(daemon, interface, monkeypatch):
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RECONNECT_S", 0.0)
    daemon.refuse["SET CADIDLE=50"] = "ERR INVALID"
    iface = interface()
    daemon.wait(lambda: daemon.connections >= 2, "a reconnect")
    assert not iface.online
    assert "SET MODE=LORA" not in " ".join(daemon.lines), "nothing after the refused line"


def test_txqueue_0_refused_busy_holds_the_interface_offline_until_the_daemon_takes_it(daemon, interface,
                                                                                    monkeypatch):
    """The daemon answers SET TXQUEUE=0 with ERR BUSY while its queue still holds frames: no frame goes
    out meanwhile, the interface connects again and is online once the line is answered OK."""
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RECONNECT_S", 0.0)
    daemon.refuse["SET TXQUEUE=0"] = "ERR BUSY"
    iface = interface(txqueue="0")
    iface.process_outgoing(b"held")
    daemon.wait(lambda: daemon.connections >= 2, "a reconnect after ERR BUSY")
    assert not iface.online and daemon.frames == []
    assert "SET MODE=LORA" not in " ".join(daemon.lines), "nothing after the refused line"
    del daemon.refuse["SET TXQUEUE=0"]                   # the daemon's queue is done
    daemon.wait(lambda: iface.txb == 4, "the held packet sent once TXQUEUE=0 is taken")
    assert daemon.frames == [b"held"]


def test_a_packet_is_one_frame_and_its_airtime_is_charged(daemon, interface, tmp_path):
    iface = interface(rf_log="yes", rf_log_path=tmp_path / "rf.log")
    daemon.wait(lambda: iface.online, "online")
    iface.process_outgoing(b"hello")
    daemon.wait(lambda: iface.txb == 5, "the packet sent")
    assert daemon.frames == [b"hello"]
    assert _charged(tmp_path) == pytest.approx(iface._toa(5))
    assert _rf_log(tmp_path)[0].endswith(' TX rssi=- snr=- len=5 outcome=ok hex=68656c6c6f ascii="hello"')


@pytest.mark.parametrize("refusal", [CHANNEL_BUSY, RADIO_NOT_READY])
def test_a_frame_refused_before_the_air_is_tried_again_and_charged_once(daemon, interface, tmp_path, refusal):
    """With RNode framing every try is a new split: its header differs from the refused one, so the
    peer drops a half it may hold. Charged once: the refused frame never went out."""
    iface = interface(rnode_framing="yes")
    daemon.wait(lambda: iface.online, "online")
    daemon.results.append(refusal)
    iface.process_outgoing(b"x" * 300)                    # split: two frames
    daemon.wait(lambda: iface.txb == 300, "the packet sent")
    first, *rest = daemon.frames
    assert len(rest) == 2 and first[1:] == rest[0][1:]     # the first half again, then the second
    assert first[0] & FLAG_SPLIT and first[0] != rest[0][0], "a new split, a new sequence"
    assert _charged(tmp_path) == pytest.approx(iface._toa(len(rest[0])) + iface._toa(len(rest[1])))


def test_a_second_half_refused_retries_the_whole_packet_charging_the_repeated_half(daemon, interface, tmp_path):
    iface = interface(rnode_framing="yes")
    daemon.wait(lambda: iface.online, "online")
    daemon.results.extend([0, CHANNEL_BUSY])
    iface.process_outgoing(b"x" * 300)
    daemon.wait(lambda: iface.txb == 300, "the packet sent")
    assert len(daemon.frames) == 4
    a, b = (iface._toa(len(f)) for f in daemon.frames[:2])
    assert _charged(tmp_path) == pytest.approx(a + b + a)  # the first half radiated twice


def test_a_lost_result_is_charged_never_repeated_and_ends_a_split(daemon, interface, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RESULT_MARGIN_S", 0.0)
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RECONNECT_S", 0.0)   # the first result: reconnect
    iface = interface(rnode_framing="yes", cadwait=50, rf_log="yes", rf_log_path=tmp_path / "rf.log")
    daemon.wait(lambda: iface.online, "online")
    daemon.results.append(NO_RESULT)
    iface.process_outgoing(b"x" * 300)
    iface.process_outgoing(b"next")
    daemon.wait(lambda: iface.txb == 4, "the next packet sent")
    assert len(daemon.frames) == 2, "the second half is not sent after an unknown first half"
    assert [line.split(" outcome=")[1].split()[0] for line in _rf_log(tmp_path)] == ["unconfirmed", "ok"]
    halves = frame(b"x" * 300)                            # the whole packet was reserved before it went
    assert _charged(tmp_path) == pytest.approx(sum(iface._toa(len(h)) for h in halves) + iface._toa(4))


def test_a_late_result_is_matched_by_sequence_and_ignored(daemon, interface, monkeypatch):
    """The first frame's result arrives after its window, just before the second frame's: it must not
    be read as the second frame's result (which is a refusal here, so the second packet goes again)."""
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RESULT_MARGIN_S", 0.0)
    iface = interface(cadwait=50)
    daemon.wait(lambda: iface.online, "online")
    daemon.results.extend([0, LATE, CHANNEL_BUSY])
    for packet in (b"one", b"two", b"three"):
        iface.process_outgoing(packet)
    daemon.wait(lambda: iface.txb == 3 + 5, "the third packet")   # "two" stays unconfirmed
    assert daemon.frames == [b"one", b"two", b"three", b"three"]


def test_a_late_result_of_the_first_frame_does_not_confirm_the_next(daemon, interface, tmp_path, monkeypatch):
    """No sequence is known when the first frame's result is missing: its late OK must not be read as
    the next frame's result, which is a refusal here, so the next packet goes again. The interface
    reconnects; the first packet stays unconfirmed and charged."""
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RESULT_MARGIN_S", 0.0)
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RECONNECT_S", 0.0)
    iface = interface(cadwait=50, rf_log="yes", rf_log_path=tmp_path / "rf.log")
    daemon.wait(lambda: iface.online, "online")
    daemon.results.extend([LATE, CHANNEL_BUSY])
    iface.process_outgoing(b"one")
    iface.process_outgoing(b"two")
    daemon.wait(lambda: len(daemon.frames) == 3, "the refused packet sent again")
    daemon.wait(lambda: iface.txb == 3, "the second packet confirmed")
    assert daemon.frames == [b"one", b"two", b"two"]
    assert [line.split(" outcome=")[1].split()[0] for line in _rf_log(tmp_path)] == ["unconfirmed", "ok"]
    assert _charged(tmp_path) == pytest.approx(iface._toa(3) + iface._toa(3))


def test_refused_frames_write_no_rf_line_and_one_node_line_when_the_packet_is_sent(daemon, interface, tmp_path,
                                                                                  refusal_lines):
    """The RF log has one TX line per frame radiated (its counters count them as aired); the node log names
    the refusals once, when the packet ends, never once per try."""
    iface = interface(rf_log="yes", rf_log_path=tmp_path / "rf.log")
    daemon.wait(lambda: iface.online, "online")
    daemon.results.extend([CHANNEL_BUSY, CHANNEL_BUSY])
    iface.process_outgoing(b"hello")
    daemon.wait(lambda: iface.txb == 5, "the packet sent")
    assert daemon.frames == [b"hello"] * 3
    assert _outcomes(tmp_path) == ["ok"]
    [(level, msg)] = refusal_lines()
    assert level == RNS.LOG_NOTICE
    assert re.search(r" sent 5 bytes after 2 refused tries \(channel-busy 2, busy 0, not-ready 0; \d+\.\d s\)$", msg), msg


def test_a_radio_not_ready_refusal_is_named_not_ready_not_a_busy_channel(daemon, interface, refusal_lines):
    iface = interface()
    daemon.wait(lambda: iface.online, "online")
    daemon.results.append(RADIO_NOT_READY)
    iface.process_outgoing(b"hello")
    daemon.wait(lambda: iface.txb == 5, "the packet sent")
    [(_level, msg)] = refusal_lines()
    assert "after 1 refused tries (channel-busy 0, busy 0, not-ready 1;" in msg, msg


def test_a_send_after_the_cad_timeout_is_logged_ok_cad_timeout(daemon, interface, tmp_path, refusal_lines):
    """TX_RESULT flag 0x04: the daemon sent the frame although its CAD wait timed out (a likely collision)."""
    iface = interface(rf_log="yes", rf_log_path=tmp_path / "rf.log")
    daemon.wait(lambda: iface.online, "online")
    daemon.results.extend([(0, 0x05), (0, 0x01)])
    iface.process_outgoing(b"one")
    iface.process_outgoing(b"two")
    daemon.wait(lambda: iface.txb == 6, "both packets sent")
    assert _outcomes(tmp_path) == ["ok-cad-timeout", "ok"]
    assert refusal_lines() == []                          # nothing was refused: no per-packet node line


def test_a_refused_second_half_has_no_rf_line_and_the_packet_one_node_line(daemon, interface, tmp_path,
                                                                           refusal_lines):
    iface = interface(rnode_framing="yes", rf_log="yes", rf_log_path=tmp_path / "rf.log")
    daemon.wait(lambda: iface.online, "online")
    daemon.results.extend([0, CHANNEL_BUSY])
    iface.process_outgoing(b"x" * 300)
    daemon.wait(lambda: iface.txb == 300, "the packet sent")
    assert len(daemon.frames) == 4                        # half 1, half 2 refused, the new split's two halves
    assert _outcomes(tmp_path) == ["ok", "ok", "ok"]
    [(_level, msg)] = refusal_lines()
    assert "after 1 refused tries (channel-busy 1, busy 0, not-ready 0;" in msg, msg


def test_the_drop_after_the_hold_names_the_refusals(daemon, interface, monkeypatch, refusal_lines):
    clock = types.SimpleNamespace(now=1000.0)
    shim = types.SimpleNamespace(**{k: getattr(daemon_mod.time, k) for k in ("monotonic", "sleep")})
    shim.time = lambda: clock.now
    monkeypatch.setattr(daemon_mod, "time", shim)
    iface = interface()
    daemon.wait(lambda: iface.online, "online")
    daemon.results.extend([CHANNEL_BUSY] * 100000)        # refused until the hold is over
    iface.process_outgoing(b"held")
    daemon.wait(lambda: len(daemon.frames) >= 2, "two refused tries")
    clock.now = 1000.0 + iface.MAX_HOLD + 1
    daemon.wait(lambda: refusal_lines(), "the drop line")
    [(level, msg)] = refusal_lines()
    assert level == RNS.LOG_WARNING and iface.txb == 0
    m = re.search(r"dropped 4 bytes: not sent within 120 s after (\d+) refused tries \(channel-busy (\d+), busy 0,"
                  r" not-ready 0; ", msg)
    assert m and m[1] == m[2] and int(m[1]) >= 2, msg


def test_rx_goes_to_rns_after_the_rf_log(daemon, interface, tmp_path):
    iface = interface(rf_log="yes", rf_log_path=tmp_path / "rf.log")
    daemon.wait(lambda: iface.online, "online")
    daemon.rx(b"\xaa\x42\x0a")
    daemon.wait(lambda: iface.owner.inbound_packets, "the packet at RNS")
    assert iface.owner.inbound_packets == [b"\xaa\x42\x0a"]
    assert _rf_log(tmp_path)[0].endswith(' RX rssi=-91.50 snr=2.75 len=3 hex=aa420a ascii=".B."')


def test_rx_with_rnode_framing_reassembles_a_split_before_rns(daemon, interface):
    iface = interface(rnode_framing="yes")
    daemon.wait(lambda: iface.online, "online")
    header = 0x50 | FLAG_SPLIT
    daemon.rx(bytes([header]) + b"a" * 254)
    daemon.rx(bytes([header]) + b"b" * 10)
    daemon.wait(lambda: iface.owner.inbound_packets, "the packet at RNS")
    assert iface.owner.inbound_packets == [b"a" * 254 + b"b" * 10]


def test_a_packet_waits_offline_within_the_hold_and_is_dropped_after_it(daemon, interface, monkeypatch):
    clock = types.SimpleNamespace(now=1000.0)
    shim = types.SimpleNamespace(**{k: getattr(daemon_mod.time, k) for k in ("monotonic", "sleep")})
    shim.time = lambda: clock.now
    monkeypatch.setattr(daemon_mod, "time", shim)
    daemon.radio = "UNPLUGGED"
    iface = interface()
    daemon.wait(lambda: daemon.lines == ["GET STATUS"], "the status query")
    iface.process_outgoing(b"stale")                      # queued at 1000
    clock.now = 1000.0 + iface.MAX_HOLD + 1
    iface.process_outgoing(b"fresh")                      # queued after the hold of the first
    daemon.broadcast("RADIO=READY")
    daemon.wait(lambda: iface.txb == 5, "the fresh packet")
    assert daemon.frames == [b"fresh"]


@pytest.mark.parametrize("extra,mtu", [({}, 255), ({"networkname": "n", "passphrase": "p"}, 247),
                                       ({"rnode_framing": "yes"}, 508)])
def test_the_hardware_mtu_follows_framing_and_leaves_room_for_ifac(daemon, interface, extra, mtu):
    assert interface(**extra).HW_MTU == mtu


def test_an_oversized_packet_is_dropped_before_the_queue(daemon, interface):
    iface = interface()
    daemon.wait(lambda: iface.online, "online")
    iface.process_outgoing(b"x" * 256)
    iface.process_outgoing(b"ok")
    daemon.wait(lambda: iface.txb == 2, "the next packet")
    assert daemon.frames == [b"ok"]


def test_the_frame_on_the_wire_is_a_framed_tx_packet(daemon, interface):
    iface = interface()
    daemon.wait(lambda: iface.online, "online")
    raw = struct.pack("<BH", 2, 3) + b"abc"
    iface.process_outgoing(b"abc")
    daemon.wait(lambda: daemon.frames, "a frame")
    assert struct.pack("<BH", 2, len(daemon.frames[0])) + daemon.frames[0] == raw


def test_a_radio_lost_while_the_settings_go_out_leaves_the_interface_offline(daemon, interface):
    """The daemon reports the radio gone between two settings: the interface must not end online on
    the strength of the replies; the next READY applies the settings again."""
    daemon.refuse["SET TXMODE=MANAGED"] = "RADIO=UNPLUGGED\nOK"
    iface = interface()
    mode = [line for line in conf_lines(check(daemon.section())) if line.startswith("SET MODE=")]
    daemon.wait(lambda: daemon.replied.count(mode[0]) == 1, "the settings answered")
    daemon.refuse.clear()
    daemon.broadcast("RADIO=READY")
    daemon.wait(lambda: daemon.replied.count(mode[0]) == 2 and iface.online, "the settings again, then online")


def test_a_fault_in_the_connection_thread_ends_the_connection_not_the_thread(daemon, interface, monkeypatch):
    """Whatever RNS's inbound (or the reassembler, or the RF log) raises, the connection is closed and
    made again; the interface does not stay offline for good with a runner that never exits."""
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RECONNECT_S", 0.0)
    iface = interface()
    daemon.wait(lambda: iface.online, "online")
    received = []

    def inbound(data, _iface):
        if not received:
            received.append(None)
            raise RuntimeError("inbound failed")
        received.append(bytes(data))
    iface.owner.inbound = inbound
    daemon.rx(b"first")
    daemon.wait(lambda: daemon.connections == 2 and iface.online, "connected again")
    daemon.rx(b"second")
    daemon.wait(lambda: len(received) == 2, "the next packet at RNS")
    assert received == [None, b"second"]
