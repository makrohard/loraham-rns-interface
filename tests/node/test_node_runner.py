"""The service runner's refusals before it reports ready."""

import pytest


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
