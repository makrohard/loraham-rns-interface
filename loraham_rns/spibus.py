"""SPI bus access that participates in the LoRaHAM daemon's fail-closed lock.

loraham_daemon/locking_pihal.h states the contract: "No SPI transfer may proceed
unless the process-shared lock is confirmed held", flock(LOCK_EX) on
<LORAHAM_RUNTIME_DIR>/spi0.lock, per transfer, 2 s bounded, fatal otherwise.
We are a peer on that bus and honour the same rules, so the daemon's invariant
still holds while we run.

One transaction = acquire lock -> assert CS -> transfer -> deassert CS -> release.
The lock is NEVER held across a wait loop: the daemon treats >2 s of contention
as a wedged peer.
"""

import errno
import fcntl
import os
import stat
import time

import gpiod
import spidev
from gpiod.line import Direction, Value

LOCK_TIMEOUT_S = 2.0          # matches LORAHAM_SPI_LOCK_TIMEOUT_MS
LOCK_FILE = "spi0.lock"

# `dtoverlay=spi0-0cs` leaves the controller with an EMPTY cs-gpios property —
# that is the property we verify. Peer processes (the opposite-band daemon) may
# legitimately hold GPIO7/GPIO8 as their own soft chip-selects.


class SpiBusError(RuntimeError):
    """Fatal: the bus cannot be used safely. Never downgrade this to a warning."""


