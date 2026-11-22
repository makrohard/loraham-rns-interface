"""loraham-rns-client: it never runs a client without the shared-instance owner."""

import pytest


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
    """The probe asks RNS about the instance named in the config directory; the
    --host/--port arguments are not used by it. A listener on the given port therefore
    changes nothing while that instance is absent. (Known limit: at rns 1.5.7 RNS
    reports any listener on the instance's OWN socket as connected — a plain socket
    included; see the CHANGELOG.)"""
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
