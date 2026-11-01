# Carried to v0.4.0

Items found while preparing 0.3.1 that need a behaviour change, so they wait for
v0.4.0 (the daemon backend, which replaces the client/instance path below).

## 1. loraham-rns-client: prove an authenticated instance, not a listener
- Measured with rns 1.5.7: `RNS.Reticulum(configdir, require_shared_instance=True)`
  followed by `is_connected_to_shared_instance` returns True when a plain socket that
  never speaks RNS is bound to the instance's address — AF_UNIX `rns/<instance_name>`
  and TCP (`shared_instance_type = tcp`, `shared_instance_port`) alike. With nothing
  listening it raises ("No shared instance available …").
- So `instance_present()` proves a listener only. The exec'd client then believes it
  is attached; whether it would load the LoRa interface in that state is not measured.
- Needed: an authenticated check (for example one RPC call that only a real instance
  with the directory's RPC key answers), or no client path at all once the daemon
  backend owns the radio.
- Open: rns 1.4.0 is the floor in pyproject.toml; its behaviour for the same probe is
  not measured. Measure it, or raise the floor to the version the check is proven on.

## 2. loraham-rns-client: `--host` / `--port` have no effect
- The probe follows `--configdir` alone; both options are accepted and ignored
  (documented in the 0.3.1 CHANGELOG and the option help). v0.4.0 removes them or
  makes the check use them.

## 3. Wiped duty ledger: each TX attempt waits about 2 s before it refuses
- Since 0.3.1 a caller that finds the lock file but no ledger waits up to
  LOCK_TIMEOUT_S (2.0 s) for the creator's initialisation write. When the ledger was
  wiped it never comes: every queued packet waits about 2 s, then refuses as before
  ("has disappeared") and is dropped. The refusal is unchanged; the TX worker is
  slower to drain its queue. A change (for example telling "creator in its gap" from
  "wiped" by the lock file's age) is a behaviour change.
