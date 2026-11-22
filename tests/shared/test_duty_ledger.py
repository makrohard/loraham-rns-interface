"""The airtime ledger: replaced atomically, validated semantically, and an empty, missing,
implausible or clock-shifted one blocks TX rather than handing back the budget; the short
window is a burst guard, the long window the legal limit."""

import json
import os

import pytest

from loraham_rns.duty import DutyAccount, DutyError


def _acct(tmp_path, **kw):
    return DutyAccount(str(tmp_path / "airtime.json"), **kw)


def test_a_missing_ledger_is_a_fresh_account(tmp_path):
    assert _acct(tmp_path).usage() is not None


def test_an_empty_ledger_blocks_tx_instead_of_resetting_the_budget(tmp_path):
    """The failure this replaces: a truncated write read back as a zero-byte file
    and was interpreted as "nothing transmitted", handing back a full hourly
    budget. It must fail CLOSED — no reservation may be granted."""
    path = tmp_path / "airtime.json"
    path.write_bytes(b"")
    acct = _acct(tmp_path)
    with pytest.raises(DutyError, match="empty"):
        acct.reserve(0.01)
    # `usage()` is contracted never to raise; it must still not claim 0 % used.
    short, long = acct.usage()
    assert short != short and long != long, "corrupt ledger reported as usable"


def test_the_ledger_is_replaced_not_rewritten_in_place(tmp_path):
    acct = _acct(tmp_path, long_limit=100.0)
    acct.reserve(0.05)
    path = tmp_path / "airtime.json"
    first = path.stat().st_ino
    acct.reserve(0.05)
    assert path.stat().st_ino != first, "ledger was rewritten in place, not renamed"
    assert json.loads(path.read_bytes())["entries"]


def test_no_temp_files_are_left_behind(tmp_path):
    acct = _acct(tmp_path, long_limit=100.0)
    for _ in range(3):
        acct.reserve(0.01)
    leftovers = [n for n in os.listdir(tmp_path) if ".tmp." in n]
    assert not leftovers, leftovers


def test_the_lock_is_a_separate_file_from_the_ledger(tmp_path):
    """A lock on the ledger's own inode is lost the moment a rename replaces it."""
    acct = _acct(tmp_path, long_limit=100.0)
    acct.reserve(0.01)
    assert (tmp_path / "airtime.json.lock").exists()


@pytest.mark.parametrize("entries", [
    [[1e9, -5.0]],                       # negative airtime SUBTRACTS from usage
    [[1e9, float("nan")]],               # NaN makes every comparison false
    [[1e9, float("inf")]],
    [[float("nan"), 1.0]],
    [[1e9, 0.0]],                        # zero-length transmission is not real
])
def test_implausible_entries_block_tx(tmp_path, entries):
    path = tmp_path / "airtime.json"
    path.write_text(json.dumps({"version": 1, "entries": entries},
                               allow_nan=True))
    (tmp_path / "airtime.json.lock").write_bytes(b"")
    with pytest.raises(DutyError):
        _acct(tmp_path).reserve(0.01)


def test_an_unknown_schema_version_blocks_tx(tmp_path):
    path = tmp_path / "airtime.json"
    path.write_text(json.dumps({"version": 2, "entries": []}))
    (tmp_path / "airtime.json.lock").write_bytes(b"")
    with pytest.raises(DutyError, match="unreadable"):
        _acct(tmp_path).reserve(0.01)


def test_a_ledger_that_disappears_after_use_fails_closed(tmp_path):
    """Deleting airtime.json used to restore a full hourly budget. The lock file
    persists, so we can tell 'never used' from 'lost'."""
    acct = _acct(tmp_path, long_limit=100.0)
    acct.reserve(0.01)                      # initialises the state dir
    (tmp_path / "airtime.json").unlink()
    with pytest.raises(DutyError, match="disappeared"):
        acct.reserve(0.01)


