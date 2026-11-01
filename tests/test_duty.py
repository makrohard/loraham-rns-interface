"""Duty accounting must survive a restart and must not let two senders both
pass the check — otherwise the 1 %/hour 868 limit is not actually enforced."""

import json
import threading
import time as time_mod
import types as types_mod

import pytest

from loraham_rns.duty import DutyAccount, DutyError


def acct(tmp_path, **kw):
    return DutyAccount(tmp_path / "airtime.json", **kw)


def test_reservation_survives_a_restart(tmp_path):
    # short_limit raised out of the way: this pins the LONG window specifically.
    a = acct(tmp_path, long_limit=1.0, short_limit=10_000.0)
    assert a.reserve(30.0)[0]                      # 30s/3600s = 0.83%
    # A *new* object is what a restarted process sees.
    b = acct(tmp_path, long_limit=1.0, short_limit=10_000.0)
    ok, why = b.reserve(30.0)                      # would total 1.67%
    assert not ok and "long-term" in why


def test_short_window_blocks_a_burst(tmp_path):
    a = acct(tmp_path, short_limit=5.0)
    assert a.reserve(0.7)[0]                       # 0.7/15 = 4.7%
    ok, why = a.reserve(0.7)
    assert not ok and "short-term" in why


def test_corrupt_state_fails_closed_for_tx(tmp_path):
    (tmp_path / "airtime.json").write_text("{not json")
    with pytest.raises(DutyError):
        acct(tmp_path).reserve(0.1)


def test_concurrent_reservations_do_not_both_pass(tmp_path):
    # One 0.7s slot fits in the 5%/15s window; two must not.
    granted = []
    def go():
        granted.append(acct(tmp_path, short_limit=5.0).reserve(0.7)[0])
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(granted) == 1, f"expected exactly one grant, got {sum(granted)}"


def test_ledger_is_json_and_owner_only(tmp_path):
    a = acct(tmp_path)
    a.reserve(0.1)
    data = json.loads((tmp_path / "airtime.json").read_text())
    assert data["version"] == 1 and len(data["entries"]) == 1
    assert (tmp_path / "airtime.json").stat().st_mode & 0o077 == 0


def test_a_caller_that_wins_the_lock_in_the_creators_gap_waits_for_the_ledger(tmp_path, monkeypatch):
    """The creator makes the lock file (O_EXCL) and only then takes the flock. A second
    account that finds the file can win the flock in that gap, see no ledger yet and hit
    the by-design 'has disappeared' refusal — an exception escaping a worker thread.

    Driven through reserve() only, ordered by events, no timing:
    * the creator is held at its first flock until the late caller has shown what it
      does — taken the lock (the old code) or started waiting for the ledger (the fix);
    * the late caller's monotonic clock stands still, so its bounded wait cannot run out
      while the creator is being scheduled.
    SAFETY_S bounds only a hang (a bug), never an ordinary pass."""
    from loraham_rns import duty as duty_mod

    SAFETY_S = 30.0
    real_fcntl, real_time = duty_mod.fcntl, duty_mod.time
    creator_in_gap = threading.Event()
    late_has_acted = threading.Event()
    threads = {}

    def is_creator():
        return threading.current_thread() is threads.get("creator")

    def flock(fd, op):
        if is_creator() and op & real_fcntl.LOCK_EX and not creator_in_gap.is_set():
            creator_in_gap.set()                     # lock file made, ledger not yet
            assert late_has_acted.wait(SAFETY_S), "the late caller never acted"
            return real_fcntl.flock(fd, op)
        result = real_fcntl.flock(fd, op)            # raises if not acquired
        if not is_creator() and op & real_fcntl.LOCK_EX:
            late_has_acted.set()                     # the late caller holds the lock
        return result

    def sleep(seconds):
        if not is_creator():
            late_has_acted.set()                     # the late caller is waiting
        real_time.sleep(seconds)

    def monotonic():
        return 0.0 if not is_creator() else real_time.monotonic()

    fcntl_shim = types_mod.SimpleNamespace(**{k: getattr(real_fcntl, k) for k in dir(real_fcntl)
                                              if not k.startswith("__")})
    fcntl_shim.flock = flock
    time_shim = types_mod.SimpleNamespace(**{k: getattr(real_time, k) for k in dir(real_time)
                                             if not k.startswith("__")})
    time_shim.sleep, time_shim.monotonic = sleep, monotonic
    monkeypatch.setattr(duty_mod, "fcntl", fcntl_shim)
    monkeypatch.setattr(duty_mod, "time", time_shim)

    path = tmp_path / "airtime.json"
    results = {}

    def run(name):
        try:
            results[name] = DutyAccount(path, short_limit=100.0).reserve(0.01)
        except Exception as exc:                     # reported, not lost in the thread
            results[name] = exc

    threads["creator"] = threading.Thread(target=run, args=("creator",))
    threads["creator"].start()
    assert creator_in_gap.wait(SAFETY_S), "the creator never reached its gap"
    assert not path.exists(), "not in the creator's gap"
    threads["late"] = threading.Thread(target=run, args=("late",))
    threads["late"].start()
    for t in threads.values():
        t.join(SAFETY_S)
        assert not t.is_alive(), "hung"
    assert results["creator"] == (True, "")
    assert results["late"] == (True, ""), results["late"]   # DutyError "has disappeared" before the fix


class _FakeClock:
    """time for loraham_rns.duty with a virtual monotonic clock: sleep() advances it
    and records the total, so the wait's bound is measured without wall time."""

    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def module(self):
        shim = types_mod.SimpleNamespace(**{k: getattr(time_mod, k) for k in dir(time_mod)
                                            if not k.startswith("__")})
        shim.monotonic = lambda: self.now
        shim.sleep = self._sleep
        return shim

    def _sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


def test_a_wiped_ledger_still_refuses_after_one_lock_timeout(tmp_path, monkeypatch):
    """Lock file present, ledger gone and never coming back: the wait for the creator
    ends at LOCK_TIMEOUT_S and the old refusal follows — bounded, not a hang."""
    from loraham_rns import duty as duty_mod

    a = acct(tmp_path, short_limit=100.0)
    assert a.reserve(0.01)[0]
    (tmp_path / "airtime.json").unlink()
    clock = _FakeClock()
    monkeypatch.setattr(duty_mod, "time", clock.module())
    with pytest.raises(DutyError, match="disappeared"):
        acct(tmp_path, short_limit=100.0).reserve(0.01)
    assert duty_mod.LOCK_TIMEOUT_S <= clock.slept < duty_mod.LOCK_TIMEOUT_S + 0.1


def test_a_status_read_never_waits_for_a_missing_ledger(tmp_path, monkeypatch):
    """usage() opens with create=False: it never initialises and must not wait either —
    a status page would stall for the lock timeout on a wiped ledger."""
    import math

    from loraham_rns import duty as duty_mod

    a = acct(tmp_path, short_limit=100.0)
    assert a.reserve(0.01)[0]
    (tmp_path / "airtime.json").unlink()
    clock = _FakeClock()
    monkeypatch.setattr(duty_mod, "time", clock.module())
    short, long_ = acct(tmp_path, short_limit=100.0).usage()
    assert math.isnan(short) and math.isnan(long_)
    assert clock.slept == 0.0
