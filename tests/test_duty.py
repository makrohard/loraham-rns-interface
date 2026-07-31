"""Duty accounting must survive a restart and must not let two senders both
pass the check — otherwise the 1 %/hour 868 limit is not actually enforced."""

import json
import threading

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
