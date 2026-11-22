"""A scripted LoRaHAM daemon for the daemon interface's tests: a CONF and a framed DATA socket
(local UNIX sockets in a short temporary directory), the lines and frames it received, and
the TX_RESULTs it answers with. Waits are ordered by a condition; SAFETY_S bounds a hang only."""

import shutil
import socket
import struct
import tempfile
import threading
from collections import deque

SAFETY_S = 10.0
TICK_S = 0.01
FAKE = "fake daemon"                # the name of every thread of the fake
OK, CHANNEL_BUSY, RADIO_NOT_READY = 0, 2, 3
NO_RESULT, LATE = "none", "late"         # a frame without a result; one whose result comes later


class FakeDaemon:
    def __init__(self, radio="READY"):
        # AF_UNIX paths are short (108 bytes): not under pytest's long per-test directory.
        self.dir = tempfile.mkdtemp(prefix="fd")
        self.conf_path, self.data_path = f"{self.dir}/loraconf-r1.sock", f"{self.dir}/loraf-r1.sock"
        self.radio = radio
        self.refuse = {}                   # a CONF line -> the reply instead of OK
        self.lines, self.frames = [], []   # what it received
        self.replied = []                  # the CONF lines it has sent its reply to
        self.results = deque()             # per frame: a status, NO_RESULT or LATE (default OK)
        self.connections = 0
        self.closed = 0                    # client connections that ended (EOF from the client)
        self.seq, self._late = 100, None
        self.cond = threading.Condition()
        self._conns, self._run = [], True
        self._listeners = []
        for path, serve in ((self.conf_path, self._serve_conf), (self.data_path, self._serve_data)):
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            srv.listen(4)
            self._listeners.append(srv)
            threading.Thread(target=self._accept, args=(srv, serve), name=FAKE, daemon=True).start()

    def section(self, **extra):
        c = {"name": "LoRa r1", "type": "LoRaDaemonInterface", "band": "868", "frequency": "868500000",
             "conf_socket": self.conf_path, "data_socket": self.data_path}
        c.update({k: str(v) for k, v in extra.items()})
        return c

    def wait(self, predicate, what):
        """Until `predicate()` holds. It may read the interface's state, which notifies nobody, so the
        condition is also re-checked every TICK_S; SAFETY_S only bounds a hang."""
        with self.cond:
            for _ in range(int(SAFETY_S / TICK_S)):
                if self.cond.wait_for(predicate, TICK_S):
                    return
        raise AssertionError(f"timed out waiting for {what}")

    def broadcast(self, line):
        with self.cond:
            conf = [c for c, kind in self._conns if kind == "conf"][-1]
        conf.sendall(line.encode() + b"\n")

    def rx(self, data, rssi_c=-9150, snr_c=275):
        with self.cond:
            sock = [c for c, kind in self._conns if kind == "data"][-1]
        sock.sendall(struct.pack("<BHhh", 1, 4 + len(data), rssi_c, snr_c) + data)

    def drop(self):
        """Close every client connection: the interface sees EOF."""
        with self.cond:
            conns, self._conns = self._conns, []
        for c, _ in conns:
            c.shutdown(socket.SHUT_RDWR)
            c.close()

    def close(self):
        self._run = False
        self.drop()
        for srv in self._listeners:
            srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- the server side ------------------------------------------------------

    def _closed(self):
        with self.cond:
            self.closed += 1
            self.cond.notify_all()

    def _accept(self, srv, serve):
        while self._run:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with self.cond:
                self._conns.append((conn, "conf" if serve == self._serve_conf else "data"))
                self.connections += serve == self._serve_conf
                self.cond.notify_all()
            threading.Thread(target=serve, args=(conn,), name=FAKE, daemon=True).start()

    def _lines_of(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                self._closed()
                return
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                yield line.decode().strip()

    def _serve_conf(self, conn):
        for line in self._lines_of(conn):
            with self.cond:
                self.lines.append(line)
                self.cond.notify_all()
                reply = (f"STATUS RADIO={self.radio} TX=0 CAD=0" if line == "GET STATUS"
                         else self.refuse.get(line, "OK"))
            try:
                conn.sendall(reply.encode() + b"\n")
            except OSError:
                return
            with self.cond:
                self.replied.append(line)
                self.cond.notify_all()

    def _serve_data(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                self._closed()
                return
            buf += chunk
            while len(buf) >= 3:
                kind, length = struct.unpack_from("<BH", buf)
                if len(buf) < 3 + length:
                    break
                payload, buf = buf[3:3 + length], buf[3 + length:]
                with self.cond:
                    self.frames.append(payload)
                    script = self.results.popleft() if self.results else OK
                    self.seq += 1
                    out = []
                    if script == LATE:
                        self._late = self.seq
                    elif script != NO_RESULT:
                        if self._late is not None:              # the earlier frame's result, late
                            out.append(struct.pack("<BHBBH", 4, 4, OK, 0, self._late))
                            self._late = None
                        out.append(struct.pack("<BHBBH", 4, 4, script, 0, self.seq))
                    self.cond.notify_all()
                try:
                    conn.sendall(b"".join(out))
                except OSError:
                    return

