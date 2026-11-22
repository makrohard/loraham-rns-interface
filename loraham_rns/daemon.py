"""LoRaDaemonInterface — an RNS interface on the LoRaHAM daemon's sockets.

The daemon owns the radio; this interface sends its section's settings on the CONF socket and
one framed packet at a time on the framed DATA socket, waiting for each frame's TX_RESULT. The
contract with the controller is docs/daemon-interface.md; duty, power policy, RNode framing and
the RF log are the ones the direct-SPI interface uses.
"""

import math
import queue
import select
import socket
import struct
import threading
import time
from collections import deque

import RNS

from .duty import DutyAccount, DutyError
from .framing import FRAMED_MTU, Reassembler, frame
from .rflog import RfLog
from .section import check, conf_lines

RX_PACKET, TX_PACKET, ERROR, TX_RESULT = 1, 2, 3, 4
OK, BUSY, CHANNEL_BUSY, RADIO_NOT_READY, RADIO_ERROR = 0, 1, 2, 3, 4
UNKNOWN, LOST = "unknown", "lost"           # no TX_RESULT: timed out / the socket went away
NOT_SENT = (BUSY, CHANNEL_BUSY, RADIO_NOT_READY)  # nothing radiated: the packet is tried again
MAX_PAYLOAD = 255                            # one framed TX_PACKET (data-protocol.md, Size limits)
NO_SIGNAL = -32768
BROADCASTS = ("TX=", "CAD=", "RSSI=", "RADIO=")


