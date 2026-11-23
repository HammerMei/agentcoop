"""The operator's channel: the control socket's commands end to end through the CLI,
and the configuration edits the CLI makes."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from coop import cli, paths
from coop.config import load
from coop.control import ControlServer, send_command

from .test_service import CONFIG, connector, service, settle


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    """A service behind a control socket, with the CLI pointed at it."""
    fc = connector()
    svc = service(tmp_path, fc)
    (tmp_path / "config.yaml").write_text(CONFIG)
    await svc.start(listen=False)
    stopped = asyncio.Event()
    socket = short_socket()  # a pytest tmp_path is longer than AF_UNIX allows on macOS
    control = ControlServer(svc, socket, tmp_path / "config.yaml", stopped.set)
    await control.start()
    monkeypatch.setattr(paths, "SOCKET_FILE", socket)
    yield svc, fc, control, stopped, tmp_path
    await control.stop()
    await svc.stop()
    shutil.rmtree(socket.parent, ignore_errors=True)


def short_socket() -> Path:
    return Path(tempfile.mkdtemp(prefix="coop-")) / "coop.sock"


async def test_status_list_and_holds_round_trip_through_the_socket(daemon):
    svc, fc, control, _, _ = daemon
    status = await send_command(control.socket_path, {"cmd": "status"})
    assert status["ok"] and status["data"]["servers"] == {"fake": ["a", "b"]}
    listed = await send_command(control.socket_path, {"cmd": "list"})
    assert listed["data"] == {"fake:team": {"hosted": ["a", "b"], "bots": ["a_bot", "b_bot", "x_bot"], "room_id": "c1", "dm": False}}
    assert (await send_command(control.socket_path, {"cmd": "pause", "agent": "a", "room": "fake:team"}))["ok"]
    assert svc.rooms["fake:team"].core.persisted["a_bot"].paused
    refused = await send_command(control.socket_path, {"cmd": "pause", "agent": "a", "room": "fake:zzz"})
    assert not refused["ok"] and "no hosted room" in refused["error"]
    assert "unknown command" in (await send_command(control.socket_path, {"cmd": "dance"}))["error"]


async def test_reload_reads_the_file_and_a_bad_file_changes_nothing(daemon):
    svc, fc, control, _, base = daemon
    (base / "config.yaml").write_text(CONFIG.replace("agents: [a, b, x]", "agents: [a, x]"))
    reply = await send_command(control.socket_path, {"cmd": "reload"})
    assert reply["ok"] and svc.rooms["fake:team"].core.hosted == ["a_bot"]
    (base / "config.yaml").write_text("servers: {fake: {type: slack}}")
    reply = await send_command(control.socket_path, {"cmd": "reload"})
    assert not reply["ok"] and "not applied" in reply["error"] and svc.rooms["fake:team"].core.hosted == ["a_bot"]


async def test_stop_fires_the_callback_and_a_live_socket_refuses_a_second_server(daemon):
    svc, fc, control, stopped, base = daemon
    second = ControlServer(svc, control.socket_path, base / "config.yaml", lambda: None)
    with pytest.raises(RuntimeError, match="is live"):
        await second.start()
    assert (await send_command(control.socket_path, {"cmd": "stop"}))["ok"] and stopped.is_set()


async def test_a_stale_socket_file_is_replaced(tmp_path):
    socket = short_socket()
    socket.write_text("")  # not a socket at all: connecting fails, so it is stale
    control = ControlServer(None, socket, tmp_path / "config.yaml", lambda: None)
    await control.start()
    try:
        assert socket.is_socket()
    finally:
        await control.stop()
    assert not socket.exists()
    shutil.rmtree(socket.parent, ignore_errors=True)


async def test_the_cli_prints_status_list_and_holds(daemon, capsys):
    svc, fc, control, _, _ = daemon
    await fc.human("c1", "H", "@a_bot hi")
    await settle()
    assert await asyncio.to_thread(cli.main, ["status"]) == 0
    out = capsys.readouterr().out
    assert "server fake: bots a, b" in out and "agent a: offline, bio v0" in out and "room fake:team: a (a_bot)" in out
    assert await asyncio.to_thread(cli.main, ["list"]) == 0
    assert "fake:team: hosted a, b; bots a_bot, b_bot, x_bot" in capsys.readouterr().out
    assert await asyncio.to_thread(cli.main, ["pause", "a", "fake:team"]) == 0
    assert svc.rooms["fake:team"].core.persisted["a_bot"].paused
    assert await asyncio.to_thread(cli.main, ["resume", "a", "fake:team"]) == 0
    assert await asyncio.to_thread(cli.main, ["reset", "zz", "fake:team"]) == 1
    assert capsys.readouterr().err.startswith("[ERROR]")


def test_the_cli_without_a_daemon_is_an_error_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "SOCKET_FILE", tmp_path / "none.sock")
    assert cli.main(["status"]) == 1
    assert "[ERROR] cannot reach the daemon" in capsys.readouterr().err


def test_config_add_agent_writes_the_entry_the_token_and_prints_what_the_host_needs(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("servers:\n  lab:\n    type: mattermost\n    url: https://mm\n    team: t\n    owners: [glin]\n")
    (tmp_path / "lab-bot.token").write_text("platform-token\n")
    argv = ["config", "--config", str(config), "add", "agent", "laomei", "--server", "lab", "--username", "laomei_bot",
            "--bot-token-file", str(tmp_path / "lab-bot.token"), "--owner", "glin", "--owner", "pat"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    token = (tmp_path / "agents" / "laomei" / "token").read_text().strip()
    assert token and f"COOP_TOKEN={token}" in out and "COOP_GATEWAY=http://127.0.0.1:7474" in out
    assert "COOP_FINGERPRINT" not in out
    loaded = load(config, tmp_path)
    assert loaded.agents["laomei"].token_file == tmp_path / "agents/laomei/token"
    assert loaded.servers["lab"].bots["laomei"].owners == {"glin", "pat"}
    assert loaded.servers["lab"].bots["laomei"].token_file == tmp_path / "lab-bot.token"
    assert cli.main(argv) == 1  # twice is refused
    assert "already exists" in capsys.readouterr().err
    assert cli.main(argv[:5] + ["other", "--server", "nope", "--username", "u", "--bot-token-file", "f"]) == 1


def test_config_add_agent_refuses_what_the_loader_refuses_and_leaves_the_file_alone(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    text = "servers:\n  lab:\n    type: mattermost\n    url: https://mm\n"  # no team
    config.write_text(text)
    argv = ["config", "--config", str(config), "add", "agent", "a", "--server", "lab", "--username", "u",
            "--bot-token-file", "t"]
    assert cli.main(argv) == 1 and "names its team" in capsys.readouterr().err
    assert config.read_text() == text


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_config_tls_makes_a_certificate_and_records_it(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("servers: {}\n")
    assert cli.main(["config", "--config", str(config), "tls", "--host", "gw.local"]) == 0
    out = capsys.readouterr().out
    assert "COOP_FINGERPRINT=" in out
    doc = yaml.safe_load(config.read_text())
    assert doc["gateway"]["tls"] == {"cert": "gateway/cert.pem", "key": "gateway/key.pem"}
    assert (tmp_path / "gateway" / "key.pem").stat().st_mode & 0o777 == 0o600
    loaded = load(config, tmp_path)
    assert loaded.gateway.tls == (tmp_path / "gateway/cert.pem", tmp_path / "gateway/key.pem")


def test_config_add_agent_leaves_no_token_behind_when_the_configuration_is_refused(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("servers:\n  lab:\n    type: mattermost\n    url: https://mm\n    team: t\n    bots:\n"
                      "      muse: {username: muse}\n")
    argv = ["config", "--config", str(config), "add", "agent", "muse", "--server", "lab", "--username", "muse",
            "--bot-token-file", "t"]
    assert cli.main(argv) == 1 and "already exists" in capsys.readouterr().err
    assert not (tmp_path / "agents").exists()
