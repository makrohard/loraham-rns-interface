"""LoRaSPIInterface — an RNS interface that drives an SX127x/SX1262 directly.

No rnoded, no RNode firmware, no KISS: one LoRa packet is one RNS packet, so the
radio's own PHY framing does the job KISS does over a serial link.
"""

import os
import threading
import time
from collections import deque

import RNS

from .duty import DutyAccount, DutyError
from .profiles import resolve
from .radio import MAX_PAYLOAD, RadioError
from .spibus import SpiBus, SpiBusError
from .sx126x import SX1262
from .sx127x import SX127x

# Per-band operating policy: (min Hz, max Hz, max dBm). 868 SRD allows 25 mW
# (14 dBm); the 433 SRD band allows 10 mW (10 dBm) — EN 300 220.
BAND_POLICY = {
    "433": (433_050_000, 434_790_000, 10),
    "868": (863_000_000, 870_000_000, 14),
}

# PERMITTED segments for GENERIC (non-specific) SRD use, as (lo Hz, hi Hz, max %).
# BNetzA Vfg. 91/2025 / ERC 70-03. The gaps are DELIBERATE: 868.6-868.7, 869.2-869.4
# and 869.65-869.7 MHz are reserved for alarm systems and carry no generic
# allocation, so Reticulum traffic there is simply not permitted — the table lists
# only what is allowed, and anything unlisted fails closed.
PERMITTED_SEGMENTS = {
    "433": (((433_050_000, 434_790_000), 10.0),),
    "868": (((863_000_000, 865_000_000), 0.1),
            ((865_000_000, 868_600_000), 1.0),
            # 868.6-868.7 alarm only
            ((868_700_000, 869_200_000), 0.1),
            # 869.2-869.4 alarm only
            ((869_400_000, 869_650_000), 10.0),
            # 869.65-869.7 alarm only
            ((869_700_000, 870_000_000), 1.0)),
}


def duty_ceiling(band, frequency, bandwidth=0):
    """Hourly duty ceiling for a channel, or None if it is not permitted.

    The WHOLE occupied bandwidth must fit inside ONE segment: a 500 kHz channel
    centred at 869.5 MHz looks legal by centre frequency alone but spills far
    outside the 250 kHz-wide 10 % segment. Returning None means "no permitted
    segment" — callers must refuse, never fall back to a default limit.
    """
    half = float(bandwidth) / 2.0
    low, high = frequency - half, frequency + half
    for (lo, hi), pct in PERMITTED_SEGMENTS.get(band, ()):
        if lo <= low and high <= hi:
            return pct
    return None


_DRIVERS = {"sx1276": SX127x, "sx1277": SX127x, "sx1278": SX127x, "sx1262": SX1262}