class LoRaDaemonInterface(RNS.Interfaces.Interface.Interface):
    FIXED_MTU = True
    AUTOCONFIGURE_MTU = False
    DEFAULT_IFAC_SIZE = 8
    MAX_HOLD = 120.0                 # a packet waits at most this long for the radio and duty
    MAX_QUEUE_PACKETS = 64
    RECONNECT_S = 5.0
    REPLY_TIMEOUT_S = 5.0
    RESULT_MARGIN_S = 1.0            # beyond CADWAIT + airtime, before a result counts as lost
    OFFLINE_IS_FATAL = False         # the runner exits on `fatal` only: offline is a hold

    def __init__(self, owner, configuration):
        super().__init__()
        s = self.settings = check(configuration)        # every refusal before any socket
        self.owner, self.name = owner, configuration["name"]
        self.max_packet = FRAMED_MTU if s.rnode_framing else MAX_PAYLOAD
        self.HW_MTU = self.max_packet - (self.DEFAULT_IFAC_SIZE if s.ifac else 0)
        self.IN, self.OUT, self.online, self.fatal = True, False, False, None
        self.bitrate = int(s.sf * (4 / s.cr) * s.bandwidth / (1 << s.sf))
        self.duty = DutyAccount(f"{s.state_dir}/airtime.json", short_limit=s.airtime_short,
                                long_limit=s.airtime_long)
        self.rflog = RfLog()
        if s.rf_log_path:
            self.rflog.open(s.rf_log_path)
        self._reassembler = Reassembler(max_age=max(5.0, 4 * self._toa(MAX_PAYLOAD))) if s.rnode_framing else None
        self._tx_queue, self._tx_wake = deque(), threading.Event()
        self._tx_dropped = self._tx_oversize = 0
        self._results = queue.Queue()
        self._ready, self._stop = threading.Event(), threading.Event()
        self._lock = threading.Lock()                 # the two sockets and the send on DATA
        self._conf = self._data = None                # shared: detach() closes them
        self._io_conf = self._io_data = None          # the connection thread's own
        self._conf_buf = self._data_buf = b""
        self._expected = None                         # the TX_RESULT sequence of the next frame
        self._state = ""                              # the radio state the daemon reported last
        self._run = True
        self._threads = [threading.Thread(target=self._io_loop, daemon=True),
                         threading.Thread(target=self._tx_loop, daemon=True)]
        for t in self._threads:
            t.start()

    def __str__(self):
        return f"LoRaDaemonInterface[{self.name}]"

    # -- RNS contract -------------------------------------------------------

    def process_incoming(self, data):
        self.rxb += len(data)
        self.owner.inbound(data, self)

    def process_outgoing(self, data):
        # Queued while offline too: a packet waits up to MAX_HOLD for the radio.
        if len(data) > self.max_packet:
            self._tx_oversize += 1
            RNS.log(f"{self} dropping {len(data)} bytes, over the {self.max_packet} byte limit "
                    f"({self._tx_oversize} oversize drops so far)", RNS.LOG_ERROR)
            return
        if len(self._tx_queue) >= self.MAX_QUEUE_PACKETS:
            self._tx_dropped += 1
            RNS.log(f"{self} outbound queue full ({self.MAX_QUEUE_PACKETS} packets) — dropping "
                    f"packet, {self._tx_dropped} dropped in total", RNS.LOG_ERROR)
            return
        self._tx_queue.append((data, time.time()))
        self._tx_wake.set()

    def detach(self):
        """Both threads stop, both sockets and the RF log close; the duty ledger stays."""
        self._run = False
        self._stop.set()
        self._tx_wake.set()
        self._ready.set()
        self._results.put(LOST)
        self._close_sockets()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(self.REPLY_TIMEOUT_S)
        self.online = False
        self.rflog.close()

    # -- the connection -----------------------------------------------------

    def _io_loop(self):
        while self._run:
            try:
                self._connect()
                self._serve()
            except Exception as exc:              # any fault ends this connection, never the thread
                if self._run:
                    RNS.log(f"{self} daemon connection: {exc}", RNS.LOG_ERROR)
            self._close_sockets()
            self._stop.wait(self.RECONNECT_S)

    def _connect(self):
        conf, data = (socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(2))
        # The connection thread keeps its own references; detach() may clear the shared ones at
        # any time, and a closed socket then raises OSError here, which ends this connection.
        self._io_conf, self._io_data, self._conf_buf, self._data_buf = conf, data, b"", b""
        with self._lock:
            self._conf, self._data = conf, data
        conf.settimeout(self.REPLY_TIMEOUT_S)
        conf.connect(self.settings.conf_socket)
        data.connect(self.settings.data_socket)
        while not self._results.empty():              # a LOST from the last connection
            self._results.get_nowait()
        status = self._request("GET STATUS")
        if not status.startswith("STATUS "):
            raise ValueError(f"GET STATUS answered {status!r}")
        fields = dict(f.split("=", 1) for f in status.split()[1:] if "=" in f)
        self._radio(fields.get("RADIO", ""))

    def _serve(self):
        conf, data = self._io_conf, self._io_data
        while self._run:
            # Lines read along with a reply first: a RADIO= line may already be here.
            while b"\n" in self._conf_buf:
                line, _, self._conf_buf = self._conf_buf.partition(b"\n")
                self._broadcast(line.decode("utf-8", "replace").strip())
            conf.settimeout(None)
            readable, _, _ = select.select([conf, data], [], [])
            if data in readable:
                self._read_data(data)
            if conf in readable:
                chunk = conf.recv(4096)
                if not chunk:
                    raise OSError("the CONF socket closed")
                self._conf_buf += chunk

    def _request(self, line):
        """Send one CONF line and return its reply; broadcasts on the way are handled."""
        conf, deadline = self._io_conf, time.monotonic() + self.REPLY_TIMEOUT_S
        conf.sendall(line.encode() + b"\n")
        while True:
            while b"\n" in self._conf_buf:
                raw, _, self._conf_buf = self._conf_buf.partition(b"\n")
                reply = raw.decode("utf-8", "replace").strip()
                if not reply.startswith(BROADCASTS):
                    return reply
                self._broadcast(reply, apply=False)
            conf.settimeout(max(0.0, deadline - time.monotonic()))
            chunk = conf.recv(4096)
            if not chunk:
                raise OSError(f"the CONF socket closed before the reply to {line!r}")
            self._conf_buf += chunk

    def _broadcast(self, line, apply=True):
        if line.startswith("RADIO="):
            self._radio(line[len("RADIO="):], apply)

    def _radio(self, state, apply=True):
        """READY: the settings again (they are the daemon's process state), then online."""
        self._state = state
        if state != "READY":
            self._set_online(False, f"radio {state or 'state unknown'}")
            return
        if not apply or self.online:
            return
        for line in conf_lines(self.settings):
            reply = self._request(line)
            if reply != "OK":
                raise ValueError(f"the daemon refused {line!r}: {reply}")
        # Online only if the radio is still READY: a RADIO= line read with a reply above may
        # have reported it gone meanwhile (then the next READY applies the settings again).
        if self._state == "READY":
            self._set_online(True, "radio READY, settings applied")

    def _set_online(self, online, why):
        if online != self.online:
            RNS.log(f"{self} {'online' if online else 'offline'}: {why}", RNS.LOG_NOTICE)
        self.online = online
        (self._ready.set if online else self._ready.clear)()

    def _close_sockets(self):
        with self._lock:
            socks, self._conf, self._data = (self._conf, self._data), None, None
        for sock in socks:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        self._expected = None                         # a new connection may be a new daemon
        self._set_online(False, "daemon connection closed")
        self._results.put(LOST)

    def _read_data(self, sock):
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("the DATA socket closed")
        self._data_buf += chunk
        while len(self._data_buf) >= 3:
            kind, length = struct.unpack_from("<BH", self._data_buf)
            if len(self._data_buf) < 3 + length:
                return
            payload, self._data_buf = self._data_buf[3:3 + length], self._data_buf[3 + length:]
            if kind == RX_PACKET and length >= 4:
                rssi, snr = struct.unpack_from("<hh", payload)
                self._received(payload[4:], rssi, snr)
            elif kind == TX_RESULT and length == 4:
                status, _flags, seq = struct.unpack("<BBH", payload)
                self._results.put((status, seq))
            elif kind == ERROR:
                RNS.log(f"{self} daemon error frame: {payload.decode('utf-8', 'replace')}", RNS.LOG_ERROR)

    def _received(self, data, rssi, snr):
        rssi = math.nan if rssi == NO_SIGNAL else rssi / 100.0
        snr = math.nan if snr == NO_SIGNAL else snr / 100.0
        self.rflog.rx(rssi, snr, data)                # what the radio received, before RNS
        if self._reassembler is not None:
            data = self._reassembler.feed(data)
            if data is None:
                return
        self.process_incoming(data)

    # -- sending ------------------------------------------------------------

    def _toa(self, n):
        """LoRa time on air (Semtech datasheet, explicit header) with the section's CRC and LDRO."""
        s = self.settings
        tsym = (1 << s.sf) / s.bandwidth
        de = (tsym >= 0.016) if s.ldro == "AUTO" else s.ldro == "1"
        num = 8 * n - 4 * s.sf + 28 + (16 if s.crc else 0)
        symbols = 8 + max(math.ceil(num / (4 * (s.sf - 2 * de))) * s.cr, 0)
        return (s.preamble + 4.25) * tsym + symbols * tsym

    def _tx_loop(self):
        while self._run:
            if not self._tx_queue:
                self._tx_wake.wait()
                self._tx_wake.clear()
                continue
            data, queued_at = self._tx_queue.popleft()
            try:
                self._send_packet(data, queued_at)
            except DutyError as exc:
                RNS.log(f"{self} TX blocked: {exc}", RNS.LOG_ERROR)
            except Exception as exc:
                RNS.log(f"{self} TX error: {exc}", RNS.LOG_ERROR)

    def _send_packet(self, data, queued_at):
        """One packet: every try a new split (a new RNode sequence), one frame at a time. Its airtime
        is reserved once; frames refused before the air keep their share for the next try."""
        credit, radio_error, head = 0.0, False, None
        while self._run:
            if time.time() - queued_at > self.MAX_HOLD:
                RNS.log(f"{self} dropped {len(data)} bytes: not sent within {self.MAX_HOLD:.0f} s",
                        RNS.LOG_WARNING)
                return
            if not self._ready.is_set():
                self._ready.wait(1.0)
                continue
            if self.settings.rnode_framing:
                # A try after a refusal takes the next sequence, never the refused one: the peer
                # then drops a first half it may hold instead of joining it to this packet.
                frames = frame(data) if head is None else frame(data, (head + 0x10) & 0xF0)
                head = frames[0][0]
            else:
                frames = [data]
            toas = [self._toa(len(f)) for f in frames]
            if sum(toas) > credit:
                ok, _why = self.duty.reserve(sum(toas) - credit)
                if not ok:
                    self._stop.wait(0.25)
                    continue
            credit = 0.0
            for i, f in enumerate(frames):
                status = self._send_frame(f, toas[i])
                if status == OK:
                    self.rflog.tx("ok", f)
                    continue
                if status in (UNKNOWN, LOST):
                    # Counted as transmitted, never repeated: it may have been radiated. A split
                    # packet whose first half is unknown is not continued.
                    RNS.log(f"{self} TX result lost (airtime still charged)", RNS.LOG_ERROR)
                    self.rflog.tx("unconfirmed", f)
                    return
                if status in NOT_SENT:
                    credit = sum(toas[i:])
                    break
                if status == RADIO_ERROR and not radio_error:
                    radio_error = True
                    break
                RNS.log(f"{self} dropped {len(data)} bytes: TX_RESULT status {status}", RNS.LOG_ERROR)
                return
            else:
                self.txb += len(data)
                return

    def _send_frame(self, f, toa):
        with self._lock:
            if self._data is None or not self.online:
                return RADIO_NOT_READY
            try:
                self._data.sendall(struct.pack("<BH", TX_PACKET, len(f)) + f)
            except OSError:
                return RADIO_NOT_READY
        deadline = time.monotonic() + self.settings.cadwait / 1000 + toa + self.RESULT_MARGIN_S
        while True:
            try:
                item = self._results.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                if self._expected is not None:        # its sequence is taken: a late result is old
                    self._expected = (self._expected + 1) & 0xFFFF
                else:
                    # No sequence is known on this connection yet, so a late result could not be
                    # told from the next frame's. A new connection ends it: the daemon delivers a
                    # result only to the connection that sent the frame.
                    RNS.log(f"{self} no TX_RESULT before any sequence: reconnecting", RNS.LOG_WARNING)
                    self._close_sockets()
                return UNKNOWN
            if item == LOST:
                return LOST
            status, seq = item
            if self._expected is not None and (seq - self._expected) & 0xFFFF >= 0x8000:
                RNS.log(f"{self} late TX_RESULT seq {seq} ignored (already charged)", RNS.LOG_NOTICE)
                continue
            self._expected = (seq + 1) & 0xFFFF
            return status
