"""Regressions for the audit findings.

  * only PERMITTED SRD segments may be used, the whole occupied bandwidth must fit
    in one of them, and anything else fails closed;
  * the airtime ledger is replaced atomically, validated semantically, and an
    empty/missing/implausible one blocks TX rather than resetting the budget;
  * SF6 is refused by the SX127x driver instead of silently misconfiguring.
"""

import json
import os

import pytest

from loraham_rns.duty import DutyAccount, DutyError
from loraham_rns.interface import PERMITTED_SEGMENTS, duty_ceiling

# ---- permitted segments ---------------------------------------------------

@pytest.mark.parametrize("band,freq,expect", [
    ("433", 433_500_000, 10.0),     # LPD433
    ("868", 864_000_000, 0.1),      # 863-865 is 0.1 %, NOT 1 %
    ("868", 866_000_000, 1.0),
    ("868", 868_500_000, 1.0),      # the channel we actually run on
    ("868", 869_000_000, 0.1),
    ("868", 869_500_000, 10.0),
    ("868", 869_800_000, 1.0),
])
def test_ceiling_follows_the_segment(band, freq, expect):
    assert duty_ceiling(band, freq, 0) == expect


@pytest.mark.parametrize("freq", [868_650_000, 869_300_000, 869_675_000])
def test_alarm_only_gaps_are_refused(freq):
    """868.6-868.7, 869.2-869.4 and 869.65-869.7 MHz carry no generic allocation.
    They must not resolve to any ceiling — the caller refuses on None."""
    assert duty_ceiling("868", freq, 0) is None


def test_the_whole_occupied_bandwidth_must_fit():
    """A 500 kHz channel centred at 869.5 MHz looks legal by centre frequency but
    spills outside the 250 kHz-wide 10 % segment."""
    assert duty_ceiling("868", 869_500_000, 125_000) == 10.0
    assert duty_ceiling("868", 869_500_000, 500_000) is None


@pytest.mark.parametrize("band,freq", [("433", 434_790_000), ("868", 870_000_000)])
def test_a_real_channel_at_the_band_edge_is_refused(band, freq):
    """The old band test accepted exactly these edges while the ceiling lookup
    excluded them, and the mismatch fell back to a 100 % duty limit. Any real
    channel centred on the edge spills over it."""
    assert duty_ceiling(band, freq, 125_000) is None


def test_segments_are_ordered_and_sane():
    """Deliberately NOT contiguous — the alarm-only gaps are legally required."""
    for _band, ranges in PERMITTED_SEGMENTS.items():
        for (lo, hi), pct in ranges:
            assert lo < hi and 0 < pct <= 100
        for ((_, hi), _), ((lo, _), _) in zip(ranges, ranges[1:]):
            assert hi <= lo, "segments must not overlap"


# ---- ledger durability ----------------------------------------------------

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


# ---- SF6 ------------------------------------------------------------------

def test_sx127x_refuses_sf6():
    from loraham_rns.sx127x import SX127x
    radio = SX127x.__new__(SX127x)
    with pytest.raises(ValueError, match="SF7-12"):
        radio.configure(868_500_000, 125_000, 6, 5, 14)


# ---- ledger contents ------------------------------------------------------

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


# ---- outbound queue -------------------------------------------------------

def test_the_outbound_queue_is_bounded():
    """A packet may wait MAX_DUTY_HOLD seconds for duty allowance; unbounded, a
    chatty local client fills memory on a Pi Zero."""
    from collections import deque

    from loraham_rns.interface import LoRaSPIInterface

    iface = LoRaSPIInterface.__new__(LoRaSPIInterface)
    iface._tx_queue, iface._tx_dropped = deque(), 0
    iface._tx_event = type("E", (), {"set": lambda self: None})()
    iface.online = True

    limit = LoRaSPIInterface.MAX_QUEUE_PACKETS
    for _ in range(limit + 10):
        LoRaSPIInterface.process_outgoing(iface, b"x" * 32)
    assert len(iface._tx_queue) == limit
    assert iface._tx_dropped == 10, "dropped packets must be counted, not silent"


# ---- ledger initialisation ------------------------------------------------

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


# ---- MTU contract ---------------------------------------------------------

def test_the_hardware_mtu_is_declared_as_fixed():
    """One RNS packet goes in one LoRa payload (255 B max). RNS's base MTU is 500, and
    it only honours an interface's HW_MTU when FIXED_MTU or AUTOCONFIGURE_MTU is set —
    otherwise links keep the 500 B default and oversized packets were dropped here."""
    from loraham_rns.interface import LoRaSPIInterface
    from loraham_rns.radio import MAX_PAYLOAD

    assert LoRaSPIInterface.FIXED_MTU is True
    assert LoRaSPIInterface.AUTOCONFIGURE_MTU is False, \
        "our limit is the radio FIFO, not the bitrate table"
    assert MAX_PAYLOAD == 255


