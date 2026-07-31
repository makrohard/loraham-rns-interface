"""Restart-safe, atomically-reserved airtime accounting.

An in-memory limiter is bypassed by a restart or a crash loop, so the 1 %/hour
868 SRD limit would not actually be enforced. Reservations are therefore taken
BEFORE the transmission starts, under an exclusive lock on the state file, and
persist across restarts.

Fail-closed: unreadable or corrupt state blocks TX. RX is never affected.
"""

import errno
import fcntl
import json
import math
import os
import stat
import time

LOCK_TIMEOUT_S = 2.0      # matches the SPI bus lock: a longer wait is a fault
CLOCK_SKEW_S = 60.0       # tolerated wall-clock jitter before a stamp reads as bogus


def _boot_id():
    """Identifies THIS boot. Monotonic time is only comparable within one boot."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class DutyError(RuntimeError):
    pass


class DutyAccount:
    """Airtime ledger over a short and a long window, persisted to disk."""

    def __init__(self, path, short_window=15.0, short_limit=5.0,
                 long_window=3600.0, long_limit=1.0):
        self.path = str(path)
        self.short_window, self.short_limit = float(short_window), float(short_limit)
        self.long_window, self.long_limit = float(long_window), float(long_limit)

    # -- state file ---------------------------------------------------------

    def _open_locked(self, create=True):
        """Lock a SEPARATE lock file, not the ledger itself.

        The ledger is replaced by rename, so a lock held on its inode would be
        dropped the moment a new file took its place — two nodes could then both
        believe they held it. The lock file is never replaced.

        Returns (fd, first_use). `first_use` is LOCAL to this operation: shared
        instance state could be flipped by a concurrent first access between
        locking and reading. `create=False` never initialises anything, so a
        read-only caller cannot consume the one-shot first-use transition.
        O_NOFOLLOW: never traverse a symlink planted where our state belongs.
        """
        lock = self.path + ".lock"
        first_use = False
        try:
            fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            if not create:
                raise
            try:
                fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                first_use = True
            except FileExistsError:        # lost the race — the winner initialised
                fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    # Unbounded waiting here stalls the TX worker indefinitely; a peer
                    # holding this lock for seconds is a fault, not congestion.
                    raise DutyError(
                        f"airtime lock {self.path}.lock busy for more than "
                        f"{LOCK_TIMEOUT_S:.0f}s — refusing to transmit")
                time.sleep(0.02)
        return fd, first_use

    def _read(self, fd, first_use=False):
        """Read the ledger. `fd` is the LOCK file — the ledger is opened by path,
        which is safe because we hold the lock."""
        try:
            lfd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                st = os.fstat(lfd)
                if not stat.S_ISREG(st.st_mode):
                    raise DutyError(f"airtime state {self.path} is not a regular file")
                with os.fdopen(os.dup(lfd), "rb") as fh:
                    raw = fh.read(1 << 20)
            finally:
                os.close(lfd)
        except FileNotFoundError:
            if first_use:
                return []                  # genuinely fresh: we created the lock
            raise DutyError(f"airtime state {self.path} has disappeared — refusing "
                            f"to assume an empty airtime budget") from None
        except OSError as exc:
            raise DutyError(f"airtime state {self.path} is unreadable: {exc}") from None
        if not raw.strip():
            # An EMPTY ledger is not an empty account: writes are atomic, so the
            # file is either absent or complete. Zero bytes means a truncated
            # write from an older build or a damaged filesystem — treating it as
            # "nothing transmitted" would silently reset the hourly budget.
            raise DutyError(f"airtime state {self.path} is empty — refusing to "
                            f"assume an empty airtime budget")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError(f"unsupported ledger version {data!r:.40}")
            raw_entries = data["entries"]
            if not isinstance(raw_entries, list):
                raise ValueError("entries is not a list")
            if not raw_entries and not (first_use or data.get("initialised") is True):
                # Apart from the initialisation write (which carries `initialised`),
                # every write appends before persisting, so the writer never produces
                # an empty list. Valid JSON with a bare `entries: []` therefore means
                # the budget was reset behind us.
                raise ValueError("initialised ledger has no entries")
            entries = []
            for item in raw_entries:
                # A string like "11" iterates into two floats and a dict iterates
                # into its keys — both used to pass as a plausible entry.
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    raise ValueError(f"entry is not a [time, airtime] pair: {item!r}")
                if any(isinstance(x, bool) or not isinstance(x, (int, float))
                       for x in item):
                    raise ValueError(f"entry is not numeric: {item!r}")
                t, a = float(item[0]), float(item[1])
                # A negative or NaN airtime would SUBTRACT from usage, or make every
                # limit comparison false — valid JSON that silently voids the budget.
                if not (math.isfinite(t) and math.isfinite(a)) or a <= 0:
                    raise ValueError(f"implausible entry {item!r}")
                # A timestamp in the future means the clock moved backwards since the
                # entry was written (an NTP correction on an RTC-less Pi). The window
                # arithmetic is then meaningless, so refuse rather than under-account.
                if t > time.time() + CLOCK_SKEW_S:
                    raise ValueError(f"entry timestamped {t - time.time():.0f}s in the "
                                     f"future — clock discontinuity")
                entries.append((t, a))
        except Exception as exc:
            # Corrupt ledger: we cannot prove we are inside the limit.
            raise DutyError(f"airtime state {self.path} is unreadable: {exc}") from None
        rebased = self._rebase(entries, data.get("clock"))
        # A rebase must be applied ONCE and persisted. Recomputing it on every read
        # shifted the same entries forward again and again, so they never aged out of
        # the window and the radio stayed duty-blocked forever after a reboot.
        self._pending_rebase = rebased is not entries
        return rebased

    def _rebase(self, entries, clock):
        """Correct entry timestamps for a wall-clock step within the same boot.

        Elapsed monotonic time is the truth; wall time is not. If the wall clock moved
        by more than the monotonic clock did, the difference is a step (an NTP
        correction) and every entry is that much too old — without this, an hour of
        recorded airtime vanished the moment the Pi got the right time, handing back a
        full legal budget. Across a reboot monotonic is incomparable, so the stamps are
        taken at face value (the ledger is then at worst conservative).
        """
        if not entries or not isinstance(clock, dict):
            return entries
        if clock.get("boot") != _boot_id():
            # DIFFERENT BOOT: monotonic time is not comparable, so we cannot know how
            # much real time passed. Taking the stamps at face value is fail-OPEN — an
            # RTC-less Pi that gets the right time after boot would age every recorded
            # transmission out of the window at once and hand back the full budget.
            # Assume instead that none of the gap was real: shift the entries forward so
            # the newest sits at "now". Worst case that holds TX back for up to one
            # window after a reboot, which is the safe direction.
            try:
                wall_then = float(clock["wall"])
            except (KeyError, TypeError, ValueError):
                return entries
            step = time.time() - wall_then
            if math.isfinite(step) and step > CLOCK_SKEW_S:
                return [(t + step, a) for t, a in entries]
            return entries
        try:
            mono_then, wall_then = float(clock["monotonic"]), float(clock["wall"])
        except (KeyError, TypeError, ValueError):
            return entries
        if not (math.isfinite(mono_then) and math.isfinite(wall_then)):
            return entries
        step = (time.time() - wall_then) - (time.monotonic() - mono_then)
        if abs(step) <= CLOCK_SKEW_S:
            return entries                      # ordinary drift, not a step
        return [(t + step, a) for t, a in entries]

    def _write(self, fd, entries):
        """Replace the ledger atomically: temp file -> fsync -> rename -> dir fsync.

        Rewriting in place meant a crash between ftruncate() and write() left a
        zero-byte ledger, which read back as "no airtime used" — the accounting
        reset itself exactly when it mattered.
        """
        # `initialised` distinguishes the one legitimate empty ledger — written when the
        # state dir is first used, BEFORE any limit decision — from a budget wiped by
        # someone else. Without it a refused first packet left no ledger at all and
        # every later reservation refused TX forever.
        # Clock evidence: wall time is not trustworthy on an RTC-less Pi, where NTP can
        # step it FORWARD by hours after boot. Recording the monotonic clock (comparable
        # only within one boot) lets the reader detect that step and rebase, instead of
        # every recorded transmission silently ageing out of the 1 h window at once.
        blob = json.dumps({"version": 1, "initialised": True,
                           "clock": {"boot": _boot_id(),
                                     "monotonic": time.monotonic(),
                                     "wall": time.time()},
                           "entries": entries}).encode()
        directory = os.path.dirname(self.path) or "."
        tmp = f"{self.path}.tmp.{os.getpid()}"
        try:
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                written = 0
                while written < len(blob):  # os.write may be partial
                    written += os.write(tfd, blob[written:])
                os.fsync(tfd)              # data on disk BEFORE it becomes the ledger
            finally:
                os.close(tfd)
            os.rename(tmp, self.path)      # atomic: readers see old or new, never partial
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)              # persist the rename itself
            finally:
                os.close(dfd)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise DutyError(f"airtime state {self.path} could not be written: {exc}") from None

    # -- accounting ---------------------------------------------------------

    def used(self, entries, window, now):
        return sum(a for t, a in entries if t > now - window)

    def reserve(self, airtime):
        """Reserve `airtime` seconds, or return why it cannot be granted.

        Read, prune, check BOTH windows and persist happen under one exclusive
        lock so two senders can never both pass the check.
        Returns (True, "") or (False, reason).
        """
        fd, first_use = self._open_locked()
        try:
            now = time.time()
            entries = [e for e in self._read(fd, first_use)
                       if e[0] > now - self.long_window]
            if first_use:
                # Persist the empty ledger BEFORE the limit decision. A refused first
                # packet used to return here having created only the lock, and every
                # later reservation then read "lock present, ledger gone" and refused
                # TX forever — one over-long first packet bricked the radio.
                self._write(fd, entries)
            short_pct = (self.used(entries, self.short_window, now) + airtime) / self.short_window * 100.0
            long_pct = (self.used(entries, self.long_window, now) + airtime) / self.long_window * 100.0
            # The short window is OUR burst guard, not the legal limit (that is the long
            # one, enforced below). It must never make transmission impossible: a single
            # 188-byte announce is 6.3 % of a 15 s window at SF9 and 11.5 % at SF10, so a
            # 5 % guard refused every packet outright at SF9+ — the stack offers SF7-12
            # and could not send one frame on most of them. Rate-limit BURSTS: when the
            # window holds nothing, the first packet always goes.
            if getattr(self, "_pending_rebase", False):
                # Persist the corrected stamps NOW, whatever the verdict below: a refused
                # reservation must not leave the shift to be recomputed on the next read.
                # Recomputing it shifted the same entries forward again and again, so they
                # never aged out of the window and the radio stayed duty-blocked forever
                # after a reboot.
                self._write(fd, entries)
                self._pending_rebase = False
            window_used = self.used(entries, self.short_window, now)
            if window_used > 0 and short_pct > self.short_limit:
                return False, f"short-term airtime {short_pct:.2f}% > {self.short_limit:.2f}%"
            if long_pct > self.long_limit:
                return False, f"long-term airtime {long_pct:.3f}% > {self.long_limit:.3f}%"
            # Persisted BEFORE the transmission begins: a crash or timeout after
            # this point stays counted, which is the conservative direction.
            entries.append((now, float(airtime)))
            self._write(fd, entries)
            return True, ""
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                raise DutyError(f"airtime lock could not be released: {exc}") from None
            finally:
                os.close(fd)

    def usage(self):
        """(short %, long %) for status. Never raises on a missing file."""
        try:
            # create=False: a status read must never consume the first-use
            # transition, which would leave the next reserve() seeing a lock with
            # no ledger and blocking the very first transmission.
            fd, _ = self._open_locked(create=False)
        except OSError:
            return 0.0, 0.0                # never used, or unreadable state dir
        try:
            now = time.time()
            entries = self._read(fd)
            return (self.used(entries, self.short_window, now) / self.short_window * 100.0,
                    self.used(entries, self.long_window, now) / self.long_window * 100.0)
        except DutyError:
            return float("nan"), float("nan")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                raise DutyError(f"airtime lock could not be released: {exc}") from None
            finally:
                os.close(fd)
