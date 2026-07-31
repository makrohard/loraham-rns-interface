"""`loraham-rns-node` — the fail-closed service runner lhpc starts.

Plain `rnsd` is not safe to supervise: RNS catches an external-interface
initialisation exception, logs it and carries on, so a failed lock or a dead
radio leaves a process running with its shared-instance ports open and no radio
behind them. A second rnsd also happily attaches to an existing instance as a
CLIENT and keeps running, loading no interfaces at all.

This runner refuses both outcomes: it exits non-zero unless it became the
shared-instance owner AND the named LoRa interface is present and online, and it
exits if the interface goes fatal later.
"""

import argparse
import ipaddress
import os
import signal
import sys
import time

import RNS

EXIT_OK = 0
EXIT_NOT_OWNER = 3          # attached to a foreign shared instance
EXIT_NO_INTERFACE = 4       # interface missing or never came online
EXIT_RADIO_FATAL = 5        # bus/lock/radio fault while running
EXIT_UNSAFE_EXPOSURE = 6    # client access bound off-loopback with no allow-list
EXIT_NO_CLIENT_PORT = 7     # the advertised client-access listener never opened

CLIENT_ACCESS_PORT = 4242

CHECK_INTERVAL_S = 2.0


_LOOPBACK = ("127.", "::1", "localhost")


def _clear_ready(path):
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        # Readiness is gated solely on this path existing. A stale marker we
        # cannot remove would make a FAILED start look verified, so this is fatal.
        raise SystemExit(f"could not clear the ready marker {path}: {exc}")


def _own_listener(port):
    """Whether THIS process owns a listening socket on 127.0.0.1:<port>.

    Connecting to the port proves only that *something* answers — and the failure we
    are detecting is precisely a FOREIGN process holding 4242, which would satisfy a
    connect test and let us report ready with no client interface of our own. So match
    the socket inodes this process holds against the kernel's listening table.
    """
    inodes = set()
    fd_dir = "/proc/self/fd"
    try:
        for name in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, name))
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[len("socket:["):-1])
    except OSError:
        return False
    if not inodes:
        return False
    want_port = f"{port:04X}"
    for table, loopback in (("/proc/net/tcp", "0100007F"), ("/proc/net/tcp6", None)):
        try:
            with open(table) as fh:
                next(fh, None)
                for line in fh:
                    cols = line.split()
                    if len(cols) < 10 or cols[3] != "0A":       # 0A = LISTEN
                        continue
                    addr, _, prt = cols[1].rpartition(":")
                    if prt != want_port:
                        continue
                    if loopback and addr != loopback:
                        continue
                    if cols[9] in inodes:
                        return True
        except OSError:
            continue
    return False


