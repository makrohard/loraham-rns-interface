"""Suite-wide guards.

loraham_rns.client.main() ends in os.execvp when it believes a shared instance is
present. Inside pytest that replaces the test process: the run stops where it is and
exits with the exec'd program's status, so a host that runs its own rnsd turns the
rest of the suite into a silent "pass". Every exec function of os raises instead, so
an exec attempt is a hard failure of the test that made it.
"""

import os

import pytest

EXEC_FUNCS = sorted(name for name in dir(os) if name.startswith("exec"))


@pytest.fixture(autouse=True)
def _no_exec(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"os.exec* called inside the test process: {args[:2]!r}")
    for name in EXEC_FUNCS:
        monkeypatch.setattr(os, name, refuse)
