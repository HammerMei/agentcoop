"""Interactive setup wizard for AgentCoop."""

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from .paths import RUNTIME_DIR

CONFIG_FILE = RUNTIME_DIR / "config.yaml"
ENV_FILE = RUNTIME_DIR / ".env"
META_FILE = RUNTIME_DIR / "install_meta.json"

# Source path for the opencode role-enforcement plugin (relative to this file).
_PLUGIN_SRC = Path(__file__).parent / "agents" / "opencode" / "hooks" / "role-enforcement.ts"

# Global opencode config dir — plugin is installed here so it applies to ALL
# opencode sessions, not just a single project directory.
_GLOBAL_OPENCODE_DIR = Path.home() / ".opencode"

console = Console()

# Version read from pyproject.toml at module load time (best-effort).
def _read_project_version() -> str:
    """Read version from pyproject.toml in the same repo as this file."""
    try:
        here = Path(__file__).parent.parent
        toml_path = here / "pyproject.toml"
        text = toml_path.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                # version = "0.1.0"
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "0.1.0"


PROJECT_VERSION = _read_project_version()

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def detect_agent_backends() -> dict[str, str]:
    """Return {backend_name: version_string} for installed backends."""
    backends: dict[str, str] = {}
    for name, cmd in [("claude", "claude"), ("opencode", "opencode")]:
        try:
            result = subprocess.run(
                [cmd, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                version = (result.stdout.strip() or result.stderr.strip() or name)
                backends[name] = version
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    return backends


def generate_config_yaml(
    agent_type: str,
    connector_type: str,
    connector_data: dict,
    watchers: list[dict],
    working_directory: str | None = None,
) -> str:
    """Generate config YAML string. Public for testing.

    Args:
        agent_type: ``"claude"`` or ``"opencode"``.
        connector_type: ``"rocketchat"`` (only supported type currently).
        connector_data: Connector-specific fields (owners, server_url, …).
        watchers: List of ``{"name": ..., "room": ...}`` dicts.
        working_directory: Absolute path to the agent's working directory.
            ``GatewayConfig.from_file`` requires every agent to have one
            (checked at config load, regardless of backend) — the caller
            (``run_onboard``) is responsible for always providing one, not
            just for opencode.
    """
    owners = connector_data.get("owners", [])

    # Fields matching the built-in dataclass defaults (attachments size/timeout,
    # reply_in_thread, permission_reply_in_thread, context_inject_files: []) are
    # intentionally omitted — restating a default adds noise without changing
    # behavior. Only deliberate, non-default choices are written out.
    connector = {
        "name": "rc-home",
        "type": "rocketchat",
        "server": {
            "url": connector_data["server_url"],
            "username": connector_data["bot_username"],
            "password": connector_data["bot_password"],
        },
        "allowed_users": {
            "owners": list(owners),
            "guests": [],
        },
    }

    agent_command = agent_type  # "claude" or "opencode"
    agent: dict = {
        "type": agent_type,
        "command": agent_command,
        # Permission gating defaults to on for a fresh setup — this is a
        # deliberate safety choice, not a restated default (AgentConfig
        # defaults permissions.enabled to False), so it stays explicit.
        "permissions": {
            "enabled": True,
            "timeout": 300,
        },
    }
    # Required for every backend — GatewayConfig.from_file rejects an agent
    # with no working_directory regardless of type. opencode additionally
    # needs this to find .opencode/opencode.json and the role-enforcement
    # plugin; claude just needs a cwd to run in and create files under.
    if working_directory:
        agent["working_directory"] = working_directory

    # One watcher rule covering every wizard-collected room (§2.6): named
    # rooms go into `rooms.include`, and any `@username` entry becomes the
    # 1:1-DM opt-in — DMs have no room name for a pattern to match, so
    # `direct: true` is how a rule claims them. The gateway creates each
    # room's watcher on its first message.
    named_rooms = [w["room"] for w in watchers if not w["room"].startswith("@")]
    wants_dms = any(w["room"].startswith("@") for w in watchers)
    rooms_block: dict = {}
    if named_rooms:
        rooms_block["include"] = named_rooms
    if wants_dms:
        rooms_block["direct"] = True
    watcher_entry = {
        "name": "my-rooms",
        "connector": "rc-home",
        "agent": "my-agent",
        "rooms": rooms_block,
    }

    config = {
        "connectors": [connector],
        "agents": {"my-agent": agent},
        "watcher_rules": [watcher_entry],
    }

    return yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_install_meta(meta_file: Path | None = None) -> dict:
    """Load install_meta.json. Returns {} if missing. Public for testing."""
    path = meta_file or META_FILE
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_install_meta(
    meta_file: Path,
    method: str,
    repo_path: Path | None,
    version: str,
) -> None:
    """Write install_meta.json. Public for testing."""
    meta = {
        "method": method,
        "repo_path": str(repo_path) if repo_path else None,
        "version": version,
        "installed_at": date.today().isoformat(),
    }
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(meta, indent=2))


def install_opencode_plugin(
    repo_path: Path | None = None,
    global_opencode_dir: Path | None = None,
) -> Path:
    """Install the AgentCoop role-enforcement plugin into the global opencode config dir.

    The plugin is installed at the **global** level (``~/.opencode/``) rather
    than inside a specific project directory.  This means it is available for
    every opencode session regardless of working directory, while remaining
    completely inert when ``COOP_ROLE`` is not set (i.e. normal CLI / web-UI use).

    Installation layout::

        ~/.opencode/plugins/role-enforcement.ts   ← plugin file (absolute path)
        ~/.opencode/opencode.json                 ← registers the plugin

    The plugin entry in ``opencode.json`` uses the **absolute path** to the
    installed file so that opencode resolves it correctly regardless of the
    current working directory.

    Args:
        repo_path: Path to the AgentCoop repo root.  Used as a fallback when the plugin
            source cannot be found next to this module file (editable install vs
            installed wheel).  Pass ``None`` to rely solely on the module-relative
            path.
        global_opencode_dir: Override the global opencode directory (default:
            ``~/.opencode``).  Exposed for testing.

    Returns:
        The absolute path of the installed plugin file.

    Raises:
        FileNotFoundError: If the plugin source file cannot be located.
    """
    # Locate plugin source.
    plugin_src = _PLUGIN_SRC
    if not plugin_src.exists() and repo_path is not None:
        plugin_src = (
            repo_path / "gateway" / "agents" / "opencode" / "hooks" / "role-enforcement.ts"
        )
    if not plugin_src.exists():
        raise FileNotFoundError(
            f"opencode plugin source not found: {plugin_src}. "
            "Make sure you are running from the AgentCoop repository."
        )

    # Install into global opencode dir.
    target_dir = (global_opencode_dir or _GLOBAL_OPENCODE_DIR) / "plugins"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "role-enforcement.ts"
    shutil.copy2(plugin_src, dest)

    # Use absolute path in opencode.json so path resolution is unambiguous
    # regardless of cwd when opencode runs.
    plugin_entry = str(dest)

    # Patch (or create) ~/.opencode/opencode.json to register the plugin.
    config_file = target_dir.parent / "opencode.json"
    if config_file.exists():
        try:
            oc_config: dict = json.loads(config_file.read_text())
        except json.JSONDecodeError:
            oc_config = {}
    else:
        oc_config = {}

    plugins: list[str] = oc_config.get("plugin", [])
    if plugin_entry not in plugins:
        plugins.append(plugin_entry)
    oc_config["plugin"] = plugins
    config_file.write_text(json.dumps(oc_config, indent=2) + "\n")

    return dest


# ---------------------------------------------------------------------------
# Wizard steps (internal)
# ---------------------------------------------------------------------------

def _step_detect_backend() -> str:
    """Step 1: detect and select agent backend. Returns backend name."""
    console.print("\n[bold cyan]Step 1:[/bold cyan] Detecting agent backends...")
    backends = detect_agent_backends()

    if not backends:
        console.print(
            Panel(
                "[red]No supported agent backend found.[/red]\n\n"
                "Please install one of:\n"
                "  • [bold]Claude Code[/bold]: https://claude.ai/download\n"
                "  • [bold]opencode[/bold]:    https://opencode.ai",
                title="Missing Backend",
                border_style="red",
            )
        )
        sys.exit(1)

    if len(backends) == 1:
        name = next(iter(backends))
        version = backends[name]
        console.print(f"  Found: [bold green]{name}[/bold green] ({version})")
        confirmed = Confirm.ask(f"  Use [bold]{name}[/bold] as the agent backend?", default=True)
        if not confirmed:
            console.print("[yellow]Aborted by user.[/yellow]")
            sys.exit(0)
        return name

    # Both found
    console.print("  Found multiple backends:")
    names = list(backends.keys())
    for i, n in enumerate(names, 1):
        console.print(f"    {i}) {n} ({backends[n]})")
    choice_str = Prompt.ask(
        "  Choose backend",
        choices=[str(i) for i in range(1, len(names) + 1)],
        default="1",
    )
    return names[int(choice_str) - 1]


def _step_select_connector() -> str:
    """Step 2: select connector type."""
    console.print("\n[bold cyan]Step 2:[/bold cyan] Connector selection")
    connectors = [
        ("rocketchat", "Rocket.Chat"),
    ]
    for i, (ctype, label) in enumerate(connectors, 1):
        console.print(f"  {i}) {label}")

    if len(connectors) == 1:
        console.print(f"  Auto-selecting: [bold green]{connectors[0][1]}[/bold green]")
        return connectors[0][0]

    choice_str = Prompt.ask(
        "  Choose connector",
        choices=[str(i) for i in range(1, len(connectors) + 1)],
        default="1",
    )
    return connectors[int(choice_str) - 1][0]


def _step_rocketchat_credentials() -> dict:
    """Step 3: gather Rocket.Chat credentials."""
    console.print("\n[bold cyan]Step 3:[/bold cyan] Rocket.Chat credentials")

    server_url = Prompt.ask("  Server URL (e.g. https://chat.example.com)")
    server_url = server_url.rstrip("/")

    bot_username = Prompt.ask("  Bot username")
    bot_password = Prompt.ask("  Bot password", password=True)

    while True:
        owners_raw = Prompt.ask("  Owner usernames (comma-separated, at least 1 required)")
        owners = [o.strip() for o in owners_raw.split(",") if o.strip()]
        if owners:
            break
        console.print("  [red]At least one owner username is required.[/red]")

    return {
        "server_url": server_url,
        "bot_username": bot_username,
        "bot_password": bot_password,
        "owners": owners,
    }


def _step_opencode_working_dir() -> Path:
    """Ask for the opencode working directory and ensure it exists."""
    console.print("\n[bold cyan]Step 3b:[/bold cyan] opencode working directory")
    console.print(
        "  opencode runs inside a project directory where it reads its config\n"
        "  and the AgentCoop role-enforcement plugin will be installed.\n"
        "  Use the directory that contains (or will contain) your project code."
    )
    while True:
        raw = Prompt.ask("  Working directory", default=str(Path.home()))
        working_dir = Path(raw).expanduser().resolve()
        if working_dir.exists():
            break
        create = Confirm.ask(
            f"  [yellow]{working_dir}[/yellow] does not exist. Create it?",
            default=True,
        )
        if create:
            working_dir.mkdir(parents=True, exist_ok=True)
            break
        # User declined to create — ask again
    return working_dir


def _step_watchers(owners: list[str]) -> list[dict]:
    """Step 4: configure watchers."""
    console.print("\n[bold cyan]Step 4:[/bold cyan] Rooms to watch")

    first_owner = owners[0]
    watchers = [
        {
            "name": f"dm-{first_owner}",
            "room": f"@{first_owner}",
        }
    ]
    console.print(f"  Auto-added DM watcher for [bold]{first_owner}[/bold]: room=@{first_owner}")

    while True:
        add_more = Confirm.ask("  Add another room to watch?", default=False)
        if not add_more:
            break
        room = Prompt.ask("  Room name (or @username for DM)")
        room = room.strip()
        # Derive watcher name: strip leading @, replace non-alnum with -
        import re
        base = room.lstrip("@")
        watcher_name = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-")
        watchers.append({"name": watcher_name, "room": room})
        console.print(f"  Added watcher [bold]{watcher_name}[/bold] → {room}")

    return watchers


def _handle_existing_config() -> bool:
    """Handle re-run when config already exists. Returns True to continue."""
    console.print(
        Panel(
            "[yellow]Config already exists[/yellow] at "
            f"[bold]{CONFIG_FILE}[/bold]",
            border_style="yellow",
        )
    )
    console.print("  1) Update existing")
    console.print("  2) Start fresh (backup old config)")
    console.print("  3) Cancel")
    choice = Prompt.ask("  Your choice", choices=["1", "2", "3"], default="1")

    if choice == "3":
        console.print("[yellow]Cancelled.[/yellow]")
        sys.exit(0)

    if choice == "2":
        import time
        ts = int(time.time())
        # Same `.config-backups/` directory the config TUI's own save()
        # (gateway/configtool/model.py) uses — one place, not two competing
        # conventions, and chmod'd 0700 since a backup can hold a plaintext
        # secret (an unmigrated password/token) same as the live files do.
        backup_dir = CONFIG_FILE.parent / ".config-backups"
        if CONFIG_FILE.exists() or ENV_FILE.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_dir.chmod(0o700)
        if CONFIG_FILE.exists():
            bak = backup_dir / f"config.yaml.bak.{ts}"
            shutil.copy2(CONFIG_FILE, bak)
            bak.chmod(0o600)
            console.print(f"  Backed up config to [dim]{bak}[/dim]")
        if ENV_FILE.exists():
            bak_env = backup_dir / f".env.bak.{ts}"
            shutil.copy2(ENV_FILE, bak_env)
            bak_env.chmod(0o600)
            console.print(f"  Backed up .env to [dim]{bak_env}[/dim]")

    return True  # continue with wizard


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_onboard(repo_path: Path | None = None) -> None:
    """Entry point called by CLI. repo_path is stored in install_meta.json."""
    console.print(
        Panel(
            "[bold]AgentCoop[/bold] setup wizard\n"
            "This will create your config at [dim]~/.agentcoop/[/dim]",
            title="Welcome",
            border_style="cyan",
        )
    )

    # Handle existing config
    if CONFIG_FILE.exists():
        _handle_existing_config()

    # Wizard steps
    agent_type = _step_detect_backend()
    connector_type = _step_select_connector()

    credentials = _step_rocketchat_credentials()

    # Every agent needs a working_directory (GatewayConfig.from_file requires
    # it regardless of backend). opencode additionally needs it to find
    # .opencode/opencode.json and the role-enforcement plugin, so it gets an
    # explicit interactive step; claude just needs a cwd to run in, so it
    # defaults quietly to ~/.agentcoop/work (created if missing).
    if agent_type == "opencode":
        working_dir = _step_opencode_working_dir()
    else:
        working_dir = RUNTIME_DIR / "work"
        working_dir.mkdir(parents=True, exist_ok=True)

    watchers = _step_watchers(credentials["owners"])

    # Summary
    console.print("\n[bold cyan]Summary[/bold cyan]")
    console.print(f"  Agent backend : [bold]{agent_type}[/bold]")
    console.print(f"  Working dir   : {working_dir}")
    console.print(f"  Connector     : [bold]{connector_type}[/bold]")
    console.print(f"  Server URL    : {credentials['server_url']}")
    console.print(f"  Bot username  : {credentials['bot_username']}")
    console.print(f"  Owners        : {', '.join(credentials['owners'])}")
    console.print(f"  Watchers      : {', '.join(w['name'] for w in watchers)}")

    confirmed = Confirm.ask("\nWrite these files?", default=True)
    if not confirmed:
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(0)

    # Write files
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    config_yaml = generate_config_yaml(
        agent_type, connector_type, credentials, watchers,
        working_directory=str(working_dir),
    )
    CONFIG_FILE.write_text(config_yaml)
    # Credentials are written directly into config.yaml (docs/design/
    # config-tool.md decision 6 revisited — no companion .env file), so it
    # gets chmod'd immediately, the same treatment .env used to get.
    CONFIG_FILE.chmod(0o600)
    console.print(f"\n  [green]✓[/green] Wrote {CONFIG_FILE}")

    method = "git" if repo_path and repo_path.exists() else "unknown"
    write_install_meta(META_FILE, method=method, repo_path=repo_path, version=PROJECT_VERSION)
    console.print(f"  [green]✓[/green] Wrote {META_FILE}")

    # Install opencode plugin into the global opencode config dir.
    if agent_type == "opencode":
        try:
            dest = install_opencode_plugin(repo_path=repo_path)
            console.print(
                f"  [green]✓[/green] Installed opencode plugin (global) → {dest}"
            )
        except FileNotFoundError as exc:
            console.print(f"  [yellow]⚠[/yellow] Plugin install skipped: {exc}")

    console.print(
        Panel(
            "[green]Setup complete![/green]\n\n"
            "Start the gateway:\n"
            "  [bold]coop start[/bold]\n\n"
            "Check status:\n"
            "  [bold]coop status[/bold]\n\n"
            "[dim]── Scheduling (optional) ──────────────────────────────[/dim]\n"
            "Your agent automatically receives the built-in context files\n"
            "(RC rules + scheduling commands) — no config needed.\n\n"
            "You can ask the agent to schedule tasks, or use the CLI:\n"
            "  [bold]coop schedule create WATCHER MSG --every 1d --at 09:00[/bold]\n"
            "  [bold]coop schedule list[/bold]\n\n"
            "To set a timezone for scheduled tasks, add to your connector in config.yaml:\n"
            "  [dim]connectors:\n"
            "    - name: rc-main\n"
            "      timezone: \"Asia/Taipei\"[/dim]",
            border_style="green",
        )
    )
