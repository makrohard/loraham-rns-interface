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

PROBE_S = 10                  # SIGALRM in the probe: a listener that accepts and never answers
LOOPBACK = ("127.0.0.1", "localhost")   # RNS reaches a TCP shared instance on 127.0.0.1 only
ADDRESS = "address: "         # the probe's line naming where the instance is


def instance_address(configdir, timeout=0.0):
    """Where the shared instance of `configdir` is ("AF_UNIX rns/<instance_name>" or
    "127.0.0.1:<shared_instance_port>") when it is there and answers with that directory's
    RPC key; None otherwise, after waiting up to `timeout` seconds.

    The check runs in a CHILD process (its own instance must not survive into the
    exec'd client): it constructs Reticulum with `require_shared_instance=True`, which
    raises when nothing listens on the instance's socket, and then asks the instance for
    its interface stats over the RPC. RNS authenticates that call with the key derived
    from `configdir` (its identity or its `rpc_key`), so a plain listener on the
    instance's address, or an instance of another config dir, is refused: the call
    raises (rns 1.5.5) or returns None (rns 1.6.1 catches it); only a dict or list passes. SIGALRM after
    PROBE_S ends a probe that a listener keeps waiting (the subprocess timeout is the outer bound).
    """
    probe = (
        "import signal, sys, RNS\n"
        "signal.alarm(int(sys.argv[2]))\n"
        "try:\n"
        "    RNS.Reticulum(configdir=sys.argv[1], require_shared_instance=True)\n"
        "    rns = RNS.Reticulum.get_instance()\n"
        "    ok = rns.is_connected_to_shared_instance and isinstance(rns.get_interface_stats(), (dict, list))\n"
        "    where = f'AF_UNIX rns/{rns.local_socket_path}' if rns.use_af_unix else f'127.0.0.1:{rns.local_interface_port}'\n"
        "except Exception as exc:\n"
        "    sys.stderr.write(str(exc)[:200]); sys.exit(1)\n"
        f"print('{ADDRESS}' + where, flush=True)\n"
        "sys.exit(0 if ok else 1)\n")
    deadline = time.time() + max(timeout, 0.0)
    while True:
        try:
            done = subprocess.run([sys.executable, "-c", probe, configdir, str(PROBE_S)],
                                  capture_output=True, text=True, timeout=30, check=False)
            lines = [line for line in done.stdout.splitlines() if line.startswith(ADDRESS)]
            if done.returncode == 0 and lines:
                return lines[-1][len(ADDRESS):]
        except (OSError, subprocess.SubprocessError):
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        prog="loraham-rns-client",
        description="Run an RNS client only if the shared instance already exists.",
        epilog="Exit 3: no shared instance for --configdir answers with its key, or it is not "
               "where --host/--port say; 2: a usage error; otherwise the command runs in this "
               "process and its status is the exit status.")
    ap.add_argument("--configdir", required=True,
                    help="the RNS config dir the client will use (the OWNER's)")
    ap.add_argument("--host",
                    help="the instance must be the TCP one on this host: 127.0.0.1 or localhost "
                         "(RNS reaches a shared instance there only)")
    ap.add_argument("--port", type=int,
                    help="the instance must be the TCP one on this port (shared_instance_type = tcp, "
                         "shared_instance_port); it is checked, not used to reach it")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="seconds to wait for the instance before giving up")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="-- <program> [args...] to exec once the instance is present")
    args = ap.parse_args(argv)

    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        ap.error("no command given (use: -- <program> [args...])")

    if args.host is not None and args.host not in LOOPBACK:
        ap.error(f"--host {args.host}: RNS reaches a shared instance on 127.0.0.1 only")

    address = instance_address(args.configdir, args.wait)
    if address is None:
        sys.stderr.write(
            f"loraham-rns-client: no Reticulum shared instance for "
            f"{args.configdir} — refusing to start {os.path.basename(cmd[0])}, "
            f"because it would become the shared-instance OWNER and take the radio.\n"
            f"Start the node first:  lhpc stack start reticulum\n")
        return 3
    if args.host is not None or args.port is not None:
        wanted = f"127.0.0.1:{args.port}" if args.port is not None else "127.0.0.1 (TCP)"
        if address.startswith("AF_UNIX") or (args.port is not None and address != wanted):
            sys.stderr.write(
                f"loraham-rns-client: the instance of {args.configdir} is at {address}, not at "
                f"{wanted} — refusing to start {os.path.basename(cmd[0])}.\n")
            return 3

    os.execvp(cmd[0], cmd)     # replaces this process; never returns on success  # noqa: S606 (the operator's argv)


if __name__ == "__main__":
    sys.exit(main())
