"""gateway/tools/tui.py — Interactive REPL for testing agent backends.

A lightweight developer tool that lets you chat directly with any configured
agent backend (Claude, opencode, …) from the terminal, without running the
full Rocket.Chat connector stack.

Interactive permission prompts are ON by default: whenever Claude wants to call
a tool, the tool details are displayed and you can approve or deny in the terminal.
Use --no-permissions to disable.

Usage::

    # From the agent-chat-gateway directory:
    uv run python -m gateway.tools.tui

    # Specific agent, simulated guest role:
    uv run python -m gateway.tools.tui --agent assistance --role guest

    # Resume an existing session:
    uv run python -m gateway.tools.tui --session <session_id>

    # Inject a prefix (simulate RC message format):
    uv run python -m gateway.tools.tui --prefix "[Rocket.Chat #general | from: alice | role: owner]"

    # Disable interactive permission prompts:
    uv run python -m gateway.tools.tui --no-permissions

In-REPL commands (type while chatting)::

    /agents            List available agents from config
    /switch <name>     Switch to a different agent (starts a new session)
    /role owner|guest  Change the simulated role (env vars only, no new session)
    /session <id>      Attach to an existing session
    /new               Start a fresh session for the current agent
    /info              Show current session info
    /prefix [text]     Set or clear the message prefix injected before each prompt
    /quit              Exit

Dependencies::

    uv add rich          # pretty output — required
    readline (stdlib)    # line-editing / history — automatically used if available
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    print(
        "Error: 'rich' is required.\n"
        "Install it with:  uv add rich\n"
        "Or:               pip install rich",
        file=sys.stderr,
    )
    sys.exit(1)

# Enable readline line-editing + history when available (macOS/Linux stdlib)
try:
    import readline  # noqa: F401
except ImportError:
    pass

from gateway.agents.claude.adapter import ClaudeBackend
from gateway.agents.opencode.adapter import OpenCodeBackend
from gateway.agents.session import AgentSession, PermissionHandler
from gateway.config import GatewayConfig
from gateway.tools.tui_permission_handler import tui_permission_handler as _tui_permission_handler

console = Console()
err_console = Console(stderr=True, style="red")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_backend(agent_cfg, timeout: int):
    """Instantiate the right AgentBackend from an AgentConfig."""
    if agent_cfg.type == "claude":
        return ClaudeBackend(agent_cfg.command, agent_cfg.new_session_args, timeout)
    elif agent_cfg.type == "opencode":
        return OpenCodeBackend(agent_cfg.command, agent_cfg.new_session_args, timeout)
    else:
        raise ValueError(f"Unknown agent type: {agent_cfg.type!r}")


def _render_metadata(response, elapsed_sec: float) -> None:
    """Render a compact metadata panel below the agent response."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()

    sid = response.session_id or "n/a"
    grid.add_row("session", f"[dim]{sid}[/dim]")

    if response.usage:
        u = response.usage
        tok = f"[cyan]{u.input_tokens:,}[/cyan]↑  [green]{u.output_tokens:,}[/green]↓"
        if u.cache_read_tokens:
            tok += f"  [dim]{u.cache_read_tokens:,} cached[/dim]"
        grid.add_row("tokens", tok)

    if response.cost_usd is not None:
        grid.add_row("cost", f"[yellow]${response.cost_usd:.4f}[/yellow]")

    dur_ms = response.duration_ms if response.duration_ms is not None else int(elapsed_sec * 1000)
    grid.add_row("time", f"{dur_ms:,} ms")

    if response.num_turns is not None:
        grid.add_row("turns", str(response.num_turns))

    if response.is_error:
        grid.add_row("status", "[bold red]ERROR[/bold red]")

    console.print(Panel(grid, style="dim", border_style="dim", padding=(0, 1)))