def _wait_own_listener(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _own_listener(port):
            return True
        time.sleep(0.2)
    return False


def _set_ready(path):
    """Written only once the shared instance is OURS and the radio is online."""
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        fh.write(f"{os.getpid()}\n")
    os.replace(tmp, path)


def _unsafe_exposure(configdir, client_allow):
    """Remote client access needs BOTH a non-loopback bind and an allow-list.
    A bind opened without one would be an unauthenticated RNS interface reachable
    from the LAN, so refuse rather than start."""
    try:
        with open(os.path.join(configdir, "config")) as fh:
            listen = ""
            for line in fh:
                key, _, val = line.partition("=")
                if key.strip() == "listen_ip":
                    listen = val.strip().strip('"')
    except OSError:
        return ""
    if not listen:
        return ""
    # EXACT parse, not a prefix test: "localhost.example" and "127.0.0.1.evil.net"
    # both passed `startswith("127.0.0.1"/"localhost")` and read as loopback.
    try:
        loopback = ipaddress.ip_address(listen).is_loopback
    except ValueError:
        loopback = listen == "localhost"
    if loopback:
        return ""
    # An allow-list is FIREWALL INTENT, not enforcement: the managed firewall may be
    # absent, stale or never applied. RNS client access carries no authentication, so
    # a non-loopback bind is refused outright in this release regardless of it.
    return (f"client access is bound to {listen} — this release binds RNS client access "
            f"to loopback only, because it has no authentication of its own and an "
            f"allow-list is not enforcement. Bind 127.0.0.1 and reach it over SSH.")


def _find(name):
    for iface in RNS.Transport.interfaces:
        if getattr(iface, "name", None) == name:
            return iface
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loraham-rns-node")
    ap.add_argument("--config", required=True, help="RNS config directory")
    ap.add_argument("--interface", required=True, help="interface name that must be online")
    ap.add_argument("--startup-timeout", type=float, default=30.0)
    ap.add_argument("--ready-file", default="",
                    help="path touched ONLY after ownership and radio are confirmed, and "
                         "removed on exit. Supervisors must key readiness on this, not on "
                         "Reticulum's own ports: RNS binds those during startup, before the "
                         "radio is known good.")
    ap.add_argument("--client-allow", default="127.0.0.1",
                    help="who may reach the client-access port (drives the managed firewall; "
                         "checked here so an exposed bind cannot ship without one)")
    args = ap.parse_args(argv)

    # Clear any stale marker FIRST: a crashed predecessor must never make this
    # launch look ready.
    _clear_ready(args.ready_file)

    unsafe = _unsafe_exposure(args.config, args.client_allow)
    if unsafe:
        RNS.log(unsafe, RNS.LOG_CRITICAL)
        return EXIT_UNSAFE_EXPOSURE

    reticulum = RNS.Reticulum(configdir=args.config)

    # 1. We must OWN the shared instance, not have attached to someone else's.
    if getattr(reticulum, "is_connected_to_shared_instance", False):
        RNS.log("another Reticulum shared instance already owns this configuration; "
                "refusing to run as a client with no radio", RNS.LOG_CRITICAL)
        return EXIT_NOT_OWNER
    # RNS falls back to STANDALONE when it can neither create nor join a shared
    # instance. That state also reports is_connected_to_shared_instance False,
    # so it would otherwise pass as "owner" while serving nobody.
    if getattr(reticulum, "is_standalone_instance", False):
        RNS.log("Reticulum fell back to a standalone instance — the shared instance "
                "could not be created; refusing to run in a state clients cannot reach",
                RNS.LOG_CRITICAL)
        return EXIT_NOT_OWNER

    # 2. The radio interface must exist and be online. RNS swallows the
    #    constructor exception, so absence here means initialisation failed.
    deadline = time.monotonic() + args.startup_timeout
    iface = None
    while time.monotonic() < deadline:
        iface = _find(args.interface)
        if iface is not None and getattr(iface, "online", False):
            break
        if iface is not None and getattr(iface, "fatal", None) is not None:
            break
        time.sleep(0.25)

    if iface is None:
        RNS.log(f"interface {args.interface!r} was not registered — its "
                f"initialisation failed (RNS logs the cause above)", RNS.LOG_CRITICAL)
        return EXIT_NO_INTERFACE
    if not getattr(iface, "online", False):
        RNS.log(f"interface {args.interface!r} did not come online: "
                f"{getattr(iface, 'fatal', 'unknown cause')}", RNS.LOG_CRITICAL)
        return EXIT_NO_INTERFACE

    # SIGTERM is how the controller stops us, and its DEFAULT action kills the
    # process outright — the `finally` below never ran, so the readiness marker
    # survived the node and lhpc reported "process ceased but readiness endpoint
    # still present", retaining ownership. Turn it into a normal unwind.
    def _terminate(_sig, _frame):
        raise SystemExit(EXIT_OK)

    signal.signal(signal.SIGTERM, _terminate)

    # Prove the ADVERTISED client interface is actually there before declaring ready.
    # RNS carries on with the radio if 4242 is already taken, so the node could report a
    # verified start while every client (lxmd/nomadnet/sideband) had nothing to attach
    # to. Readiness must mean the whole contract, not just the radio.
    if not _wait_own_listener(CLIENT_ACCESS_PORT):
        RNS.log(f"client access on 127.0.0.1:{CLIENT_ACCESS_PORT} is not owned by this "
                f"process (another process is holding it, or RNS never opened it) — "
                f"refusing to report ready", RNS.LOG_CRITICAL)
        return EXIT_NO_CLIENT_PORT

    _set_ready(args.ready_file)
    RNS.log(f"loraham-rns-node ready: owner of the shared instance, "
            f"{iface} online at {iface.bitrate} bps", RNS.LOG_NOTICE)

    # 3. Stay up, but die loudly if the radio goes fatal.
    try:
        while True:
            time.sleep(CHECK_INTERVAL_S)
            if getattr(iface, "fatal", None) is not None:
                RNS.log(f"radio fault: {iface.fatal}", RNS.LOG_CRITICAL)
                return EXIT_RADIO_FATAL
            if not getattr(iface, "online", False):
                RNS.log("interface went offline", RNS.LOG_CRITICAL)
                return EXIT_RADIO_FATAL
    except KeyboardInterrupt:
        RNS.log("shutting down", RNS.LOG_NOTICE)
        return EXIT_OK
    finally:
        _clear_ready(args.ready_file)


if __name__ == "__main__":
    sys.exit(main())