class LoRaSPIInterface(RNS.Interfaces.Interface.Interface):
    # We put ONE RNS packet in ONE LoRa payload, and the SX127x/SX1262 FIFO caps that
    # at 255 bytes. RNS's base MTU is 500: with neither flag set,
    # Transport.next_hop_interface_hw_mtu() returns None, links keep the 500-byte
    # default, and everything above 255 was silently dropped here. FIXED_MTU makes RNS
    # honour HW_MTU when negotiating link MTU. Not AUTOCONFIGURE: our limit comes from
    # the radio, not from the bitrate table upstream would apply.
    FIXED_MTU = True
    AUTOCONFIGURE_MTU = False
    DEFAULT_IFAC_SIZE = 8
    MAX_DUTY_HOLD = 120.0          # give up on a packet rather than queue forever
    # A packet can wait MAX_DUTY_HOLD seconds for duty allowance while local
    # clients keep submitting. Unbounded, that is a memory-exhaustion path on a
    # Pi Zero, so the queue is capped and overflow is dropped LOUDLY.
    MAX_QUEUE_PACKETS = 64

    def __init__(self, owner, configuration):
        super().__init__()
        c = configuration
        self.owner = owner
        self.name = c["name"]

        # Pins and chip come from the hardware profile, never from free-form
        # config: a wrong PA or TCXO value can damage the module.
        band = str(c["band"])
        self.profile = resolve(c["hardware"], band)

        # IFAC is ONE setting, not two. Reticulum derives an IFAC identity from
        # EITHER a network name or a passphrase, so a name left behind without
        # its secret yields authentication derived from a publicly known string
        # — worse than no IFAC, because it looks authenticated.
        netname = str(c.get("networkname", "") or "").strip()
        passphrase = str(c.get("passphrase", "") or "").strip()
        if bool(netname) != bool(passphrase):
            have, missing = ("network name", "passphrase") if netname else ("passphrase", "network name")
            raise ValueError(f"IFAC is half configured: {have} is set but {missing} is not. "
                             f"Set both (the passphrase belongs in config/secrets.toml) or neither.")

        self.frequency = int(c["frequency"])
        self.bandwidth = int(c.get("bandwidth", 125000))
        self.sf = int(c.get("spreadingfactor", 8))
        self.cr = int(c.get("codingrate", 5))
        self.txpower = int(c.get("txpower", 14))

        # The operating policy is the CONTROLLER's, not the user's: a value that
        # survived a hand-edit must not widen it. Frequencies are checked against
        # the band actually selected, so a 433 stack cannot transmit on 868.
        power_cap = BAND_POLICY[band][2]
        # One check replaces the old band-range test: a channel is legal only if its
        # complete occupied bandwidth sits inside a permitted segment. The previous
        # range test passed exactly 434.790/870.000 MHz while the segment lookup
        # excluded them, which then fell back to a 100 % duty ceiling.
        ceiling = duty_ceiling(band, self.frequency, self.bandwidth)
        if ceiling is None:
            raise ValueError(
                f"{self.frequency/1e6:.3f} MHz with {self.bandwidth/1e3:.0f} kHz "
                f"bandwidth is not inside a permitted {band} MHz segment "
                f"(alarm-only gaps and band edges are excluded)")
        if self.txpower > power_cap:
            raise ValueError(f"TX power {self.txpower} dBm exceeds the {band} MHz "
                             f"ceiling of {power_cap} dBm")
        # The LONG window carries the legal limit; the short window is our own burst
        # guard and has no statutory ceiling. A hand-edited config must not widen
        # either — 100 % on the long window would have been accepted before.
        for name, limit in (("airtime_limit_short", 100.0),
                            ("airtime_limit_long", ceiling)):
            try:
                value = float(c.get(name, 0))
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a number") from None
            if not 0 < value <= limit:
                raise ValueError(
                    f"{name} must be between 0 and {limit}"
                    + (f" ({self.frequency/1e6:.3f} MHz duty ceiling)"
                       if name == "airtime_limit_long" and ceiling else ""))
        self.syncword = int(str(c.get("syncword", "0x12")), 0)
        self.preamble = int(c.get("preamble", 8))

        # RNS adds the IFAC bytes in Transport.transmit(), AFTER the packet is packed
        # (Transport.py: `raw = ... + ifac`). Advertising the full 255 therefore had a
        # legal 255-byte packet leave as 263 and get dropped here. Reserve the room.
        self.HW_MTU = MAX_PAYLOAD - (self.DEFAULT_IFAC_SIZE if netname else 0)
        self.IN, self.OUT, self.online = True, False, False
        self.bitrate = 0
        self.last_rssi = self.last_snr = None

        state_dir = c.get("state_dir") or "."
        self.duty = DutyAccount(os.path.join(state_dir, "airtime.json"),
                                short_limit=float(c.get("airtime_limit_short", 5.0)),
                                long_limit=float(c.get("airtime_limit_long", 1.0)))

        self._tx_queue = deque()
        self._tx_dropped = 0
        self._tx_oversize = 0
        self._tx_event = threading.Event()
        self._tx_active = threading.Event()
        # Held across a COMPLETE radio operation (poll+read, or the whole TX
        # sequence), not per SPI transfer.
        self._radio_lock = threading.RLock()
        self._run = True
        self.fatal = None                   # set when the runner must exit

        self.bus = SpiBus(c.get("spidev", "/dev/spidev0.0"), self.profile.cs,
                          c.get("gpio_chip", "/dev/gpiochip0"),
                          c.get("runtime_dir"), int(c.get("spi_speed", 2000000)))
        driver = _DRIVERS[self.profile.chip]
        self.radio = driver(self.bus, self.profile, c.get("gpio_chip", "/dev/gpiochip0"))
        self.radio.configure(self.frequency, self.bandwidth, self.sf, self.cr,
                             self.txpower, self.syncword, self.preamble)
        self.radio.start_rx()

        self.bitrate = self.radio.bitrate(self.sf, self.bandwidth, self.cr)
        self.online = True

        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()

        for _note in getattr(self.radio, "notes", ()):
            RNS.log(f"{self} {_note}", RNS.LOG_NOTICE)
        RNS.log(f"{self} online: {self.profile.name} ({self.profile.chip}) on "
                f"{self.bus.path} cs={self.profile.cs} [{self.bus.no_cs_mode}], "
                f"{self.frequency/1e6:.3f} MHz SF{self.sf} "
                f"BW{self.bandwidth/1000:.0f}k CR4/{self.cr} {self.txpower} dBm, "
                f"{self.bitrate} bps", RNS.LOG_NOTICE)

    # -- RNS contract -------------------------------------------------------

    def process_incoming(self, data):
        self.rxb += len(data)
        self.owner.inbound(data, self)

    def process_outgoing(self, data):
        if not self.online:
            return
        if len(data) > MAX_PAYLOAD:
            # RNS packs NON-LINK packets against its global 500-byte MTU and appends
            # the IFAC afterwards, so an oversized announce or direct packet can still
            # arrive here. A LoRa payload physically cannot exceed MAX_PAYLOAD and this
            # driver does not fragment, so the packet is dropped — but never silently:
            # the count makes a systematic problem visible instead of "the link is
            # flaky". Fragmentation is the real fix and is not in this release.
            self._tx_oversize += 1
            RNS.log(f"{self} dropping {len(data)} bytes, over the {MAX_PAYLOAD} byte "
                    f"LoRa limit ({self._tx_oversize} oversize drops so far; "
                    f"link MTU is {self.HW_MTU})", RNS.LOG_ERROR)
            return
        if len(self._tx_queue) >= self.MAX_QUEUE_PACKETS:
            self._tx_dropped += 1
            RNS.log(f"{self} outbound queue full ({self.MAX_QUEUE_PACKETS} packets) — "
                    f"dropping packet, {self._tx_dropped} dropped in total",
                    RNS.LOG_ERROR)
            return
        self._tx_queue.append((data, time.time()))
        self._tx_event.set()

    def detach(self):
        # Order matters: clear _run first so a loop that trips over its own
        # released line does not report a shutdown as a hardware fault.
        self._run = False
        self._tx_event.set()
        self.online = False
        time.sleep(0.15)              # let the loops observe it before teardown
        try:
            self.radio.close()
            self.bus.close()
        except Exception:
            pass

    def __str__(self):
        return f"LoRaSPIInterface[{self.name}]"

    # -- loops --------------------------------------------------------------

    def _fail(self, exc):
        """A bus/lock/radio fault is not recoverable here. Record it, stop BOTH
        worker loops and go offline so the service runner exits non-zero — RNS
        itself would happily keep running without us.

        A fault raised while we are already shutting down is not a fault: the
        loops see their own released lines. Reporting those as CRITICAL would
        bury a real one in noise.
        """
        if not self._run:
            return
        self._run = False
        self._tx_event.set()          # wake the TX worker so it can exit
        self.online = False
        self.fatal = exc
        RNS.log(f"{self} FATAL: {exc}", RNS.LOG_CRITICAL)

    def _rx_loop(self):
        while self._run:
            try:
                if self._tx_active.is_set():
                    time.sleep(0.005)
                    continue
                self.radio.wait_irq(0.1)          # bounded; flags stay authoritative
                with self._radio_lock:
                    if self._tx_active.is_set():
                        continue          # TX took the radio while we waited
                    got = self.radio.poll_rx()
                if got:
                    data, rssi, snr = got
                    self.last_rssi, self.last_snr = rssi, snr
                    RNS.log(f"{self} RX {len(data)} B  RSSI {rssi:.0f} dBm  "
                            f"SNR {snr:.2f} dB", RNS.LOG_DEBUG)
                    self.process_incoming(data)
            except (SpiBusError, RadioError) as exc:
                self._fail(exc)
                return
            except Exception as exc:
                if self._run:
                    RNS.log(f"{self} RX loop error: {exc}", RNS.LOG_ERROR)
                    time.sleep(0.5)

    def _tx_loop(self):
        while self._run:
            self._tx_event.wait(timeout=1.0)
            self._tx_event.clear()
            while self._tx_queue and self._run:
                data, queued_at = self._tx_queue.popleft()
                try:
                    self._transmit(data, queued_at)
                except (SpiBusError, RadioError) as exc:
                    self._fail(exc)
                    return
                except DutyError as exc:
                    RNS.log(f"{self} TX blocked: {exc}", RNS.LOG_ERROR)
                except Exception as exc:
                    RNS.log(f"{self} TX error: {exc}", RNS.LOG_ERROR)

    def _transmit(self, data, queued_at):
        toa = self.radio.time_on_air(len(data), self.sf, self.bandwidth,
                                     self.cr, self.preamble)
        # Reserved BEFORE keying up and persisted, so a crash or restart cannot
        # wipe the hour's accounting.
        while True:
            ok, why = self.duty.reserve(toa)
            if ok:
                break
            if time.time() - queued_at > self.MAX_DUTY_HOLD:
                RNS.log(f"{self} dropped {len(data)} bytes: {why}", RNS.LOG_WARNING)
                return
            time.sleep(0.25)

        with self._radio_lock:
            self._tx_active.set()
        try:
            if not self.radio.transmit(data, max(5.0, toa * 4)):
                # Counted as transmitted: we cannot prove nothing was radiated.
                RNS.log(f"{self} TX did not confirm within the window "
                        f"(airtime still charged)", RNS.LOG_ERROR)
            self.txb += len(data)
            with self._radio_lock:
                self.radio.start_rx()
        finally:
            self._tx_active.clear()
