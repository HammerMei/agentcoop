"""The production classifier: one fresh CLI process per call (spec §5.1 "receives
only": a fresh process carries no earlier request). The prompt goes in on stdin,
the answer is the process's stdout, parsed by :func:`parse_decision`. The default
command is Claude Code's one-shot mode; any command that reads a prompt on stdin and
writes the JSON answer works.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .classifier import ClassifierRequest, ClassifierResult, ParseError, parse_decision
from .prompt import render_classifier_prompt

logger = logging.getLogger(__name__)

DEFAULT_COMMAND = ("claude", "-p", "--output-format", "text")


class SubprocessClassifier:
    def __init__(self, command: tuple[str, ...] = DEFAULT_COMMAND, room: str = "", cwd: str | None = None) -> None:
        self.command = tuple(command)
        self.room = room
        self.cwd = cwd

    async def classify(self, request: ClassifierRequest) -> ClassifierResult:
        prompt = render_classifier_prompt(request, self.room)
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await process.communicate(prompt.encode())
            except asyncio.CancelledError:  # the core's timeout: the process goes with the call
                process.kill()
                raise
        except (OSError, ValueError) as exc:
            return ClassifierResult(error=f"{self.command[0]}: {exc}")
        if process.returncode != 0:
            return ClassifierResult(error=f"{self.command[0]} exited {process.returncode}: "
                                          f"{err.decode(errors='replace').strip()[:200]}")
        try:
            decision = parse_decision(out.decode(errors="replace"), request)
        except ParseError as exc:
            return ClassifierResult(error=f"unparseable answer: {exc}")
        logger.debug("%s: classifier answered in %.1fs", self.room, time.monotonic() - started)
        return ClassifierResult(decision=decision)
