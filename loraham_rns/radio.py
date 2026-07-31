"""Radio abstraction shared by the SX127x and SX126x drivers.

The interface owns the threads; a radio only exposes primitives. Every wait is
bounded — an unbounded BUSY/IRQ poll is how a radio bug becomes a hung stack.
"""

import math
import time
from datetime import timedelta

import gpiod
from gpiod.line import Bias, Direction, Edge, Value

MAX_PAYLOAD = 255


class RadioError(RuntimeError):
    pass


class Radio:
    def __init__(self, bus, profile, chip_path):
        self.bus, self.profile, self.chip_path = bus, profile, str(chip_path)
        self._reset_req = None
        self._irq_req = None
        self._busy_req = None
        self.notes = []                  # operational notices, drained by the interface
        self._claim_lines()

    # -- GPIO ---------------------------------------------------------------

    def _claim_lines(self):
        p = self.profile
        if p.reset >= 0:
            self._reset_req = gpiod.request_lines(
                self.chip_path, consumer="loraham-rns-reset",
                config={p.reset: gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.ACTIVE)})
        # IRQ: DIO0 on SX127x, DIO1 on SX126x. Used only to WAKE a loop; the
        # status registers stay the authority, so a missed edge costs latency.
        self._irq_req = gpiod.request_lines(
            self.chip_path, consumer="loraham-rns-irq",
            config={p.irq: gpiod.LineSettings(
                direction=Direction.INPUT, edge_detection=Edge.RISING,
                bias=Bias.PULL_DOWN)})
        if p.busy >= 0:
            self._busy_req = gpiod.request_lines(
                self.chip_path, consumer="loraham-rns-busy",
                config={p.busy: gpiod.LineSettings(direction=Direction.INPUT)})
        if p.txen >= 0:
            self._txen_req = gpiod.request_lines(
                self.chip_path, consumer="loraham-rns-txen",
                config={p.txen: gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.INACTIVE)})
        else:
            self._txen_req = None

    def wait_irq(self, timeout_s):
        """True if an edge arrived. NEVER call read_edge_events() unless this
        returned True — it blocks until events are buffered.

        A GPIO fault here is not recoverable by retrying: raise RadioError so the
        interface goes offline and the runner exits, rather than spinning while
        reporting itself healthy.
        """
        try:
            if self._irq_req.wait_edge_events(timedelta(seconds=timeout_s)):
                self._irq_req.read_edge_events()
                return True
            return False
        except Exception as exc:
            raise RadioError(f"IRQ line failed: {exc}") from None

    def hard_reset(self):
        if self._reset_req is None:
            return False        # not wired (Uputronics) — caller soft-resets
        try:
            self._reset_req.set_value(self.profile.reset, Value.INACTIVE)
            time.sleep(0.002)
            self._reset_req.set_value(self.profile.reset, Value.ACTIVE)
            time.sleep(0.010)
        except Exception as exc:
            raise RadioError(f"reset line failed: {exc}") from None
        return True

    def note(self, message):
        """A one-line operational notice from the driver.

        Collected here rather than logged directly: this layer must not import RNS
        (it is usable standalone), and the interface surfaces these through RNS.log
        once it is attached."""
        self.notes.append(message)

    def set_txen(self, on):
        # Failing to DEASSERT would leave the RF path keyed while we report
        # ourselves online — the worst outcome available here.
        if self._txen_req is not None:
            try:
                self._txen_req.set_value(self.profile.txen,
                                         Value.ACTIVE if on else Value.INACTIVE)
            except Exception as exc:
                raise RadioError(f"TX-enable line failed ({'assert' if on else 'release'}): "
                                 f"{exc}") from None

    def close(self):
        for req in (self._reset_req, self._irq_req, self._busy_req,
                    getattr(self, "_txen_req", None)):
            try:
                if req is not None:
                    req.release()
            except Exception:
                pass

    # -- shared maths -------------------------------------------------------

    def time_on_air(self, payload_len, sf, bw, cr, preamble=8):
        """Semtech SX127x/SX126x datasheet LoRa time-on-air (explicit header,
        CRC on). Identical formula on both families."""
        tsym = (1 << sf) / bw
        de = 1 if tsym > 0.016 else 0
        num = 8 * payload_len - 4 * sf + 28 + 16
        den = 4 * (sf - 2 * de)
        symbols = 8 + max(math.ceil(num / den) * cr, 0)
        return (preamble + 4.25) * tsym + symbols * tsym

    def bitrate(self, sf, bw, cr):
        return int(sf * (4 / cr) * bw / (1 << sf))
