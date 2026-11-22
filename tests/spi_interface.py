"""The direct-SPI interface without its hardware, for the tests of the hooks it applies
(the RF log, RNode framing): a fake radio, a fake owner, an interface built without a bus,
and a minimal interface section."""

import threading
from collections import deque

from loraham_rns.interface import LoRaSPIInterface
from loraham_rns.rflog import RfLog


class FakeRadio:
    """Just enough radio for the two loops: one packet to hear, one to send."""

    def __init__(self, heard, confirm):
        self.heard, self.confirm = heard, confirm
        self.sent = []

    def wait_irq(self, timeout):
        pass

    def poll_rx(self):
        got, self.heard = self.heard, None
        return got

    def time_on_air(self, n, sf, bw, cr, preamble):
        return 0.1

    def transmit(self, data, timeout):
        self.sent.append(bytes(data))
        return self.confirm

    def start_rx(self):
        pass


class FakeDuty:
    def reserve(self, toa):
        return True, ""


class FakeOwner:
    def __init__(self):
        self.inbound_packets = []

    def inbound(self, data, iface):
        self.inbound_packets.append(bytes(data))


def bare_interface(tmp_path, radio):
    """The interface without its hardware: __init__ needs a bus and a radio, the
    hooks under test do not."""
    iface = LoRaSPIInterface.__new__(LoRaSPIInterface)
    iface.name = "LoRa"
    iface.owner = FakeOwner()
    iface.radio, iface.duty = radio, FakeDuty()
    iface.sf, iface.bandwidth, iface.cr, iface.preamble = 8, 125000, 5, 8
    iface.rxb = iface.txb = 0
    iface.last_rssi = iface.last_snr = None
    iface._tx_queue = deque()
    iface._tx_event = threading.Event()
    iface._tx_active = threading.Event()
    iface._radio_lock = threading.RLock()
    iface._run = True
    iface.rnode_framing, iface._reassembler = False, None
    iface.rflog = RfLog()
    iface.rflog.open(str(tmp_path / "rf-reticulum.log"))
    return iface


def spi_config(tmp_path, **extra):
    c = {"name": "LoRa", "hardware": "loraham", "band": "868", "frequency": 868500000,
         "airtime_limit_short": 5.0, "airtime_limit_long": 1.0, "state_dir": str(tmp_path)}
    c.update(extra)
    return c
