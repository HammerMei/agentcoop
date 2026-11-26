"""The daemon process's life in this process: the startup pipe's outcome, the stop
through the control socket, and the pid lock."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

import pytest

from coop import daemon
from coop.control import send_command

from .test_control_cli import short_socket
from .test_service import CONFIG, config, connector


@pytest.fixture
def fake_mattermost(monkeypatch):
    fc = connector()

    def factory(server, on_event):
        fc.on_event = on_event
        return fc

    monkeypatch.setattr(daemon, "make_connector", factory)
    return fc


async def test_run_reports_ok_on_the_pipe_serves_the_socket_and_stops_on_command(tmp_path, fake_mattermost, caplog):
    config(tmp_path)
    (tmp_path / "config.yaml").write_text(CONFIG + "gateway:\n  listen: 127.0.0.1:0\n")
    sock = short_socket()
    read_fd, write_fd = os.pipe()
    task = asyncio.create_task(daemon.run(tmp_path / "config.yaml", startup_fd=write_fd, pid_file=tmp_path / "pid",
                                          socket_path=sock, state_dir=tmp_path / "state"))
    report = await asyncio.to_thread(os.read, read_fd, 4096)
    os.close(read_fd)
    assert report.decode().splitlines()[-1] == "ok"
    status = await send_command(sock, {"cmd": "status"})
    assert status["ok"] and status["data"]["listening"].startswith("127.0.0.1:")
    with caplog.at_level(logging.INFO, logger="coop.trace"):
        await fake_mattermost.human("c1", "H", "@a_bot hello")
        for _ in range(20):  # the room's worker takes the post on its own task
            await asyncio.sleep(0)
    assert any(r.name == "coop.trace" and r.getMessage().startswith("turn_start ") for r in caplog.records)
    assert (await send_command(sock, {"cmd": "stop"}))["ok"]
    assert await asyncio.wait_for(task, 10) == 0
    assert not sock.exists()
    shutil.rmtree(sock.parent, ignore_errors=True)


async def test_run_reports_a_startup_failure_on_the_pipe_and_exits_1(tmp_path, fake_mattermost):
    (tmp_path / "config.yaml").write_text("servers: {x: {type: slack, url: u}}")
    read_fd, write_fd = os.pipe()
    code = await daemon.run(tmp_path / "config.yaml", startup_fd=write_fd, pid_file=tmp_path / "pid",
                            socket_path=tmp_path / "sock", state_dir=tmp_path / "state")
    report = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    assert code == 1 and report.startswith("error:") and "slack" in report and "ok" not in report.splitlines()


def test_the_pid_lock_names_a_live_process_and_forgets_a_dead_one(tmp_path):
    pid_file = tmp_path / "coop.pid"
    assert daemon.locked_pid(pid_file) is None and daemon.is_running(pid_file) == (False, None)
    daemon.acquire_lock(pid_file)
    assert daemon.locked_pid(pid_file) == os.getpid() and daemon.is_running(pid_file) == (True, os.getpid())
    pid_file.write_text("2147483647")  # no such process
    assert daemon.is_running(pid_file) == (False, None) and not pid_file.exists()
    pid_file.write_text("garbage")
    assert daemon.locked_pid(pid_file) is None
    daemon.release_lock(pid_file)
    daemon.release_lock(pid_file)  # idempotent


def test_stop_daemon_without_a_daemon_says_so(tmp_path, capsys):
    assert daemon.stop_daemon(tmp_path / "coop.pid") == 0
    assert "not running" in capsys.readouterr().out


def test_the_pid_file_is_taken_once_and_released_only_by_its_holder(tmp_path):
    pid_file = tmp_path / "coop.pid"
    daemon.acquire_lock(pid_file)
    with pytest.raises(RuntimeError, match="already running"):
        daemon.acquire_lock(pid_file)
    pid_file.write_text("2147483647")  # another, dead, holder
    daemon.acquire_lock(pid_file)  # stale: taken over
    assert daemon.locked_pid(pid_file) == os.getpid()
    pid_file.write_text(str(os.getppid()))  # a live holder that is not us
    daemon.release_lock(pid_file)
    assert pid_file.exists(), "a live daemon's pid file is not ours to remove"
    pid_file.write_text(str(os.getpid()))
    daemon.release_lock(pid_file)
    assert not pid_file.exists()


def test_the_daemons_trace_logs_every_event_sweeps_at_debug_and_keeps_none(caplog):
    trace = daemon.daemon_trace()
    with caplog.at_level(logging.DEBUG, logger="coop.trace"):
        trace.add(1.0, "rule", gateway="lab:team", bot="a_bot", thread="t1", mechanism="rate", outcome="violated")
        trace.add(2.0, "sweep", gateway="lab:team", bot="a_bot", reason="timer", candidates=[])
    assert trace.events == []
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "coop.trace"] == [
        (logging.INFO, "rule gateway='lab:team' bot='a_bot' thread='t1' mechanism='rate' outcome='violated'"),
        (logging.DEBUG, "sweep gateway='lab:team' bot='a_bot' reason='timer' candidates=[]"),
    ]
