"""The service runner: its refusals before it reports ready, and what ends it once running."""

import signal
import types

import pytest
import RNS

from loraham_rns import node
from loraham_rns.daemon import LoRaDaemonInterface


@pytest.mark.parametrize("listen,refused", [
    ("127.0.0.1", False), ("127.0.1.1", False), ("localhost", False), ("::1", False),
    ("localhost.example", True), ("127.0.0.1.evil.net", True),
    ("192.168.1.5", True), ("0.0.0.0", True),
])
def test_only_a_real_loopback_bind_is_accepted(tmp_path, listen, refused):
    """A prefix test accepted `localhost.example` and `127.0.0.1.evil.net`."""
    from loraham_rns.node import _unsafe_exposure

    (tmp_path / "config").write_text(f'listen_ip = {listen}\n')
    msg = _unsafe_exposure(str(tmp_path), "192.168.0.0/24")
    assert bool(msg) is refused, msg


@pytest.mark.parametrize("daemon_backend,ticks", [(False, 1), (True, 3)])
def test_offline_ends_the_runner_only_where_offline_is_a_fault(tmp_path, monkeypatch, daemon_backend, ticks):
    """The direct-SPI interface goes offline only with a fault: the runner exits at once. The daemon
    interface's offline is a hold (the daemon's radio not ready): the runner stays until a fault.
    Driven through main() with the node's surroundings faked; each check tick advances the script."""
    iface = types.SimpleNamespace(name="LoRa r1", online=True, fatal=None, bitrate=1)
    if daemon_backend:
        iface.OFFLINE_IS_FATAL = LoRaDaemonInterface.OFFLINE_IS_FATAL
    script = [lambda: setattr(iface, "online", False), lambda: None,
              lambda: setattr(iface, "fatal", "radio failed")]
    done = []

    def tick(_seconds):
        script[len(done)]()
        done.append(1)

    reticulum = types.SimpleNamespace(is_connected_to_shared_instance=False, is_standalone_instance=False)
    monkeypatch.setattr(RNS, "Reticulum", lambda **_kw: reticulum)
    monkeypatch.setattr(RNS.Transport, "interfaces", [iface])
    monkeypatch.setattr(node, "_wait_own_listener", lambda port: True)      # the client port: a host seam
    monkeypatch.setattr(node, "time", types.SimpleNamespace(sleep=tick, monotonic=node.time.monotonic,
                                                            time=node.time.time))
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    ready = tmp_path / "ready"
    rc = node.main(["--config", str(tmp_path), "--interface", "LoRa r1", "--ready-file", str(ready)])
    assert (rc, len(done)) == (node.EXIT_RADIO_FATAL, ticks)
    assert not ready.exists()