def test_a_status_read_does_not_block_the_first_transmission(tmp_path):
    """usage() used to create the lock without a ledger; the next reserve() then
    saw a lock with no ledger, called it lost, and blocked every first TX."""
    acct = _acct(tmp_path, long_limit=100.0)
    assert acct.usage() == (0.0, 0.0)
    ok, why = acct.reserve(0.01)
    assert ok, why


def test_concurrent_first_access_still_permits_one_transmission(tmp_path):
    """first-use was shared instance state: a second account object could flip it
    between locking and reading."""
    a, b = _acct(tmp_path, long_limit=100.0), _acct(tmp_path, long_limit=100.0)
    b.usage()
    ok, why = a.reserve(0.01)
    assert ok, why
    ok, why = b.reserve(0.01)
    assert ok, why


def test_an_initialised_ledger_with_no_entries_is_corrupt(tmp_path):
    """The writer always appends before persisting, so `entries: []` after
    initialisation means someone reset the budget."""
    acct = _acct(tmp_path, long_limit=100.0)
    acct.reserve(0.01)
    (tmp_path / "airtime.json").write_text(json.dumps({"version": 1, "entries": []}))
    with pytest.raises(DutyError):
        acct.reserve(0.01)


@pytest.mark.parametrize("entries", [
    ["11"],                              # a 2-char string iterates into two floats
    [{"a": 1, "b": 2}],                  # a dict iterates into its keys
    [[1e9]],                             # wrong arity
    [[1e9, 1.0, 3.0]],
    [[True, True]],                      # bools are ints in Python
    ["not a pair"],
])
def test_structurally_wrong_entries_block_tx(tmp_path, entries):
    acct = _acct(tmp_path, long_limit=100.0)
    acct.reserve(0.01)                     # initialise
    (tmp_path / "airtime.json").write_text(
        json.dumps({"version": 1, "entries": entries}))
    with pytest.raises(DutyError):
        acct.reserve(0.01)


def test_a_refused_first_packet_does_not_brick_the_radio(tmp_path):
    """The lock was created before the ledger, so a first packet over the duty limit
    returned having created only the lock — every later reservation then read
    "lock present, ledger gone" and refused TX permanently."""
    strict = _acct(tmp_path, long_limit=0.001)
    ok, _ = strict.reserve(5.0)
    assert not ok
    assert (tmp_path / "airtime.json").exists(), "ledger must exist after a refusal"
    ok2, why = _acct(tmp_path, long_limit=100.0).reserve(0.001)
    assert ok2, why


def test_a_future_timestamp_is_refused(tmp_path):
    """A stamp ahead of now means the clock moved backwards since it was written (NTP
    on an RTC-less Pi); the window arithmetic is then meaningless."""
    acct = _acct(tmp_path, long_limit=100.0)
    acct.reserve(0.01)
    future = 4_102_444_800.0                    # 2100-01-01 UTC: ahead of any real clock
    (tmp_path / "airtime.json").write_text(json.dumps(
        {"version": 1, "initialised": True, "entries": [[future, 1.0]]}))
    with pytest.raises(DutyError):
        acct.reserve(0.01)


def test_a_forward_clock_step_does_not_hand_back_the_budget(tmp_path, monkeypatch):
    """An RTC-less Pi gets the right time from NTP minutes after boot. Pruning purely
    on wall-clock stamps made every transmission recorded before that step instantly
    older than the 1 h window — the legal budget reset itself exactly once per boot."""
    import json as _json

    acct = _acct(tmp_path, long_limit=100.0, short_limit=100.0)
    acct.reserve(1.0)                                  # recorded before the step
    used_before = acct.usage()[1]
    assert used_before > 0

    raw = _json.loads((tmp_path / "airtime.json").read_text())
    # Simulate the wall clock jumping 2 h forward while monotonic advanced ~0 s.
    raw["clock"]["wall"] -= 7200
    (tmp_path / "airtime.json").write_text(_json.dumps(raw))

    used_after = _acct(tmp_path, long_limit=100.0, short_limit=100.0).usage()[1]
    assert used_after == pytest.approx(used_before, rel=0.05), \
        "airtime must survive a forward clock step, not age out of the window"


