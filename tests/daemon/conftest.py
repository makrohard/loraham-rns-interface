"""The daemon interface against a scripted daemon: `daemon` is the fake, `interface(**keys)` builds
LoRaDaemonInterface on its sockets with the duty ledger and RF log in the test's directory, and
detaches every interface it built at the end."""

import pytest
import RNS
from fake_daemon import FakeDaemon

from loraham_rns.daemon import LoRaDaemonInterface


class Owner:
    def __init__(self):
        self.inbound_packets = []

    def inbound(self, data, iface):
        self.inbound_packets.append(bytes(data))


@pytest.fixture
def daemon():
    d = FakeDaemon()
    yield d
    d.close()


def _base_init(self):
    """RNS's Interface.__init__ reads announce defaults off a running Reticulum instance; only the
    counters the interface uses are needed here."""
    self.rxb = self.txb = 0
    self.online = False


@pytest.fixture
def interface(daemon, tmp_path, monkeypatch):
    monkeypatch.setattr(RNS.Interfaces.Interface.Interface, "__init__", _base_init)
    built = []

    def make(**keys):
        keys.setdefault("state_dir", tmp_path)
        iface = LoRaDaemonInterface(Owner(), daemon.section(**keys))
        built.append(iface)
        return iface
    yield make
    for iface in built:
        iface.detach()
