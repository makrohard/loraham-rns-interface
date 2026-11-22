"""loraham-rns-client: it never runs a client without the shared-instance owner."""

import contextlib
import os
import select
import socket
import subprocess
import sys
import threading

import pytest

OWNER = "import sys, threading, RNS\nRNS.Reticulum(configdir=sys.argv[1])\nprint('up', flush=True)\nthreading.Event().wait()\n"


def _configdir(tmp_path, name, instance, tcp=None):
    """A config dir of its own; `tcp` = (instance port, RPC port) makes its instance a TCP one."""
    cfg = tmp_path / name
    cfg.mkdir()
    extra = (f"  shared_instance_type = tcp\n  shared_instance_port = {tcp[0]}\n  instance_control_port = {tcp[1]}\n"
             if tcp else "")
    (cfg / "config").write_text(f"[reticulum]\n  share_instance = Yes\n  instance_name = {instance}\n{extra}\n[interfaces]\n")
    return cfg


def _free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


@contextlib.contextmanager
def _owner(cfg):
    """A real Reticulum shared-instance owner on `cfg`, in its own process, while the block runs."""
    proc = subprocess.Popen([sys.executable, "-c", OWNER, str(cfg)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert select.select([proc.stdout], [], [], 30)[0] and proc.stdout.readline() == "up\n", "owner did not start"
        yield
    finally:
        proc.kill()
        proc.wait()


def _run(monkeypatch, cfg, *options):
    """client.main with the exec recorded instead of made: (exit code, the exec'd argv or None)."""
    from loraham_rns import client

    ran = []
    monkeypatch.setattr(os, "execvp", lambda file, argv: ran.append(argv))
    rc = client.main(["--configdir", str(cfg), *options, "--", "/bin/true"])
    return rc, (ran[0] if ran else None)


def _isolated_configdir(tmp_path):
    """An RNS config whose shared instance has a name no other process uses.

    Without it RNS falls back to the instance name "default", and on a host that runs
    its own rnsd the client's probe attaches to THAT instance — the test then measures
    the host, not the code."""
    import uuid

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config").write_text(
        "[reticulum]\n"
        "  share_instance = Yes\n"
        f"  instance_name = lhpc-test-{uuid.uuid4().hex}\n"
        "\n[interfaces]\n")
    return cfg


@pytest.fixture
def default_instance_occupied():
    """Something listens on the default instance's address (AF_UNIX "rns/default") for
    the whole test: a plain socket of ours, or the host's own rnsd when one runs. RNS
    reports any listener there as an instance, so a test that is not isolated attaches
    and tries to exec — on every host, not only on one that runs rnsd."""
    import errno
    import socket

    srv = socket.socket(socket.AF_UNIX)
    try:
        srv.bind("\0rns/default")
        srv.listen(1)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
    try:
        yield
    finally:
        srv.close()


def test_a_client_refuses_to_run_without_the_owner(tmp_path, capsys, default_instance_occupied):
    """Reticulum makes the FIRST process the shared-instance owner, and the clients are
    pointed at the owner's config — which declares the LoRa interface. Started with no
    owner they take the radio."""
    from loraham_rns import client

    marker = tmp_path / "ran"
    rc = client.main(["--configdir", str(_isolated_configdir(tmp_path)),
                      "--", "/bin/sh", "-c", f"touch {marker}"])
    assert rc == 3
    assert not marker.exists(), "the client must not have been executed"
    assert "would become the shared-instance OWNER" in capsys.readouterr().err


def test_an_open_port_does_not_stand_in_for_an_absent_instance(tmp_path, default_instance_occupied):
    """The probe asks RNS about the instance named in the config directory; --port only
    states where that instance must be. A listener on the given port therefore changes
    nothing while that instance is absent."""
    import socket

    from loraham_rns import client

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    marker = tmp_path / "ran"
    try:
        rc = client.main(["--configdir", str(_isolated_configdir(tmp_path)),
                          "--port", str(port), "--", "/bin/sh", "-c", f"touch {marker}"])
    finally:
        srv.close()
    assert rc == 3, "an absent instance must refuse, whatever listens on --port"
    assert not marker.exists()


def test_a_client_runs_when_the_owner_answers(tmp_path, monkeypatch):
    import uuid

    cfg = _configdir(tmp_path, "cfg", f"lhpc-test-{uuid.uuid4().hex}")
    with _owner(cfg):
        assert _run(monkeypatch, cfg, "--wait", "10") == (None, ["/bin/true"])


def _accept_and_close(srv):
    def loop():
        while True:
            try:
                srv.accept()[0].close()
            except OSError:
                return
    threading.Thread(target=loop, daemon=True).start()


def _silent(srv):
    held = []
    def loop():
        while True:
            try:
                held.append(srv.accept()[0])
            except OSError:
                return
    threading.Thread(target=loop, daemon=True).start()
    return held


@pytest.mark.parametrize("rpc", [False, True])
def test_a_listener_on_the_instance_address_is_not_the_owner(tmp_path, monkeypatch, rpc):
    """A plain socket on the instance's own address (AF_UNIX `rns/<instance_name>`), and also on its
    RPC address when `rpc`: RNS attaches to it, but the RPC with the config dir's key fails."""
    import uuid

    name = f"lhpc-test-{uuid.uuid4().hex}"
    cfg = _configdir(tmp_path, "cfg", name)
    with contextlib.ExitStack() as stack:
        for address in [f"\0rns/{name}"] + ([f"\0rns/{name}/rpc"] if rpc else []):
            srv = stack.enter_context(socket.socket(socket.AF_UNIX))
            srv.bind(address)
            srv.listen(4)
            _accept_and_close(srv)
        assert _run(monkeypatch, cfg) == (3, None)


def test_the_owner_of_another_config_dir_is_not_this_owner(tmp_path, monkeypatch):
    """Same instance name, another directory (another identity, so another RPC key): refused."""
    import uuid

    name = f"lhpc-test-{uuid.uuid4().hex}"
    with _owner(_configdir(tmp_path, "theirs", name)):
        assert _run(monkeypatch, _configdir(tmp_path, "ours", name)) == (3, None)


def test_a_listener_that_never_answers_ends_the_probe_at_its_alarm(tmp_path, monkeypatch):
    """Both addresses accept and stay silent: the RPC handshake waits, the alarm ends the child
    (killed by SIGALRM), not the subprocess timeout."""
    import signal
    import uuid

    from loraham_rns import client

    monkeypatch.setattr(client, "PROBE_S", 1)
    real, ends = subprocess.run, []

    def run(*args, **kwargs):
        try:
            done = real(*args, **{**kwargs, "timeout": 5})
        except subprocess.TimeoutExpired:
            ends.append("timeout")
            raise
        ends.append(done.returncode)
        return done

    monkeypatch.setattr(subprocess, "run", run)
    name = f"lhpc-test-{uuid.uuid4().hex}"
    cfg = _configdir(tmp_path, "cfg", name)
    with contextlib.ExitStack() as stack:
        for address in (f"\0rns/{name}", f"\0rns/{name}/rpc"):
            srv = stack.enter_context(socket.socket(socket.AF_UNIX))
            srv.bind(address)
            srv.listen(4)
            stack.callback(lambda held: [c.close() for c in held], _silent(srv))
        assert _run(monkeypatch, cfg) == (3, None)
    assert ends == [-signal.SIGALRM]


def test_port_and_host_state_where_the_tcp_instance_must_be(tmp_path, monkeypatch, capsys):
    """The owner's instance is the TCP one on port P: --port P (with or without a loopback --host)
    runs the client, another port refuses with a line naming both addresses."""
    import uuid

    port, rpc, other = _free_ports(3)
    cfg = _configdir(tmp_path, "cfg", f"lhpc-test-{uuid.uuid4().hex}", tcp=(port, rpc))
    with _owner(cfg):
        assert _run(monkeypatch, cfg, "--wait", "10", "--port", str(port)) == (None, ["/bin/true"])
        assert _run(monkeypatch, cfg, "--host", "localhost", "--port", str(port)) == (None, ["/bin/true"])
        assert _run(monkeypatch, cfg, "--host", "127.0.0.1") == (None, ["/bin/true"])
        capsys.readouterr()
        assert _run(monkeypatch, cfg, "--port", str(other)) == (3, None)
    err = capsys.readouterr().err
    assert f"is at 127.0.0.1:{port}, not at 127.0.0.1:{other}" in err


@pytest.mark.parametrize("options", [["--port", "37428"], ["--host", "127.0.0.1"]])
def test_an_af_unix_instance_is_not_the_tcp_one_the_options_name(tmp_path, monkeypatch, capsys, options):
    """The Linux default: the instance is on AF_UNIX `rns/<instance_name>`, so a stated TCP address refuses."""
    import uuid

    name = f"lhpc-test-{uuid.uuid4().hex}"
    cfg = _configdir(tmp_path, "cfg", name)
    with _owner(cfg):
        assert _run(monkeypatch, cfg, *options) == (3, None)
    assert f"is at AF_UNIX rns/{name}, not at 127.0.0.1" in capsys.readouterr().err


def test_a_host_rns_cannot_reach_is_a_usage_error_and_nothing_is_probed(tmp_path, monkeypatch):
    real, probed = subprocess.run, []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: probed.append(a) or real(*a, **k))
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, _isolated_configdir(tmp_path), "--host", "192.0.2.1")
    assert (exc.value.code, probed) == (2, [])
