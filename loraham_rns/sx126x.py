"""SX1262 LoRa driver — command/opcode based.

Opcodes, parameter codes and register addresses are taken from the RadioLib
checkout pinned by lhpc (src/RadioLib/src/modules/SX126x/), not from memory.

NOTE: this path is code-complete but has NOT been exercised on hardware — no
SX1262 board was available. The PA configuration uses the datasheet's canonical
rows rather than an optimisation table, because a mis-transcribed PA setting can
damage the module.
"""

import time

from .radio import MAX_PAYLOAD, Radio, RadioError

CMD_SET_SLEEP, CMD_SET_STANDBY, CMD_SET_TX, CMD_SET_RX = 0x84, 0x80, 0x83, 0x82
CMD_WRITE_REGISTER, CMD_READ_REGISTER = 0x0D, 0x1D
CMD_WRITE_BUFFER, CMD_READ_BUFFER = 0x0E, 0x1E
CMD_SET_DIO_IRQ_PARAMS, CMD_GET_IRQ_STATUS, CMD_CLEAR_IRQ_STATUS = 0x08, 0x12, 0x02
CMD_SET_RF_FREQUENCY, CMD_SET_PACKET_TYPE, CMD_SET_TX_PARAMS = 0x86, 0x8A, 0x8E
CMD_SET_MODULATION_PARAMS, CMD_SET_PACKET_PARAMS = 0x8B, 0x8C
CMD_SET_BUFFER_BASE_ADDRESS, CMD_SET_PA_CONFIG = 0x8F, 0x95
CMD_GET_RX_BUFFER_STATUS, CMD_GET_PACKET_STATUS = 0x13, 0x14
CMD_SET_REGULATOR_MODE, CMD_CALIBRATE, CMD_CALIBRATE_IMAGE = 0x96, 0x89, 0x98
CMD_SET_DIO2_AS_RF_SWITCH, CMD_SET_DIO3_AS_TCXO = 0x9D, 0x97
CMD_GET_DEVICE_ERRORS, CMD_CLEAR_DEVICE_ERRORS = 0x17, 0x07
DEVICE_ERR_XOSC_START = 0x0020        # bit 5: the 32 MHz oscillator never started

STANDBY_RC, REGULATOR_DC_DC, CALIBRATE_ALL = 0x00, 0x01, 0x7F
PACKET_TYPE_LORA = 0x01
REG_LORA_SYNC_WORD_MSB = 0x0740

IRQ_TX_DONE, IRQ_RX_DONE, IRQ_CRC_ERR, IRQ_TIMEOUT = 0x0001, 0x0002, 0x0040, 0x0200

FSTEP = 32_000_000.0 / (1 << 25)          # 0.95367431640625 Hz
BW_TABLE = {7810: 0x00, 10420: 0x08, 15630: 0x01, 20830: 0x09, 31250: 0x02,
            41670: 0x0A, 62500: 0x03, 125000: 0x04, 250000: 0x05, 500000: 0x06}
# DIO3 TCXO control voltage codes.
TCXO_VOLTS = {1.6: 0x00, 1.7: 0x01, 1.8: 0x02, 2.2: 0x03,
              2.4: 0x04, 2.7: 0x05, 3.0: 0x06, 3.3: 0x07}
# Datasheet SX1262 PA settings (Table 13-21): power -> (paDutyCycle, hpMax).
# deviceSel 0x00 = SX1262, paLut 0x01. Deliberately only the documented rows.
PA_ROWS = ((14, 0x02, 0x02), (17, 0x02, 0x03), (20, 0x03, 0x05), (22, 0x04, 0x07))

BUSY_TIMEOUT_S = 1.0