class SpiBus:
    def __init__(self, spidev_path, cs_pin, chip_path, runtime_dir, speed_hz=2_000_000):
        self.path, self.cs_pin, self.speed_hz = str(spidev_path), int(cs_pin), int(speed_hz)
        self.chip_path = str(chip_path)
        self._lock_fd = self._open_lock(runtime_dir)

        bus, dev = self._parse(self.path)
        self._bus, self._dev = bus, dev
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.spi.max_speed_hz = self.speed_hz
        self.spi.mode = 0

        # We drive NSS ourselves (soft CS), active low. Requesting the line via
        # libgpiod also re-muxes it away from the SPI controller's alt function,
        # which is what actually guarantees the kernel cannot assert it.
        self._cs = gpiod.request_lines(
            str(chip_path), consumer="loraham-rns-cs",
            config={self.cs_pin: gpiod.LineSettings(
                direction=Direction.OUTPUT, active_low=True,
                output_value=Value.INACTIVE)})          # idle = NSS released
        self._enforce_no_kernel_cs()

    # -- setup --------------------------------------------------------------

    @staticmethod
    def _parse(path):
        base = os.path.basename(path)
        if not base.startswith("spidev") or "." not in base:
            raise SpiBusError(f"not a spidev path: {path!r}")
        bus, _, dev = base[len("spidev"):].partition(".")
        if not (bus.isdigit() and dev.isdigit()):
            raise SpiBusError(f"not a spidev path: {path!r}")
        return int(bus), int(dev)

    def _open_lock(self, runtime_dir):
        """Open <runtime_dir>/spi0.lock. No /tmp fallback: if the trusted lock
        directory is unusable we refuse to touch the bus, exactly as the daemon
        refuses to start that radio."""
        if not runtime_dir:
            raise SpiBusError("LORAHAM_RUNTIME_DIR is unset — refusing to use the SPI bus")
        try:
            dfd = os.open(str(runtime_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise SpiBusError(f"lock directory {runtime_dir!r} unusable: {exc}") from None
        try:
            st = os.fstat(dfd)
            if not stat.S_ISDIR(st.st_mode):
                raise SpiBusError(f"lock directory {runtime_dir!r} is not a directory")
            if st.st_uid != os.geteuid():
                raise SpiBusError(f"lock directory {runtime_dir!r} is not owned by us")
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise SpiBusError(f"lock directory {runtime_dir!r} is group/world writable")
            try:
                fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=dfd)
            except OSError as exc:
                raise SpiBusError(f"cannot open {runtime_dir}/{LOCK_FILE}: {exc}") from None
            return fd
        finally:
            os.close(dfd)

    def _enforce_no_kernel_cs(self):
        """Guarantee the kernel cannot assert a chip-select in parallel with ours.

        Preferred: the SPI_NO_CS mode bit. RP1's dw_spi_mmio rejects it with
        EINVAL (verified on a Pi 5), so we fall back to the property that
        actually holds on this platform and verify it rather than assume it:
        holding the CS line through libgpiod re-muxes the pin to SIO, so the
        controller's CE output is physically disconnected from it. Under
        `dtoverlay=spi0-0cs` the CS pins are not alt-muxed at all (the kernel
        logs "not valid maps for state default") and a transfer with no CS
        asserted reads back nothing.

        If neither the mode bit nor our GPIO claim can be confirmed, this is
        fatal — we never proceed with an unproven bus.
        """
        try:
            self.spi.no_cs = True
            if self.spi.no_cs:
                self.no_cs_mode = "SPI_NO_CS"
                return
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP):
                raise SpiBusError(f"setting SPI_NO_CS failed: {exc}") from None
        try:
            self._cs.get_value(self.cs_pin)          # proves we hold the line
        except Exception as exc:
            raise SpiBusError(
                "SPI_NO_CS is unsupported by this controller and the chip-select "
                f"line could not be confirmed held: {exc}") from None
        self._assert_no_kernel_cs_pins()
        self.no_cs_mode = "gpio-claim"

    def _assert_no_kernel_cs_pins(self):
        """Prove the CONTROLLER has no chip-selects, without touching peers' lines.

        The previous check required the bus's other native CS GPIO to be claimable.
        That is wrong here: the cooperating opposite-band LoRaHAM daemon holds its
        own soft-CS line for its whole lifetime (RadioLib's Pi HAL claims outputs
        persistently), so `daemon 433 + reticulum 868` — the combination the
        manifest declares cooperative — was rejected as if the kernel owned the pin.

        The honest evidence is the controller's own configuration: under
        `dtoverlay=spi0-0cs` its `cs-gpios` property is EMPTY, i.e. it has no
        chip-select to assert. A peer process holding GPIO7 or GPIO8 is then
        irrelevant — both of us drive our own NSS and serialise on spi0.lock.
        """
        try:
            controller = os.path.realpath(
                f"/sys/class/spidev/spidev{self._bus}.{self._dev}/device/..")
            cs_gpios = os.path.join(controller, "of_node", "cs-gpios")
            size = os.stat(cs_gpios).st_size
        except OSError as exc:
            raise SpiBusError(
                "SPI_NO_CS is unsupported and the controller's chip-select "
                f"configuration could not be read ({exc}) — refusing an unproven "
                "bus. Boot with dtoverlay=spi0-0cs.") from None
        if size:
            raise SpiBusError(
                f"the SPI controller still declares {size // 12} hardware "
                "chip-select(s); it can assert one during our transfers. Boot with "
                "dtoverlay=spi0-0cs (SPI_NO_CS is unsupported by this controller).")

    # -- transactions -------------------------------------------------------

    def _acquire(self):
        """Bounded exclusive acquisition. A timeout means a wedged peer, and is
        fatal — the same conclusion the daemon draws."""
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EINTR):
                    raise SpiBusError(f"SPI bus lock failed: {exc}") from None
                if time.monotonic() >= deadline:
                    raise SpiBusError(
                        f"SPI bus lock not acquired within {LOCK_TIMEOUT_S:.0f}s — "
                        "a peer holding the shared bus is wedged") from None

    def xfer(self, payload):
        """One complete hardware transaction, lock held throughout.

        Every spidev/libgpiod failure is raised as SpiBusError so the caller
        cannot mistake a dead bus for a transient error and keep running: the
        interface treats SpiBusError as fatal and goes offline.
        """
        self._acquire()
        try:
            try:
                self._cs.set_value(self.cs_pin, Value.ACTIVE)   # active_low -> NSS low
            except Exception as exc:
                raise SpiBusError(f"chip-select assert failed: {exc}") from None
            try:
                return self.spi.xfer2(list(payload))
            except Exception as exc:
                raise SpiBusError(f"SPI transfer failed: {exc}") from None
            finally:
                try:
                    self._cs.set_value(self.cs_pin, Value.INACTIVE)
                except Exception as exc:
                    raise SpiBusError(f"chip-select release failed: {exc}") from None
        finally:
            # A failed release leaves the daemon's bus lock held forever; that is a
            # wedged bus, not a warning.
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError as exc:
                raise SpiBusError(f"releasing the SPI lock failed: {exc}") from None

    def close(self):
        for shut in (lambda: self.spi.close(),
                     lambda: self._cs.release(),
                     lambda: os.close(self._lock_fd)):
            try:
                shut()
            except Exception:
                pass