# ---- audit round 5 --------------------------------------------------------

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
    import time
    acct = _acct(tmp_path, long_limit=100.0)
    acct.reserve(0.01)
    (tmp_path / "airtime.json").write_text(json.dumps(
        {"version": 1, "initialised": True, "entries": [[time.time() + 3600, 1.0]]}))
    with pytest.raises(DutyError):
        acct.reserve(0.01)


def test_the_mtu_leaves_room_for_the_ifac_bytes():
    """RNS appends the IFAC in Transport.transmit(), AFTER packing: advertising the
    full 255 made a legal 255-byte packet leave as 263 and get dropped here."""
    from loraham_rns.interface import LoRaSPIInterface
    from loraham_rns.radio import MAX_PAYLOAD

    assert LoRaSPIInterface.DEFAULT_IFAC_SIZE == 8
    assert MAX_PAYLOAD - LoRaSPIInterface.DEFAULT_IFAC_SIZE == 247


@pytest.mark.parametrize("listen,refused", [
    ("127.0.0.1", False), ("127.0.1.1", False), ("localhost", False), ("::1", False),
    ("localhost.example", True), ("127.0.0.1.evil.net", True),
    ("192.168.1.5", True), ("0.0.0.0", True),
])
def test_only_a_real_loopback_bind_is_accepted(tmp_path, listen, refused):
    """A prefix test accepted `localhost.example` and `127.0.0.1.evil.net`."""
    from loraham_rns.node import _unsafe_exposure

    (tmp_path / "config").write_text(f'listen_ip = {listen}\n')
    msg = _unsafe_exposure(str(tmp_path), "192.168.0.0/24")
    assert bool(msg) is refused, msg


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


# ---- client boundary ------------------------------------------------------

def test_a_client_refuses_to_run_without_the_owner(tmp_path, capsys):
    """Reticulum makes the FIRST process the shared-instance owner, and the clients are
    pointed at the owner's config — which declares the LoRa interface. Started with no
    owner they take the radio."""
    from loraham_rns import client

    marker = tmp_path / "ran"
    rc = client.main(["--configdir", str(tmp_path / "cfg"), "--port", "1",
                      "--", "/bin/sh", "-c", f"touch {marker}"])
    assert rc == 3
    assert not marker.exists(), "the client must not have been executed"
    assert "would become the shared-instance OWNER" in capsys.readouterr().err


def test_a_bare_open_port_is_not_accepted_as_an_instance(tmp_path):
    """An open TCP port is NOT proof. When RNS cannot authenticate against whatever is
    listening it falls back to standalone and loads the LoRa interface — the exact
    takeover this wrapper prevents. Only an authenticated shared instance counts."""
    import socket

    from loraham_rns import client

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    marker = tmp_path / "ran"
    try:
        rc = client.main(["--configdir", str(tmp_path / "cfg"), "--port", str(port),
                          "--", "/bin/sh", "-c", f"touch {marker}"])
    finally:
        srv.close()
    assert rc == 3, "a dumb listener must not satisfy the guard"
    assert not marker.exists()


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


# ---- location plugin -------------------------------------------------------

def _plugin():
    import builtins
    import importlib.util
    builtins.SidebandTelemetryPlugin = type("S", (), {"start": lambda self: None,
                                                      "stop": lambda self: None})
    spec = importlib.util.spec_from_file_location(
        "lhpc_location", pathlib_Path(__file__).parent.parent / "plugins" / "lhpc_location.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


from pathlib import Path as pathlib_Path  # noqa: E402


def test_speeds_are_reported_in_kmh():
    """Sideband's Location sensor expects km/h — its own gpsd plugin multiplies m/s by
    3.6. This plugin passed gpsd m/s straight through and converted NMEA knots to m/s,
    so every speed was wrong by 3.6x / 3.6x1.852."""
    import inspect

    m = _plugin()
    src = inspect.getsource(m)
    assert "spd * 3.6" in src, "gpsd m/s must become km/h"
    assert "speed * 1.852" in src, "NMEA knots must become km/h"
    assert "0.514444" not in src, "the m/s conversion must be gone"


def test_a_stale_fix_is_removed_not_reused():
    """Returning early on an expired fix left the previous coordinates in the sensor —
    a parked position kept looking live."""
    import inspect

    m = _plugin()
    src = inspect.getsource(m.LhpcLocationPlugin.update_telemetry)
    assert "pop(\"location\"" in src
    assert "set_update_time" in src, "freshness must go through set_update_time()"
    assert "stale_time" in src