def test_a_cross_boot_clock_step_does_not_reset_the_budget(tmp_path):
    """Across a reboot monotonic time is not comparable, so the elapsed real time is
    unknowable. Taking the wall stamps at face value was fail-OPEN: an RTC-less Pi
    that gets the right time after boot aged every recorded transmission out of the
    1 h window at once. Assume none of the gap was real."""
    import json as _json

    acct = _acct(tmp_path, long_limit=100.0, short_limit=100.0)
    acct.reserve(1.0)
    used_before = acct.usage()[1]
    assert used_before > 0

    raw = _json.loads((tmp_path / "airtime.json").read_text())
    raw["clock"]["boot"] = "00000000-0000-0000-0000-000000000000"   # a previous boot
    raw["clock"]["wall"] -= 7200                                     # clock now 2 h ahead
    raw["entries"] = [[t - 7200, a] for t, a in raw["entries"]]      # stamps are 2 h old
    (tmp_path / "airtime.json").write_text(_json.dumps(raw))

    used_after = _acct(tmp_path, long_limit=100.0, short_limit=100.0).usage()[1]
    assert used_after == pytest.approx(used_before, rel=0.05), \
        "a cross-boot forward step must not expire the accounted airtime"


def test_a_rebase_is_applied_once_not_on_every_read(tmp_path):
    """Recomputing the cross-boot shift on every read moved the same entries forward
    again and again: they never aged out of the window and the radio stayed
    duty-blocked forever after a reboot. Verified on hardware — a Zero 2 W could not
    transmit at all until the ledger was persisted with corrected stamps."""
    import json as _json

    acct = _acct(tmp_path, long_limit=100.0, short_limit=100.0)
    acct.reserve(1.0)
    raw = _json.loads((tmp_path / "airtime.json").read_text())
    raw["clock"]["boot"] = "00000000-0000-0000-0000-000000000000"   # a previous boot
    raw["clock"]["wall"] -= 7200
    raw["entries"] = [[t - 7200, a] for t, a in raw["entries"]]
    (tmp_path / "airtime.json").write_text(_json.dumps(raw))

    a2 = _acct(tmp_path, long_limit=100.0, short_limit=100.0)
    a2.reserve(0.01)                                   # triggers the rebase + persist
    stored = _json.loads((tmp_path / "airtime.json").read_text())
    assert stored["clock"]["boot"] != "00000000-0000-0000-0000-000000000000", \
        "the corrected clock must be persisted"

    # A later read must NOT shift again: usage stays put instead of creeping upward.
    a3 = _acct(tmp_path, long_limit=100.0, short_limit=100.0)
    first = a3.usage()[1]
    second = a3.usage()[1]
    assert first == pytest.approx(second, rel=1e-6), "entries must not be re-shifted"


def test_the_short_guard_never_blocks_a_lone_packet(tmp_path):
    """The short window is a BURST guard, not the legal limit. A single 188-byte
    announce is 6.3 % of a 15 s window at SF9 and 11.5 % at SF10, so the 5 % default
    refused every packet outright at SF9+ — the stack offers SF7-12 and could not send
    one frame on most of them."""
    acct = _acct(tmp_path, short_limit=5.0, long_limit=100.0)
    ok, why = acct.reserve(1.72)                 # SF10 announce: 11.5 % of the window
    assert ok, f"a lone packet must go out: {why}"
    # ...but a BURST is still refused.
    ok2, why2 = acct.reserve(1.72)
    assert not ok2 and "short-term" in why2


def test_the_long_limit_still_applies_to_a_lone_packet(tmp_path):
    """Relaxing the burst guard must not weaken the LEGAL hourly ceiling."""
    acct = _acct(tmp_path, short_limit=100.0, long_limit=0.01)
    ok, why = acct.reserve(5.0)                  # 0.14 % of an hour > 0.01 % limit
    assert not ok and "long-term" in why
