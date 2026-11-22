"""The operator's commands (design §3.1): the daemon's life, what it hosts, and the
holds on one bot in one room. They talk to the daemon over the control socket.
The agent's commands are a different program, :mod:`coop.agent`, and share
nothing with this one on purpose: the agent side stays small.

    coop start [--config PATH] [--foreground]
    coop stop
    coop status
    coop list
    coop pause  AGENT ROOM        ROOM is the handle `list` shows: <server>:<room name>
    coop resume AGENT ROOM
    coop reset  AGENT ROOM
    coop reload                   the daemon re-reads the configuration and applies it in place
    coop config add agent NAME --server S --username U --bot-token-file F [--owner O ...]
    coop config tls               a self-signed certificate for the doorbell, with its fingerprint
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import subprocess
import sys
from pathlib import Path

import yaml

from . import paths
from .config import ConfigError
from .control import send_command
from .fingerprint import fingerprint_of_file


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.run(args)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coop", description="the AgentCoop gateway daemon, for the operator")
    sub = p.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start the daemon")
    start.add_argument("--config", type=Path, default=paths.CONFIG_FILE)
    start.add_argument("--foreground", action="store_true", help="run in this terminal, log to stderr")
    start.set_defaults(run=cmd_start)
    sub.add_parser("stop", help="stop the daemon").set_defaults(run=cmd_stop)
    sub.add_parser("status", help="what the daemon hosts and who is online").set_defaults(run=cmd_status)
    sub.add_parser("list", help="the hosted rooms and their bots").set_defaults(run=cmd_list)
    for name, doc in (("pause", "no wake for the bot in the room until resume"),
                      ("resume", "lift the pause"),
                      ("reset", "the bot's next wake in the room starts a fresh session")):
        c = sub.add_parser(name, help=doc)
        c.add_argument("agent")
        c.add_argument("room", help="<server>:<room name>, as `list` shows it")
        c.set_defaults(run=cmd_hold, hold=name)
    sub.add_parser("reload", help="re-read the configuration and apply it in place").set_defaults(run=cmd_reload)

    config = sub.add_parser("config", help="edit the configuration file")
    config.add_argument("--config", type=Path, default=paths.CONFIG_FILE)
    csub = config.add_subparsers(dest="what", required=True)
    add = csub.add_parser("add", help="add an entry")
    asub = add.add_subparsers(dest="kind", required=True)
    agent = asub.add_parser("agent", help="a remote agent and its bot on one server")
    agent.add_argument("name")
    agent.add_argument("--server", required=True)
    agent.add_argument("--username", required=True, help="the bot's username on the server")
    agent.add_argument("--bot-token-file", required=True, type=Path, help="the bot's platform token")
    agent.add_argument("--owner", action="append", default=[], help="an owner of this bot (default: the server's)")
    agent.set_defaults(run=cmd_config_add_agent)
    tls = csub.add_parser("tls", help="a self-signed certificate and key for the doorbell")
    tls.add_argument("--host", default="localhost", help="the name on the certificate")
    tls.set_defaults(run=cmd_config_tls)
    return p


# ── the daemon ──────────────────────────────────────────────────────────────


def cmd_start(args) -> int:
    from . import daemon

    if args.foreground:
        return daemon.run_foreground(args.config)
    return daemon.start_daemon(args.config)


def cmd_stop(args) -> int:
    from . import daemon

    return daemon.stop_daemon()


def cmd_status(args) -> int:
    data = _ask({"cmd": "status"})
    if data is None:
        return 1
    print(f"doorbell: {data['listening'] or 'not listening'}")
    for server, bots in data["servers"].items():
        print(f"server {server}: bots {', '.join(bots) or 'none'}")
    for name, state in data["agents"].items():
        print(f"agent {name}: {'online' if state['online'] else 'offline'}"
              f"{', in a turn' if state['lease'] else ''}, bio v{state['bio_version']}")
    for handle, room in data["rooms"].items():
        for bot, state in room["bots"].items():
            flags = [f for f, on in (("paused", state["paused"]), ("wake", state["wake"] is not None)) if on]
            print(f"room {handle}: {bot} cursor={state['cursor']} unfinished={state['unfinished']}"
                  f"{' ' + ' '.join(flags) if flags else ''}")
    for note in data["notes"]:
        print(note, file=sys.stderr if note.startswith("[ERROR]") else sys.stdout)
    return 0


def cmd_list(args) -> int:
    data = _ask({"cmd": "list"})
    if data is None:
        return 1
    if not data:
        print("no hosted rooms")
    for handle, room in data.items():
        print(f"{handle}: hosted {', '.join(room['hosted'])}; roster {', '.join(room['agents'])}"
              f"{' (dm)' if room['dm'] else ''}")
    return 0


def cmd_hold(args) -> int:
    data = _ask({"cmd": args.hold, "agent": args.agent, "room": args.room})
    if data is None:
        return 1
    print(f"{args.hold}: {args.agent} in {args.room}")
    return 0


def cmd_reload(args) -> int:
    data = _ask({"cmd": "reload"}, timeout=120)
    if data is None:
        return 1
    for note in data["notes"]:
        print(note)
    print("configuration applied.")
    return 0


def _ask(request: dict, timeout: float = 60) -> dict | None:
    """One command to the daemon; on failure an ``[ERROR]`` line and ``None``."""
    try:
        response = asyncio.run(send_command(paths.SOCKET_FILE, request, timeout))
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        print(f"[ERROR] cannot reach the daemon at {paths.SOCKET_FILE}: {exc}", file=sys.stderr)
        return None
    if not response.get("ok"):
        print(f"[ERROR] {response.get('error', 'the daemon refused')}", file=sys.stderr)
        return None
    return response.get("data")


# ── the configuration ───────────────────────────────────────────────────────


def cmd_config_add_agent(args) -> int:
    """Adds the agent with a fresh bearer token and its bot on the server, then prints
    what the agent's host needs once: the token, the address, the fingerprint. The
    daemon picks it up on ``coop reload``."""
    raw = _read_yaml(args.config)
    if raw is None:
        return 1
    servers = raw.setdefault("servers", {}) or {}
    if args.server not in servers:
        print(f"[ERROR] servers.{args.server} is not in {args.config}", file=sys.stderr)
        return 1
    agents = raw.setdefault("agents", {}) or {}
    if args.name in agents:
        print(f"[ERROR] agents.{args.name} already exists", file=sys.stderr)
        return 1
    base = args.config.parent
    token_file = base / "agents" / args.name / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(secrets.token_urlsafe(32) + "\n")
    token_file.chmod(0o600)
    agents[args.name] = {"type": "remote", "token_file": str(token_file.relative_to(base))}
    bot: dict = {"username": args.username, "token_file": str(_relative(args.bot_token_file, base))}
    if args.owner:
        bot["owners"] = list(args.owner)
    server = servers[args.server] or {}
    bots = server.get("bots") or {}
    if args.name in bots:
        print(f"[ERROR] servers.{args.server}.bots.{args.name} already exists", file=sys.stderr)
        return 1
    bots[args.name] = bot
    server["bots"] = bots
    servers[args.server] = server
    raw["agents"], raw["servers"] = agents, servers
    try:
        config = _validate(raw, base)
    except ConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    _write_yaml(args.config, raw)
    print(f"agent {args.name} added to {args.config}; run `coop reload` to host it.")
    print("Give the agent's host, once:")
    print(f"  COOP_GATEWAY={_address(config.gateway.address, config.gateway.tls is not None)}")
    print(f"  COOP_TOKEN={token_file.read_text().strip()}")
    if config.gateway.tls is not None:
        print(f"  COOP_FINGERPRINT={fingerprint_of_file(config.gateway.tls[0])}")
    return 0


def cmd_config_tls(args) -> int:
    raw = _read_yaml(args.config)
    if raw is None:
        return 1
    base = args.config.parent
    cert, key = base / "gateway" / "cert.pem", base / "gateway" / "key.pem"
    gateway = raw.setdefault("gateway", {}) or {}
    gateway["tls"] = {"cert": str(cert.relative_to(base)), "key": str(key.relative_to(base))}
    raw["gateway"] = gateway
    try:
        _validate(raw, base)  # before the files exist: a bad configuration leaves the live pair alone
    except ConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    cert.parent.mkdir(parents=True, exist_ok=True)
    command = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
               "-subj", f"/CN={args.host}", "-keyout", str(key), "-out", str(cert)]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) else exc
        print(f"[ERROR] openssl failed: {detail}", file=sys.stderr)
        return 1
    key.chmod(0o600)
    _write_yaml(args.config, raw)
    print(f"certificate {cert}, key {key}; restart the daemon to serve it.")
    print(f"  COOP_FINGERPRINT={fingerprint_of_file(cert)}")
    return 0


def _read_yaml(path: Path) -> dict | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"[ERROR] {path}: {exc}", file=sys.stderr)
        return None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        print(f"[ERROR] {path}: the document is not a mapping", file=sys.stderr)
        return None
    return raw


def _write_yaml(path: Path, raw: dict) -> None:
    """Rewritten whole: comments in the file do not survive an edit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)


def _validate(raw: dict, base: Path):
    from .config import parse

    return parse(json.loads(json.dumps(raw)), base)


def _relative(path: Path, base: Path) -> Path:
    try:
        return path.resolve().relative_to(base.resolve())
    except ValueError:
        return path


def _address(listen: str, tls: bool) -> str:
    host, _, port = listen.rpartition(":")
    if host in ("0.0.0.0", "::", ""):
        host = "<this host>"
    return f"{'https' if tls else 'http'}://{host}:{port}"


if __name__ == "__main__":
    sys.exit(main())
