"""Pure prompt construction — no side effects, no I/O.

Extracted from MessageProcessor._process() to make prompt logic independently
testable and to keep the processor focused on queue orchestration.
"""

from __future__ import annotations

from .config import WatcherConfig


def build_system_header(wc: WatcherConfig, agent_username: str) -> str:
    """Build the AgentCoop identity + multi-agent addressing header.

    Pure — no I/O, no agent calls. This is protocol-invariant content that
    must survive Claude Code's context compaction, so it is delivered via
    --append-system-prompt-file, not as a compactable user message.

    Args:
        wc: The watcher's config (provides name/room).
        agent_username: The agent's own RC username. When truthy, the
            ``## Multi-Agent Addressing`` block is appended so the agent
            knows how to interpret the ``to:`` field on message headers.

    Returns:
        The combined header string. Never empty — the identity block is
        always present.
    """
    header = (
        f"## Coop Session Identity\n"
        f"- **Watcher name:** `{wc.name}`\n"
        f"- **Room:** `{wc.room}`\n"
        f"- **Connector:** `{wc.connector}`\n"
    )
    if agent_username:
        header += (
            f"- **Your username:** `@{agent_username}`\n"
            f"\n"
            f"## Multi-Agent Addressing\n"
            f"Each message header includes a `to:` field. Use it to decide your response:\n"
            f"- `to: me` — message explicitly addressed to you → respond normally\n"
            f"- `to: @all` — room-wide explicit mention → broader fan-out is intentional; reply only with useful, non-duplicative input, otherwise output ONLY `<end-of-agent-chain>`\n"
            f"- `to: @<agent>` — addressed to another agent → stay silent unless you have something essential to add\n"
            f"- `to: me+@<agent>` / `to: me+@all+@<agent>` — addressed to you and other priority responders; if `@all` is present, broader fan-out is intentional but keep replies concise and non-duplicative\n"
            f"- `to: *` — no explicit agent mention → use judgment; respond only if you have something meaningful to contribute\n"
        )
    return header


def build_prompt(text: str, prefix: str | None, warnings: list[str] | None = None) -> str:
    """Build the final agent prompt from cleaned text, platform prefix, and warnings.

    Args:
        text: Already cleaned (mention-stripped) message text from the Connector.
        prefix: Platform-injected prompt prefix (RBAC boundary). Empty string
                or ``None`` when the connector does not inject a prefix.
        warnings: Attachment download warnings to append (too large, timed out, etc.).

    Returns:
        Assembled prompt string ready to pass to ``AgentBackend.send()``.
    """
    prompt = f"{prefix} {text}".strip() if prefix else text
    if warnings:
        prompt = prompt + "\n" + "\n".join(warnings)
    return prompt


def build_catchup_prompt(
    history_lines: list[str],
    anchor_prompt: str,
) -> str:
    """Build a catch-up prompt when the queue had >1 messages pending.

    Called by MessageProcessor._process_batch() when an agent is lagged
    and multiple messages accumulated while it was processing a previous turn.
    Instead of processing them serially (each blind to the others), the agent
    receives the full backlog as a structured [CATCH-UP] header and responds
    to the most recent message (anchor) with complete context.

    Args:
        history_lines: Formatted prompt lines for all queued messages EXCEPT
            the anchor.  Each line is the output of
            ``build_prompt(msg.text, connector.format_prompt_prefix(msg))``
            (no warnings — history is context only).
        anchor_prompt: Fully-assembled prompt for the anchor (last) message,
            including all aggregated warnings from the entire batch.

    Returns:
        Complete catch-up prompt string.

    Example output::

        [CATCH-UP: The following messages arrived while you were processing your last response]
          [Rocket.Chat #general | from: agent2_bot | role: owner | day: Wed | ts: 2026-05-06T10:02:15+08:00 | to: *] Hi from Agent 2
          [Rocket.Chat #general | from: glin | role: owner | day: Wed | ts: 2026-05-06T10:04:22+08:00 | to: *] do xyz
        [END CATCH-UP]

        Latest message (respond to this):
        [Rocket.Chat #general | from: agent2_bot | role: owner | day: Wed | ts: 2026-05-06T10:05:30+08:00 | to: *] I already handled do xyz
    """
    header = (
        "[CATCH-UP: The following messages arrived while you were "
        "processing your last response]"
    )
    indented = "\n".join(f"  {line}" for line in history_lines)
    footer = "[END CATCH-UP]"
    return (
        f"{header}\n{indented}\n{footer}\n\n"
        f"Latest message (respond to this):\n{anchor_prompt}"
    )