class SX1262(Radio):
    def _wait_busy(self, timeout_s=BUSY_TIMEOUT_S):
        """SX126x accepts a command only while BUSY is low. Bounded: a stuck
        BUSY is a dead radio, not something to spin on forever."""
        if self._busy_req is None:
            time.sleep(0.001)
            return
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                raw = self._busy_req.get_value(self.profile.busy)
            except Exception as exc:
                raise RadioError(f"BUSY line failed: {exc}") from None
            # libgpiod 2.x returns a `gpiod.line.Value` enum, which is NOT
            # int-convertible (`int(Value.ACTIVE)` raises TypeError). BUSY is the only
            # line this driver READS, so the SX1262 path was the only one that hit it —
            # and it failed at the first configure() on real hardware. `.value` when
            # present, the object itself when a binding hands back a plain int.
            busy = getattr(raw, "value", raw)
            if busy == 0:
                return
            time.sleep(0.0005)
        raise RadioError(f"SX1262 BUSY stuck high for {timeout_s:.1f}s")

    def cmd(self, opcode, *params):
        self._wait_busy()
        self.bus.xfer([opcode] + list(params))

    def query(self, opcode, nbytes, *params):
        """Read `nbytes` of response to `opcode`.

        The SX126x returns a status byte while the opcode and parameters are
        clocked out, and ANOTHER status byte before the payload, so the response
        starts at 1 (opcode) + len(params) + 1 (status). Slicing one byte early
        shifts every read — sync-word readback, IRQ status, packet status,
        RX-buffer status and the payload itself.
        """
        self._wait_busy()
        raw = self.bus.xfer([opcode] + list(params) + [0x00] * (nbytes + 1))
        start = len(params) + 2
        return bytes(raw[start:start + nbytes])

    def write_register(self, addr, *values):
        self.cmd(CMD_WRITE_REGISTER, (addr >> 8) & 0xFF, addr & 0xFF, *values)

    def read_register(self, addr, n=1):
        return self.query(CMD_READ_REGISTER, n, (addr >> 8) & 0xFF, addr & 0xFF)

    def probe(self):
        # SX126x has no version register; prove the link by writing the LoRa
        # sync word and reading it back.
        self.write_register(REG_LORA_SYNC_WORD_MSB, 0x14, 0x24)
        got = self.read_register(REG_LORA_SYNC_WORD_MSB, 2)
        if len(got) < 2 or got[0] != 0x14 or got[1] != 0x24:
            raise RadioError(f"no SX1262 on {self.bus.path}: sync-word readback "
                             f"{got.hex() if got else '<none>'} != 1424")
        return True

    def configure(self, frequency, bandwidth, sf, cr, txpower, syncword=0x12, preamble=8):
        self.hard_reset()
        self._wait_busy()
        self.cmd(CMD_SET_STANDBY, STANDBY_RC)
        self.cmd(CMD_SET_REGULATOR_MODE, REGULATOR_DC_DC)

        p = self.profile
        if p.tcxo_voltage and not self._setup_tcxo(p.tcxo_voltage):
            # The board has a plain crystal, not a TCXO on DIO3. Waiting for a TCXO
            # that is not there leaves XOSC_START_ERR set and the chip stuck in
            # STBY_RC: SetTx is accepted but never starts, so TX_DONE never arrives.
            # Verified on a Waveshare SX1262 HAT — every TCXO voltage errored, plain
            # XTAL transmitted immediately. Re-init cleanly without the TCXO.
            self.note(f"no TCXO on DIO3 (XOSC_START_ERR at {p.tcxo_voltage} V) "
                      f"— falling back to the crystal")
            self.hard_reset()
            self._wait_busy()
            self.cmd(CMD_SET_STANDBY, STANDBY_RC)
            self.cmd(CMD_SET_REGULATOR_MODE, REGULATOR_DC_DC)
            self.cmd(CMD_CLEAR_DEVICE_ERRORS, 0x00, 0x00)
            self.cmd(CMD_CALIBRATE, CALIBRATE_ALL)
            time.sleep(0.02)
            self._wait_busy()
        if p.dio2_rf_switch:
            self.cmd(CMD_SET_DIO2_AS_RF_SWITCH, 0x01)

        self.cmd(CMD_SET_PACKET_TYPE, PACKET_TYPE_LORA)
        self.probe()

        # Image calibration for the band in use, then the frequency itself.
        if frequency < 500_000_000:
            self.cmd(CMD_CALIBRATE_IMAGE, 0x6B, 0x6F)      # 430-440 MHz
        else:
            self.cmd(CMD_CALIBRATE_IMAGE, 0xD7, 0xDB)      # 863-870 MHz
        frf = round(frequency / FSTEP)        # round() already yields an int
        self.cmd(CMD_SET_RF_FREQUENCY, (frf >> 24) & 0xFF, (frf >> 16) & 0xFF,
                 (frf >> 8) & 0xFF, frf & 0xFF)

        bw = BW_TABLE.get(bandwidth)
        if bw is None:
            raise RadioError(f"unsupported bandwidth {bandwidth}")
        ldro = 1 if ((1 << sf) / bandwidth) > 0.016 else 0
        self.cmd(CMD_SET_MODULATION_PARAMS, sf, bw, cr - 4, ldro)
        self.cmd(CMD_SET_BUFFER_BASE_ADDRESS, 0x00, 0x00)
        self._preamble = int(preamble)
        self._set_packet_params(self._preamble, MAX_PAYLOAD)
        # RadioLib's exact encoding (SX126x::setSyncWord): each nibble of the 1-byte
        # sync word goes in the HIGH nibble of its register, and both LOW nibbles are the
        # fixed 0x4 compatibility bits. This wrote 0x11 0x24 for 0x12 — the value carried
        # (high nibbles 1,2) but the MSB's low nibble was 1 instead of 4. It interoperated
        # with SX127x here, yet it is out of spec and `probe()` already writes 0x14 0x24,
        # so configure() was undoing its own known-good value.
        self.write_register(REG_LORA_SYNC_WORD_MSB,
                            (syncword & 0xF0) | 0x04,
                            ((syncword & 0x0F) << 4) | 0x04)
        self._set_tx_power(txpower)

        irqs = IRQ_TX_DONE | IRQ_RX_DONE | IRQ_CRC_ERR | IRQ_TIMEOUT
        self.cmd(CMD_SET_DIO_IRQ_PARAMS, (irqs >> 8) & 0xFF, irqs & 0xFF,
                 (irqs >> 8) & 0xFF, irqs & 0xFF, 0, 0, 0, 0)   # all IRQs on DIO1

    def _setup_tcxo(self, voltage):
        """Enable the TCXO on DIO3 and report whether the oscillator actually started.

        Returns False (never raises) when the chip reports XOSC_START_ERR, so the caller
        can fall back to the crystal. A board without a TCXO is a normal variant, not a
        fault — but it must not be left in the failed state, because SetTx then silently
        does nothing.
        """
        code = TCXO_VOLTS.get(round(float(voltage), 1))
        if code is None:
            raise RadioError(f"unsupported TCXO voltage {voltage}")
        delay = int(0.010 / 15.625e-6)          # 10 ms startup, in 15.625 us steps
        self.cmd(CMD_CLEAR_DEVICE_ERRORS, 0x00, 0x00)
        self.cmd(CMD_SET_DIO3_AS_TCXO, code,
                 (delay >> 16) & 0xFF, (delay >> 8) & 0xFF, delay & 0xFF)
        # Mandatory after enabling the TCXO: the factory calibration ran without it.
        self.cmd(CMD_CALIBRATE, CALIBRATE_ALL)
        time.sleep(0.02)
        self._wait_busy()
        err = self.query(CMD_GET_DEVICE_ERRORS, 2)
        errors = ((err[0] << 8) | err[1]) if len(err) >= 2 else 0
        return not errors & DEVICE_ERR_XOSC_START

    def _set_packet_params(self, preamble, payload_len):
        self.cmd(CMD_SET_PACKET_PARAMS, (preamble >> 8) & 0xFF, preamble & 0xFF,
                 0x00,                 # explicit (variable length) header
                 payload_len & 0xFF,
                 0x01,                 # CRC on
                 0x00)                 # standard IQ

    def _set_tx_power(self, dbm):
        dbm = max(-9, min(22, int(dbm)))
        duty, hpmax = PA_ROWS[0][1], PA_ROWS[0][2]
        for row_dbm, row_duty, row_hp in PA_ROWS:
            if dbm >= row_dbm:
                duty, hpmax = row_duty, row_hp
        self.cmd(CMD_SET_PA_CONFIG, duty, hpmax, 0x00, 0x01)   # deviceSel SX1262
        self.cmd(CMD_SET_TX_PARAMS, dbm & 0xFF, 0x04)          # 200 us ramp

    def start_rx(self):
        self.cmd(CMD_CLEAR_IRQ_STATUS, 0xFF, 0xFF)
        self.cmd(CMD_SET_RX, 0xFF, 0xFF, 0xFF)                 # continuous

    def _irq_status(self):
        b = self.query(CMD_GET_IRQ_STATUS, 2)
        return (b[0] << 8) | b[1] if len(b) >= 2 else 0

    def poll_rx(self):
        irq = self._irq_status()
        if not irq & IRQ_RX_DONE:
            return None
        self.cmd(CMD_CLEAR_IRQ_STATUS, (irq >> 8) & 0xFF, irq & 0xFF)
        if irq & IRQ_CRC_ERR:
            return None
        st = self.query(CMD_GET_RX_BUFFER_STATUS, 2)
        length, start = st[0], st[1]
        data = self.query(CMD_READ_BUFFER, length, start)
        ps = self.query(CMD_GET_PACKET_STATUS, 3)
        rssi = -ps[0] / 2.0
        snr = (ps[1] - 256 if ps[1] > 127 else ps[1]) / 4.0
        return (bytes(data), rssi, snr) if data else None

    def transmit(self, data, timeout_s):
        if len(data) > MAX_PAYLOAD:
            raise RadioError(f"{len(data)} bytes exceeds the {MAX_PAYLOAD} byte LoRa limit")
        self.cmd(CMD_SET_STANDBY, STANDBY_RC)
        self.cmd(CMD_CLEAR_IRQ_STATUS, 0xFF, 0xFF)
        # The CONFIGURED preamble, not a hard-coded 8: transmit() reset the packet
        # params and silently overrode it, so airtime was mis-accounted for any
        # other value (under-accounted below 8) and the setting had no effect.
        self._set_packet_params(getattr(self, "_preamble", 8), len(data))
        self.cmd(CMD_WRITE_BUFFER, 0x00, *data)
        self.set_txen(True)
        self.cmd(CMD_SET_TX, 0x00, 0x00, 0x00)                 # no hardware timeout
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                irq = self._irq_status()
                if irq & IRQ_TX_DONE:
                    self.cmd(CMD_CLEAR_IRQ_STATUS, 0xFF, 0xFF)
                    return True
                if irq & IRQ_TIMEOUT:
                    self.cmd(CMD_CLEAR_IRQ_STATUS, 0xFF, 0xFF)
                    return False
                time.sleep(0.005)
            return False
        finally:
            self.set_txen(False)
