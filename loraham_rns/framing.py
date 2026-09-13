"""RNode air framing — the one header byte the official RNode firmware puts on
every LoRa packet, so a LoRaHAM box and an RNode can exchange packets.

Facts, read from markqvist/RNode_Firmware (``Framing.h``, ``Config.h``,
``RNode_Firmware.ino`` ``transmit()`` / ``receive_callback()``):

* ``header = random(256) & 0xF0``; ``FLAG_SPLIT`` (0x01) is set when the packet
  exceeds one frame's payload (``SINGLE_MTU - HEADER_L`` = 254). The sequence is
  ``header >> 4``.
* TX writes the SAME header byte at the start of every frame, so the split flag
  is set on the last fragment too. Firmware ``MTU`` is 508: two frames.
* RX: split + no buffer → start a buffer; split + same sequence → append and
  DELIVER (the second fragment completes the packet); split + other sequence →
  the buffer is replaced by the new first fragment; not split → any buffer is
  discarded and this frame is delivered on its own.
* The firmware has no timeout: an orphan first fragment lives until the next
  split frame or the next whole packet. That risks gluing a stale half onto an
  unrelated later fragment that happens to carry the same 4-bit sequence, so
  this side adds ONE guard the firmware lacks — a buffer older than ``max_age``
  is dropped instead of completed. It only ever discards; it never delivers
  anything the firmware would not.
"""

import math
import random
import time

HEADER_L = 1
FLAG_SPLIT = 0x01
SEQ_UNSET = 0xFF
FRAME_PAYLOAD = 255 - HEADER_L      # SINGLE_MTU - HEADER_L
FRAMED_MTU = 2 * FRAME_PAYLOAD      # the firmware's MTU (508): two frames

PREAMBLE_SYMBOLS_MIN = 18           # Config.h LORA_PREAMBLE_SYMBOLS_MIN
PREAMBLE_TARGET_MS = 24.0           # LORA_PREAMBLE_TARGET_MS
PREAMBLE_FAST_DELTA_MS = 18.0       # LORA_PREAMBLE_FAST_DELTA
FAST_THRESHOLD_BPS = 30e3           # LORA_FAST_THRESHOLD_BPS


def rnode_preamble(sf, bw, cr):
    """The preamble length the RNode firmware programs for these settings —
    ``updateBitrate()`` in Utilities.h: symbols for a 24 ms target (6 ms above
    30 kbps), never fewer than 18. An SX127x receiver only locks when its own
    preamble setting is at least as long as the transmitter's, so the box must
    follow this rule rather than the firmware's minimum.
    """
    sf, bw, cr = int(sf), float(bw), int(cr)
    symbol_time_ms = (2 ** sf) / bw * 1000.0
    bitrate = sf * (4.0 / cr) / ((2 ** sf) / (bw / 1000.0)) * 1000.0
    target_ms = PREAMBLE_TARGET_MS - (PREAMBLE_FAST_DELTA_MS if bitrate > FAST_THRESHOLD_BPS else 0.0)
    symbols = target_ms / symbol_time_ms
    return PREAMBLE_SYMBOLS_MIN if symbols < PREAMBLE_SYMBOLS_MIN else int(math.ceil(symbols))


def frame(data, header=None):
    """The LoRa frames the RNode firmware would send for one packet.

    ``header`` fixes the random high nibble for tests; the split flag is
    always derived from the length, never taken from the caller.
    """
    data = bytes(data)
    if len(data) > FRAMED_MTU:
        raise ValueError(f"{len(data)} bytes exceed the RNode MTU of {FRAMED_MTU}")
    head = (random.getrandbits(8) if header is None else int(header)) & 0xF0
    if len(data) > FRAME_PAYLOAD:
        head |= FLAG_SPLIT
    chunks = [data[i:i + FRAME_PAYLOAD] for i in range(0, len(data), FRAME_PAYLOAD)] or [b""]
    return [bytes([head]) + chunk for chunk in chunks]


class Reassembler:
    """The firmware's receive rules, one frame in, at most one packet out."""

    def __init__(self, max_age=None, clock=time.monotonic):
        self.max_age = max_age
        self._clock = clock
        self._seq = SEQ_UNSET
        self._buf = b""
        self._since = 0.0
        self.dropped = 0             # fragments discarded without delivery

    def _reset(self):
        self._seq, self._buf = SEQ_UNSET, b""

    def feed(self, raw):
        """Return the packet completed by this frame, or None."""
        raw = bytes(raw)
        if len(raw) <= HEADER_L:
            # A header with nothing behind it — the firmware's trailer after an
            # exact-508-byte packet. The firmware would complete whatever half is
            # pending with zero bytes and deliver it; if that half's partner was
            # lost, that is a damaged packet. Deliver nothing, and drop the pending
            # half too, so the next packet reusing the sequence is not glued to it.
            if self._seq != SEQ_UNSET:
                self.dropped += 1
                self._reset()
            self.dropped += 1
            return None
        header, payload = raw[0], raw[HEADER_L:]
        seq, split = header >> 4, bool(header & FLAG_SPLIT)
        now = self._clock()
        if (self._seq != SEQ_UNSET and self.max_age is not None
                and now - self._since > self.max_age):
            self.dropped += 1
            self._reset()
        if split:
            if self._seq == SEQ_UNSET:
                self._seq, self._buf, self._since = seq, payload, now
                return None
            if self._seq == seq:
                packet = self._buf + payload
                self._reset()
                return packet
            self.dropped += 1                    # a new split packet replaces the old half
            self._seq, self._buf, self._since = seq, payload, now
            return None
        if self._seq != SEQ_UNSET:
            self.dropped += 1                    # whole packet clears a pending half
            self._reset()
        return payload
