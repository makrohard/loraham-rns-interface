"""detach() and a later attach of the same section (what `rnstatus --detach` / `--attach` do): both
threads end, both sockets and the RF log close, the duty ledger stays, and a new interface on the
same section connects anew. Observed from outside: the threads the constructor started, the
daemon's view of its connections, the process's open descriptors, the ledger file."""

import json
import os
import threading

from fake_daemon import FAKE, NO_RESULT, SAFETY_S

from loraham_rns import daemon as daemon_mod


def _open_fds_to(path):
    target, found = os.path.realpath(path), []
    for name in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{name}") == target:
                found.append(name)
        except OSError:                              # listdir's own descriptor, already closed
            pass
    return found


def _started_by(make):
    before = set(threading.enumerate())
    iface = make()
    return iface, {t for t in set(threading.enumerate()) - before if t.name != FAKE}


def _ended(threads):
    for t in threads:
        t.join(SAFETY_S)
    return [t for t in threads if t.is_alive()]


def test_detach_ends_both_threads_closes_both_sockets_and_the_rf_log(daemon, interface, tmp_path):
    log = tmp_path / "rf.log"
    iface, threads = _started_by(lambda: interface(rf_log="yes", rf_log_path=log))
    daemon.wait(lambda: iface.online, "online")
    iface.process_outgoing(b"hello")
    daemon.wait(lambda: iface.txb == 5, "the packet sent")
    assert len(threads) == 2 and _open_fds_to(log)
    iface.detach()
    assert _ended(threads) == []
    daemon.wait(lambda: daemon.closed == 2, "both connections closed by the interface")
    assert not iface.online
    assert _open_fds_to(log) == []
    entries = json.loads((tmp_path / "airtime.json").read_text())["entries"]
    assert len(entries) == 1, "the duty ledger stays"


def test_a_new_attach_of_the_same_section_connects_anew_and_keeps_the_ledger(daemon, interface, tmp_path):
    first = interface()
    daemon.wait(lambda: first.online, "online")
    first.process_outgoing(b"one")
    daemon.wait(lambda: first.txb == 3, "the first packet")
    first.detach()
    daemon.wait(lambda: daemon.closed == 2, "the first connection closed")
    second = interface()
    daemon.wait(lambda: second.online, "the second online")
    second.process_outgoing(b"two")
    daemon.wait(lambda: second.txb == 3, "the second packet")
    assert daemon.connections == 2 and daemon.frames == [b"one", b"two"]
    entries = json.loads((tmp_path / "airtime.json").read_text())["entries"]
    assert len(entries) == 2, "the second interface charges into the same ledger"


def test_detach_while_the_daemon_is_away_ends_without_waiting_for_the_reconnect(daemon, interface, monkeypatch):
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RECONNECT_S", 10 * SAFETY_S)   # longer than any wait here
    daemon.close()                                   # nothing listens: the interface keeps retrying
    iface, threads = _started_by(lambda: interface())
    iface.detach()
    assert _ended(threads) == []


def test_detach_while_a_frame_waits_for_its_result_ends_the_wait(daemon, interface, monkeypatch):
    monkeypatch.setattr(daemon_mod.LoRaDaemonInterface, "RESULT_MARGIN_S", SAFETY_S)   # far beyond the test
    iface, threads = _started_by(lambda: interface())
    daemon.wait(lambda: iface.online, "online")
    daemon.results.append(NO_RESULT)
    iface.process_outgoing(b"waiting")
    daemon.wait(lambda: daemon.frames, "the frame at the daemon")
    iface.detach()
    assert _ended(threads) == []
