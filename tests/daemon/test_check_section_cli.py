"""loraham-rns-check-section, the controller's pre-check (docs/daemon-interface.md, "The pre-check"):
one section from FILE or stdin, exit 0 when the daemon interface accepts it, 1 with one line naming the key,
2 on a usage error. It is the constructor's own check, so the two never disagree."""

import io
import subprocess
import sys

import pytest
import RNS

from loraham_rns.daemon import LoRaDaemonInterface
from loraham_rns.section import SectionError, main

SECTION = """  [[LoRa r2]]
    type = LoRaDaemonInterface
    interface_enabled = no
    band = 868
    frequency = 868500000
    # the daemon's sockets
    conf_socket = /run/loraham/loraconf-r2.sock
    data_socket = "/run/loraham/loraf-r2.sock"
"""


def _run(text, capsys):
    rc = main([], io.StringIO(text))
    return rc, capsys.readouterr().out


def test_an_accepted_section_exits_0_silently(capsys):
    assert _run(SECTION, capsys) == (0, "")


@pytest.mark.parametrize("extra,key", [("txpower = 20", "txpower"), ("networkname = lab", "passphrase"),
                                       ("band = 868", "band"), ("frequency 868500000", "line 9")])
def test_a_refused_section_exits_1_with_one_line_naming_the_key(capsys, extra, key):
    rc, out = _run(SECTION + f"    {extra}\n", capsys)
    assert rc == 1 and out.count("\n") == 1 and out.partition(": ")[0] == key


@pytest.mark.parametrize("line", ["frequency = 868500000   # EU 868", "frequency = '868500000'"])
def test_the_section_is_read_as_the_node_reads_it(capsys, line):
    """RNS reads its config with its vendored ConfigObj: an inline comment or quotes are not the value."""
    assert _run(SECTION.replace("frequency = 868500000", line), capsys) == (0, "")


@pytest.mark.parametrize("extra,key", [("    junk\n    more junk\n", "line 9"), ("    band = 868\n    junk\n", "band")])
def test_several_bad_lines_are_one_refusal_naming_the_first(capsys, extra, key):
    rc, out = _run(SECTION + extra, capsys)
    assert rc == 1 and out.count("\n") == 1 and out.partition(": ")[0] == key


def test_a_second_section_is_refused(capsys):
    rc, out = _run(SECTION + "  [[LoRa r3]]\n    band = 868\n", capsys)
    assert rc == 1 and out.partition(": ")[0] == "section"


def test_only_a_daemon_interface_section_passes(capsys):
    rc, out = _run(SECTION.replace("LoRaDaemonInterface", "LoRaSPIInterface"), capsys)
    assert rc == 1 and out.partition(": ")[0] == "type"


@pytest.mark.parametrize("extra", ["txpower = 15", "cadidle = 2001", "airtime_limit_long = 2", "rf_log = yes"])
def test_the_cli_refuses_exactly_what_the_constructor_refuses(capsys, monkeypatch, extra):
    """The same section through the interface's constructor: refused before any socket, same key."""
    monkeypatch.setattr(RNS.Interfaces.Interface.Interface, "__init__", lambda self: None)
    rc, out = _run(SECTION + f"    {extra}\n", capsys)
    key, _, value = extra.partition(" = ")
    config = {"name": "LoRa r2", "type": "LoRaDaemonInterface", "band": "868", "frequency": "868500000",
              "conf_socket": "/run/loraham/loraconf-r2.sock", "data_socket": "/run/loraham/loraf-r2.sock", key: value}
    with pytest.raises(SectionError) as err:
        LoRaDaemonInterface(None, config)
    assert (rc, out) == (1, f"{err.value}\n")


def _command(*args, **stdin):
    run = subprocess.run([sys.executable, "-m", "loraham_rns.section", *args], text=True,
                         capture_output=True, check=False, **stdin)
    return run.returncode, run.stdout


def test_the_command_reads_stdin_and_refuses_other_arguments(tmp_path):
    (tmp_path / "a").write_text(SECTION)
    assert _command(input=SECTION) == (0, "")
    assert _command("--band", "868", input=SECTION)[0] == 2
    assert _command(str(tmp_path / "a"), str(tmp_path / "a"), input=SECTION)[0] == 2


@pytest.mark.parametrize("extra,out", [("", ""), ("    txpower = 20\n", "txpower")])
def test_the_command_reads_a_file_with_no_stdin_as_it_reads_stdin(tmp_path, extra, out):
    """A runner without stdin passes the section as a file: the same exit and line as on stdin."""
    path = tmp_path / "section"
    path.write_text(SECTION + extra)
    rc, line = _command(str(path), stdin=subprocess.DEVNULL)
    assert (rc, line) == _command(input=SECTION + extra) and (rc, line.partition(": ")[0]) == (int(bool(out)), out)


@pytest.mark.parametrize("content", [None, b"\xff\xfe band = 868\n"])
def test_an_unreadable_file_is_a_usage_error_not_a_refusal(tmp_path, content):
    path = tmp_path / "section"
    if content is not None:
        path.write_bytes(content)
    rc, line = _command(str(path), stdin=subprocess.DEVNULL)
    assert (rc, line) == (2, "")
