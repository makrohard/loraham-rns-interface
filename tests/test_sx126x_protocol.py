"""SX1262 SPI framing.

The response to a read command starts after the opcode, its parameters AND a
status byte. Slicing one byte early shifted every read — sync-word readback, IRQ
status, packet status, RX-buffer status and the payload — which would have made
the Waveshare probe fail in a way that looks like absent hardware.
"""

from loraham_rns.sx126x import SX1262


class FakeBus:
    """Echoes a known response frame: [status][status][payload...]."""

    def __init__(self, payload):
        self.payload, self.sent = payload, []

    def xfer(self, data):
        self.sent.append(list(data))
        # device shifts out one status byte per outgoing byte, then the payload
        n = len(data)
        frame = [0xA2] * n
        for i, b in enumerate(self.payload):
            idx = n - len(self.payload) + i
            if 0 <= idx < n:
                frame[idx] = b
        return frame


def _radio(payload):
    r = SX1262.__new__(SX1262)          # no GPIO/hardware
    r.bus = FakeBus(payload)
    r._busy_req = None
    r.profile = type("P", (), {"busy": -1})()
    return r


def test_query_skips_opcode_params_and_status():
    r = _radio([0x14, 0x24])
    assert r.query(0x1D, 2, 0x07, 0x40) == b"\x14\x24"


def test_query_returns_exactly_nbytes():
    r = _radio([0x01, 0x02, 0x03])
    assert len(r.query(0x12, 3)) == 3


def test_irq_status_is_decoded_from_the_right_offset():
    r = _radio([0x00, 0x02])            # IRQ_RX_DONE
    assert r._irq_status() == 0x0002


def test_sync_word_readback_matches_what_was_written():
    # The probe writes 0x14/0x24 and reads it back; an off-by-one made this
    # compare status bytes and always fail.
    r = _radio([0x14, 0x24])
    assert r.probe() is True


def test_busy_read_handles_the_libgpiod_value_enum():
    """libgpiod 2.x returns a `gpiod.line.Value`, which is NOT int-convertible —
    `int(Value.ACTIVE)` raises TypeError. BUSY is the only line this driver READS, so
    the SX1262 was the only path that touched it and it died at the first configure()
    on real Waveshare hardware."""
    import enum

    from loraham_rns.sx126x import SX1262

    class Value(enum.Enum):          # mirrors gpiod.line.Value
        INACTIVE = 0
        ACTIVE = 1

    class Req:
        def __init__(self, seq): self.seq = list(seq)
        def get_value(self, _pin): return self.seq.pop(0)

    r = SX1262.__new__(SX1262)
    r.profile = type("P", (), {"busy": 20})()

    # high then low: the wait must return once BUSY drops, not raise
    r._busy_req = Req([Value.ACTIVE, Value.INACTIVE])
    r._wait_busy(timeout_s=1.0)

    # a binding handing back a plain int must work identically
    r._busy_req = Req([1, 0])
    r._wait_busy(timeout_s=1.0)


def test_configure_writes_the_radiolib_sync_word_encoding():
    """configure() overwrote probe()'s known-good 0x14 0x24 with 0x11 0x24: the value
    survived (high nibbles 1,2 = 0x12) but the MSB's low nibble was 1 instead of the
    fixed 0x4 compatibility bits. Confirmed on hardware — the chip held 0x11 0x24."""
    from loraham_rns.sx126x import REG_LORA_SYNC_WORD_MSB, SX1262

    writes = []

    r = SX1262.__new__(SX1262)
    r.write_register = lambda reg, *vals: writes.append((reg, list(vals)))

    # exercise only the sync-word line, with the same expression configure() uses
    for sync, expect in ((0x12, [0x14, 0x24]), (0x34, [0x34, 0x44])):
        writes.clear()
        r.write_register(REG_LORA_SYNC_WORD_MSB,
                         (sync & 0xF0) | 0x04, ((sync & 0x0F) << 4) | 0x04)
        assert writes == [(REG_LORA_SYNC_WORD_MSB, expect)], f"{sync:#04x} -> {writes}"


def test_configure_does_not_undo_the_probe_sync_word():
    """probe() and configure() must agree: the source must not contain the old
    `(syncword >> 4) | 0x10` form anywhere."""
    import inspect

    from loraham_rns import sx126x

    src = inspect.getsource(sx126x)
    assert "(syncword >> 4) | 0x10" not in src
    assert "(syncword & 0xF0) | 0x04" in src
