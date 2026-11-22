"""Launch an RNS client only when the radio owner is already up.

Reticulum makes the FIRST process the shared-instance owner. Our clients (lxmd,
NomadNet, Sideband) are pointed at the owner's config directory — which they must be,
because RNS derives the shared-instance RPC key from the identity in that directory —
and that config declares the LoRa interface. So a client started while `rns` is absent
does not fail: it becomes the owner and initialises the radio itself, taking the
hardware behind lhpc's back.

Upstream's own guard is `Reticulum(require_shared_instance=True)`, a CONSTRUCTOR
argument. We cannot pass it without patching lxmd/NomadNet/Sideband, and patching
upstream is out of scope, so this wrapper enforces the same precondition from outside:
prove the owner's shared instance answers with the config dir's RPC key, then exec the client.

HONEST LIMIT: this closes the window that matters in practice (client started with no
owner running — the manual command, a client restarted outside lhpc, a stale unit), but
it is not atomic. If `rns` dies between the check and the client's own Reticulum
init, that client can still take ownership. Removing that race needs the constructor
argument, i.e. upstream support.
"""

import argparse
import os
import subprocess
import sys
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 37428          # the shared instance's RPC listener
PROBE_S = 10                  # SIGALRM in the probe: a listener that accepts and never answers


def instance_present(configdir, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=0.0):
    """Ask RNS whether the shared instance of `configdir` is there and answers with that
    directory's RPC key.

    The check runs in a CHILD process (its own instance must not survive into the
    exec'd client): it constructs Reticulum with `require_shared_instance=True`, which
    raises when nothing listens on the instance's socket, and then asks the instance for
    its interface stats over the RPC. RNS authenticates that call with the key derived
    from `configdir` (its identity or its `rpc_key`), so a plain listener on the
    instance's address, or an instance of another config dir, is refused: the call
    raises (rns 1.5.5) or returns None (rns 1.6.1 catches it); only a dict or list passes. SIGALRM after
    PROBE_S ends a probe that a listener keeps waiting (the subprocess timeout is the outer bound).

    KNOWN LIMIT: `host` and `port` are not used: the probe follows the config in
    `configdir` alone, so `--host/--port` of loraham-rns-client have no effect.
    """
    probe = (
        "import signal, sys, RNS\n"
        "signal.alarm(int(sys.argv[2]))\n"
        "try:\n"
        "    RNS.Reticulum(configdir=sys.argv[1], require_shared_instance=True)\n"
        "    rns = RNS.Reticulum.get_instance()\n"
        "    ok = rns.is_connected_to_shared_instance and isinstance(rns.get_interface_stats(), (dict, list))\n"
        "except Exception as exc:\n"
        "    sys.stderr.write(str(exc)[:200]); sys.exit(1)\n"
        "sys.exit(0 if ok else 1)\n")
    deadline = time.time() + max(timeout, 0.0)
    while True:
        try:
            done = subprocess.run([sys.executable, "-c", probe, configdir, str(PROBE_S)],
                                  capture_output=True, timeout=30, check=False)
            if done.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        prog="loraham-rns-client",
        description="Run an RNS client only if the shared instance already exists.")
    ap.add_argument("--configdir", required=True,
                    help="the RNS config dir the client will use (the OWNER's)")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="not used: the check follows --configdir (known limit)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="not used: the check follows --configdir (known limit)")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="seconds to wait for the instance before giving up")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="-- <program> [args...] to exec once the instance is present")
    args = ap.parse_args(argv)

    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        ap.error("no command given (use: -- <program> [args...])")

    if not instance_present(args.configdir, args.host, args.port, args.wait):
        sys.stderr.write(
            f"loraham-rns-client: no Reticulum shared instance for "
            f"{args.configdir} — refusing to start {os.path.basename(cmd[0])}, "
            f"because it would become the shared-instance OWNER and take the radio.\n"
            f"Start the node first:  lhpc stack start reticulum\n")
        return 3

    os.execvp(cmd[0], cmd)     # replaces this process; never returns on success  # noqa: S606 (the operator's argv)


if __name__ == "__main__":
    sys.exit(main())
