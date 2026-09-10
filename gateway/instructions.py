"""Bundled instruction document lookup for lazy-loaded AgentCoop docs."""

from __future__ import annotations

from pathlib import Path

BUILTIN_CONTEXTS_DIR = Path(__file__).parent / "contexts"
INSTRUCTION_DOCS = {
    "scheduling": "scheduling-context.md",
    "fetch-history": "fetch-history-context.md",
}


def instruction_names() -> list[str]:
    """Return the supported instruction names in stable CLI display order."""
    return sorted(INSTRUCTION_DOCS)


def read_instruction(name: str) -> str:
    """Return a bundled instruction document by name.

    Raises:
        ValueError: if the instruction name is unknown.
        OSError: if the bundled file cannot be read.
    """
    filename = INSTRUCTION_DOCS.get(name)
    if filename is None:
        supported = ", ".join(instruction_names())
        raise ValueError(f"Unknown instruction {name!r}; expected one of: {supported}")
    return (BUILTIN_CONTEXTS_DIR / filename).read_text(encoding="utf-8")
