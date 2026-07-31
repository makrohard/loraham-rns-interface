"""SX1276/77/78 LoRa driver — register-based.

Proven on air on both the LoRaHAM RFM95 (868) and the Uputronics board (868),
including boards with no RESET line.
"""

import time

from .radio import MAX_PAYLOAD, Radio, RadioError

REG_FIFO, REG_OP_MODE = 0x00, 0x01
REG_FRF_MSB, REG_PA_CONFIG, REG_OCP, REG_LNA = 0x06, 0x09, 0x0B, 0x0C
REG_FIFO_ADDR_PTR, REG_FIFO_TX_BASE, REG_FIFO_RX_BASE = 0x0D, 0x0E, 0x0F
REG_FIFO_RX_CURRENT, REG_IRQ_FLAGS, REG_RX_NB_BYTES = 0x10, 0x12, 0x13
REG_PKT_SNR, REG_PKT_RSSI = 0x19, 0x1A
REG_MODEM_CONFIG_1, REG_MODEM_CONFIG_2 = 0x1D, 0x1E
REG_PREAMBLE_MSB, REG_PAYLOAD_LENGTH, REG_MODEM_CONFIG_3 = 0x20, 0x22, 0x26
REG_SYNC_WORD, REG_DIO_MAPPING_1, REG_VERSION, REG_PA_DAC = 0x39, 0x40, 0x42, 0x4D

MODE_SLEEP, MODE_STDBY, MODE_TX, MODE_RX_CONT = 0x00, 0x01, 0x03, 0x05
MODE_LORA = 0x80
MODE_LOW_FREQ = 0x08          # RegOpMode bit 3: LowFrequencyModeOn (<525 MHz)
IRQ_TX_DONE, IRQ_CRC_ERROR, IRQ_RX_DONE = 0x08, 0x20, 0x40

FSTEP = 32_000_000.0 / (1 << 19)
BW_TABLE = {7800: 0, 10400: 1, 15600: 2, 20800: 3, 31250: 4, 41700: 5,
            62500: 6, 125000: 7, 250000: 8, 500000: 9}


LOW_BAND_MAX_HZ = 525_000_000     # datasheet split between the register banks


class SX127x(Radio):
    EXPECTED_VERSION = 0x12
    _lf_bit = 0                    # set from the configured frequency

    def read(self, reg):
        return self.bus.xfer([reg & 0x7F, 0x00])[1]

    def write(self, reg, val):
        self.bus.xfer([reg | 0x80, val & 0xFF])

    def _mode(self, mode):
        # Bit 3 selects the low-frequency register bank and must PERSIST across
        # every mode write; clearing it leaves a 433 MHz module in high-band mode.
        self.write(REG_OP_MODE, MODE_LORA | self._lf_bit | mode)

    def probe(self):
        v = self.read(REG_VERSION)
        if v != self.EXPECTED_VERSION:
            raise RadioError(f"no SX127x on {self.bus.path}: RegVersion=0x{v:02X}, "
                             f"expected 0x{self.EXPECTED_VERSION:02X}")
        return v

    def configure(self, frequency, bandwidth, sf, cr, txpower, syncword=0x12, preamble=8):
        # SF6 on the SX127x is a special mode: it requires the IMPLICIT header plus
        # specific detection-optimize/threshold values (0x05/0x0C). This driver
        # configures the ordinary explicit-header path, so SF6 would produce a link
        # that never demodulates. Refuse it rather than transmit into a dead mode.
        if int(sf) < 7:
            raise ValueError("SX127x: spreading factor 6 needs implicit-header mode "
                             "and is not supported by this driver — use SF7-12")
        self._lf_bit = MODE_LOW_FREQ if frequency < LOW_BAND_MAX_HZ else 0
        self.hard_reset()                      # no-op when RESET is not wired
        self._mode(MODE_SLEEP)                 # LoRa bit is settable only in sleep
        time.sleep(0.01)
        self._mode(MODE_STDBY)
        self.probe()

        frf = round(frequency / FSTEP)        # round() already yields an int
        for i, shift in enumerate((16, 8, 0)):
            self.write(REG_FRF_MSB + i, (frf >> shift) & 0xFF)

        bw = BW_TABLE.get(bandwidth)
        if bw is None:
            raise RadioError(f"unsupported bandwidth {bandwidth}")
        self.write(REG_MODEM_CONFIG_1, (bw << 4) | ((cr - 4) << 1))   # explicit header
        self.write(REG_MODEM_CONFIG_2, (sf << 4) | 0x04)              # CRC on
        ldro = 1 if ((1 << sf) / bandwidth) > 0.016 else 0
        self.write(REG_MODEM_CONFIG_3, (ldro << 3) | 0x04)            # + AgcAutoOn
        self.write(REG_PREAMBLE_MSB, (preamble >> 8) & 0xFF)
        self.write(REG_PREAMBLE_MSB + 1, preamble & 0xFF)
        self.write(REG_SYNC_WORD, syncword)
        self.write(REG_LNA, 0x23)

        dbm = max(2, min(17, int(txpower)))                           # PA_BOOST only
        self.write(REG_PA_DAC, 0x84)
        self.write(REG_OCP, 0x2B)
        self.write(REG_PA_CONFIG, 0x80 | (dbm - 2))

        self.write(REG_FIFO_TX_BASE, 0x00)
        self.write(REG_FIFO_RX_BASE, 0x00)
        self.write(REG_DIO_MAPPING_1, 0x00)                           # DIO0 RxDone/TxDone

    def start_rx(self):
        self.write(REG_FIFO_ADDR_PTR, 0x00)
        self.write(REG_IRQ_FLAGS, 0xFF)
        self._mode(MODE_RX_CONT)

    def poll_rx(self):
        """(payload, rssi, snr) or None. Clears the flags it consumes."""
        flags = self.read(REG_IRQ_FLAGS)
        if not flags & IRQ_RX_DONE:
            return None
        self.write(REG_IRQ_FLAGS, IRQ_RX_DONE | IRQ_CRC_ERROR)
        if flags & IRQ_CRC_ERROR:
            return None
        nb = self.read(REG_RX_NB_BYTES)
        self.write(REG_FIFO_ADDR_PTR, self.read(REG_FIFO_RX_CURRENT))
        data = bytes(self.bus.xfer([REG_FIFO & 0x7F] + [0x00] * nb)[1:])
        raw_snr = self.read(REG_PKT_SNR)
        snr = (raw_snr - 256 if raw_snr > 127 else raw_snr) / 4.0
        # Datasheet 5.5.5: the RSSI constant differs per band (-157 high, -164 low).
        rssi = self.read(REG_PKT_RSSI) - (164 if self._lf_bit else 157)
        if snr < 0:
            rssi += snr
        return (data, rssi, snr) if data else None

    def transmit(self, data, timeout_s):
        if len(data) > MAX_PAYLOAD:
            raise RadioError(f"{len(data)} bytes exceeds the {MAX_PAYLOAD} byte LoRa limit")
        self._mode(MODE_STDBY)
        self.write(REG_IRQ_FLAGS, 0xFF)
        self.write(REG_FIFO_ADDR_PTR, 0x00)
        self.bus.xfer([REG_FIFO | 0x80] + list(data))
        self.write(REG_PAYLOAD_LENGTH, len(data))
        self.set_txen(True)
        self._mode(MODE_TX)
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                if self.read(REG_IRQ_FLAGS) & IRQ_TX_DONE:
                    self.write(REG_IRQ_FLAGS, IRQ_TX_DONE)
                    return True
                time.sleep(0.005)
            return False                       # caller counts it as transmitted
        finally:
            self.set_txen(False)