async def _async_input(prompt: str) -> str:
    """Non-blocking input() so asyncio event loop keeps running."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------

class TUIState:
    """Mutable state for the TUI loop — avoids leaking mutable dicts."""

    def __init__(
        self,
        cfg: GatewayConfig,
        agent_name: str,
        role: str,
        prefix: str,
        session_id: str | None,
        cwd: str,
        interactive_permissions: bool = False,
    ):
        self.cfg = cfg
        self.cwd = cwd
        self.agent_name = agent_name
        self.role = role
        self.prefix = prefix  # injected before every prompt
        self.interactive_permissions = interactive_permissions

        self._session: AgentSession | None = None
        self._session_entered = False

        # Initialise first session
        self._init_session(session_id)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _close_session(self) -> None:
        """Exit the current session context if it was entered.

        Stops the permission broker (if any) and cleans up resources.
        Must be called before replacing self._session.
        """
        if self._session is not None and self._session_entered:
            await self._session.__aexit__(None, None, None)
        self._session_entered = False

    def _init_session(self, session_id: str | None = None) -> None:
        """(Re-)create the AgentSession, optionally resuming session_id.

        Callers must await _close_session() before calling this to ensure
        the previous session's broker is stopped cleanly.
        """
        agent_cfg = self.cfg.agents[self.agent_name]
        backend = _build_backend(agent_cfg, agent_cfg.timeout)
        handler: PermissionHandler | None = None
        if self.interactive_permissions:
            handler = _tui_permission_handler
        self._session = AgentSession(
            backend=backend,
            working_directory=self.cwd,
            timeout=agent_cfg.timeout,
            session_title=f"tui:{self.agent_name}",
            session_id=session_id,
            permission_handler=handler,
        )
        self._session_entered = False

    async def ensure_session(self) -> None:
        """Enter the session context (creates / resumes) on first use."""
        if not self._session_entered:
            await self._session.__aenter__()
            self._session_entered = True
            sid = self._session.session_id
            console.print(f"[dim]  → session: {sid}[/dim]")

    async def new_session(self) -> None:
        await self._close_session()
        self._init_session()
        console.print("[yellow]New session will be created on next message.[/yellow]")

    async def switch_agent(self, agent_name: str) -> None:
        await self._close_session()
        self.agent_name = agent_name
        self._init_session()
        console.print(
            f"Switched to [bold]{agent_name}[/bold] "
            f"([cyan]{self.cfg.agents[agent_name].type}[/cyan]). "
            "New session on next message."
        )

    async def attach_session(self, session_id: str) -> None:
        await self._close_session()
        self._init_session(session_id)
        console.print(f"Will resume session: [cyan]{session_id}[/cyan]")

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(self, text: str) -> tuple:
        """Send a message and return (AgentResponse, elapsed_sec)."""
        await self.ensure_session()

        env = {"COOP_ROLE": self.role}

        prompt = f"{self.prefix} {text}".strip() if self.prefix else text

        t0 = time.time()
        response = await self._session.send(prompt=prompt, env=env)
        elapsed = time.time() - t0

        # Keep session_id up to date if the backend rotated it
        if response.session_id:
            self._session.session_id = response.session_id

        return response, elapsed

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def info_lines(self) -> list[str]:
        agent_cfg = self.cfg.agents[self.agent_name]
        sid = self._session.session_id if self._session else "(not started)"
        perms_status = "[green]on[/green]" if self.interactive_permissions else "[dim]off[/dim]"
        return [
            f"  agent:   [bold]{self.agent_name}[/bold] ([cyan]{agent_cfg.type}[/cyan])",
            f"  role:    [bold]{self.role}[/bold]",
            f"  session: [dim]{sid}[/dim]",
            f"  cwd:     [dim]{self.cwd}[/dim]",
            f"  prefix:  [dim]{self.prefix or '(none)'}[/dim]",
            f"  perms:   {perms_status}",
        ]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_tui(
    config_path: str,
    agent_name: str | None,
    role: str,
    prefix: str,
    session_id: str | None,
    cwd: str,
    interactive_permissions: bool = False,
) -> None:
    # ---- Load config -------------------------------------------------------
    try:
        cfg = GatewayConfig.from_file(config_path)
    except FileNotFoundError:
        err_console.print(f"Config not found: {config_path}")
        return
    except Exception as exc:
        err_console.print(f"Config error: {exc}")
        return

    available = list(cfg.agents.keys())
    # No `default_agent:` to fall back to (removed with the loader's implicit
    # agent resolution). A single-agent config still needs no flag; anything
    # else has to be named, rather than picked by document order.
    if agent_name:
        resolved_agent = agent_name
    elif len(available) == 1:
        resolved_agent = available[0]
    else:
        err_console.print(
            "Multiple agents are configured — pass --agent NAME.\n"
            f"Available: {', '.join(available)}"
        )
        return

    if resolved_agent not in cfg.agents:
        err_console.print(
            f"Agent '{resolved_agent}' not found in config.\n"
            f"Available: {available}"
        )
        return

    # ---- Build state -------------------------------------------------------
    state = TUIState(
        cfg=cfg,
        agent_name=resolved_agent,
        role=role,
        prefix=prefix,
        session_id=session_id,
        cwd=cwd,
        interactive_permissions=interactive_permissions,
    )

    # ---- Header ------------------------------------------------------------
    console.rule("[bold blue]agent-chat-gateway TUI[/bold blue]")
    for line in state.info_lines():
        console.print(line)
    console.print()
    console.print(
        "[dim]Commands: /agents  /switch <name>  /role owner|guest  "
        "/session <id>  /new  /prefix [text]  /info  /quit[/dim]"
    )
    console.rule()

    # ---- REPL loop ---------------------------------------------------------
    while True:
        prompt_str = f"\n[{state.agent_name}|{state.role}] > "
        try:
            user_input = await _async_input(prompt_str)
        except EOFError:
            await state._close_session()
            console.print("\n[dim]EOF — bye![/dim]")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # ---- Commands -------------------------------------------------------
        if user_input in ("/quit", "/exit", "/q"):
            await state._close_session()
            console.print("[dim]Bye![/dim]")
            break

        elif user_input == "/agents":
            console.print(f"Available: {available}")
            console.print(f"Current:   [bold]{state.agent_name}[/bold]")

        elif user_input == "/info":
            for line in state.info_lines():
                console.print(line)

        elif user_input == "/new":
            await state.new_session()

        elif user_input.startswith("/switch "):
            name = user_input[8:].strip()
            if name not in cfg.agents:
                console.print(f"[red]Unknown agent '{name}'.[/red] Available: {available}")
            else:
                await state.switch_agent(name)

        elif user_input.startswith("/role "):
            new_role = user_input[6:].strip()
            if new_role not in ("owner", "guest"):
                console.print("[red]Role must be 'owner' or 'guest'.[/red]")
            else:
                state.role = new_role
                console.print(f"Role set to [bold]{new_role}[/bold] (takes effect next message).")

        elif user_input.startswith("/session "):
            sid = user_input[9:].strip()
            await state.attach_session(sid)

        elif user_input.startswith("/prefix"):
            rest = user_input[7:].strip()
            state.prefix = rest
            if rest:
                console.print(f"Prefix set to: [dim]{rest}[/dim]")
            else:
                console.print("Prefix cleared.")

        elif user_input.startswith("/"):
            console.print(f"[red]Unknown command:[/red] {user_input}")

        # ---- Normal message -------------------------------------------------
        else:
            console.print("[dim]⏳ Thinking…[/dim]")
            try:
                response, elapsed = await state.send(user_input)
            except asyncio.TimeoutError:
                agent_cfg = cfg.agents[state.agent_name]
                console.print(
                    f"[red]⏱ Timeout[/red] — agent did not respond within "
                    f"{agent_cfg.timeout}s."
                )
                continue
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted.[/yellow]")
                continue
            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")
                continue

            console.print()
            if response.is_error:
                console.print(
                    Panel(
                        response.text,
                        title="[red]Agent Error[/red]",
                        border_style="red",
                    )
                )
            else:
                console.print(
                    Panel(
                        Markdown(response.text),
                        title=f"[green]{state.agent_name}[/green]",
                        border_style="green",
                    )
                )

            _render_metadata(response, elapsed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="gateway-tui",
        description="Interactive REPL for testing agent-chat-gateway agent backends.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: config.yaml in current directory)",
    )
    parser.add_argument(
        "--agent",
        default=None,
        metavar="NAME",
        help="Agent name to use (required unless the config has exactly one agent)",
    )
    parser.add_argument(
        "--role",
        default="owner",
        choices=["owner", "guest"],
        help="Simulated role — controls which env vars are injected (default: owner)",
    )
    parser.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help="Resume an existing agent session instead of creating a new one",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        metavar="PATH",
        help="Working directory for the agent subprocess (default: current directory)",
    )
    parser.add_argument(
        "--prefix",
        default="",
        metavar="TEXT",
        help=(
            "Text to prepend to every prompt (e.g. a Rocket.Chat role header). "
            'Example: --prefix "[Rocket.Chat #dev | from: alice | role: owner]"'
        ),
    )
    parser.add_argument(
        "--permissions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Interactive permission prompts: whenever Claude wants to call a tool, "
            "display the tool details and ask you to allow or deny in the terminal "
            "(default: on).  Use --no-permissions to disable.  "
            "Only effective for Claude (claude) backends."
        ),
    )

    args = parser.parse_args(argv)
    cwd = args.cwd or os.getcwd()

    try:
        asyncio.run(
            run_tui(
                config_path=args.config,
                agent_name=args.agent,
                role=args.role,
                prefix=args.prefix,
                session_id=args.session,
                cwd=cwd,
                interactive_permissions=args.permissions,  # True by default
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")


if __name__ == "__main__":
    main()
