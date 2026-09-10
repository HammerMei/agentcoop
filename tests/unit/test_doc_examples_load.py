"""Every COMPLETE config example in the docs is loaded by the real loader.

A config example that does not load is worse than no example: it is copied, it
fails, and the failure looks like the reader's mistake. This has happened
repeatedly rather than once — a `rooms:` inheritance example in the migration
guide that raised a shadowing warning when run, and a `watcher_rules:` block in
docs/scheduling.md still written in the removed static shape (`room:`, no
`name:`) long after the cutover that removed it. Both were found by running
them, which is what this file does automatically.

**Only complete examples.** A block qualifies when it has all three of
`connectors:`, `agents:` and `watcher_rules:` AND every connector and agent in it
declares a `type:`. The second half is what separates a config from a fragment:
a block demonstrating one field (the three context-injection layers, say) lists a
connector by name with only that field, and demanding it load would force every
such fragment to carry a whole config it is not about. A block whose entries do
declare their types is one someone can copy whole.

`working_directory` is redirected to a real temp directory before loading: the
loader checks that it exists, and an example naturally writes something like
`~/.agentcoop/work` that a test machine has no reason to have. Nothing
else is rewritten, so a rule missing a required field still fails here.

Run with:
    uv run python -m pytest tests/unit/test_doc_examples_load.py -v
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import yaml

from gateway.config import GatewayConfig

DOCS = Path(__file__).resolve().parents[2] / "docs"
REQUIRED_SECTIONS = {"connectors", "agents", "watcher_rules"}
YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.S)


def _is_complete(document: dict) -> bool:
    """A copyable config, not a fragment demonstrating one field."""
    if not REQUIRED_SECTIONS <= set(document):
        return False
    connectors = document.get("connectors")
    agents = document.get("agents")
    if not isinstance(connectors, list) or not isinstance(agents, dict):
        return False
    entries = [c for c in connectors if isinstance(c, dict)]
    entries += [a for a in agents.values() if isinstance(a, dict)]
    if not entries:
        return False
    # `inherits:` supplies the type for an entry that omits it.
    return all("type" in e or "inherits" in e for e in entries)


def complete_examples() -> list[tuple[str, int, dict]]:
    """(doc name, 1-based line of the block, parsed document) per complete example."""
    found = []
    for path in sorted(DOCS.glob("*.md")):
        text = path.read_text()
        for match in YAML_BLOCK.finditer(text):
            try:
                document = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                continue  # a deliberately broken snippet, or not really YAML
            if isinstance(document, dict) and _is_complete(document):
                line = text[: match.start()].count("\n") + 1
                found.append((path.name, line, document))
    return found


class TestEveryCompleteExampleLoads(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp()) / "work"
        self.work.mkdir(parents=True)

    def _with_real_working_dirs(self, document: dict) -> dict:
        for block in ("agents", "agent_templates"):
            for entry in (document.get(block) or {}).values():
                if isinstance(entry, dict) and "working_directory" in entry:
                    entry["working_directory"] = str(self.work)
        # An agent that never names one still needs it: the loader requires it.
        for entry in (document.get("agents") or {}).values():
            if isinstance(entry, dict) and "inherits" not in entry:
                entry.setdefault("working_directory", str(self.work))
        return document

    def test_there_are_examples_to_check(self):
        """Guard on the guard: a regex that silently matches nothing would make
        every assertion below vacuous."""
        self.assertGreaterEqual(len(complete_examples()), 3, "extractor found nothing")

    def test_each_one_loads(self):
        for name, line, document in complete_examples():
            with self.subTest(doc=f"{name}:{line}"):
                path = Path(tempfile.mkstemp(suffix=".yaml")[1])
                path.write_text(yaml.safe_dump(self._with_real_working_dirs(document)))
                try:
                    config = GatewayConfig.from_file(path)
                except (ValueError, FileNotFoundError) as exc:
                    self.fail(
                        f"{name}, yaml block at line {line}, does not load:\n  "
                        + " ".join(str(exc).split())
                    )
                self.assertTrue(config.watcher_rules, "the example defines no rule")

    def test_every_rule_in_them_resolves_an_agent(self):
        """The specific thing this change made required. Stated separately so a
        future failure says which contract broke, not just "it did not load"."""
        for name, line, document in complete_examples():
            with self.subTest(doc=f"{name}:{line}"):
                path = Path(tempfile.mkstemp(suffix=".yaml")[1])
                path.write_text(yaml.safe_dump(self._with_real_working_dirs(document)))
                config = GatewayConfig.from_file(path)
                for rule in config.watcher_rules:
                    self.assertIn(
                        rule.agent, config.agents,
                        f"{name}: rule '{rule.name}' resolves to an agent that is not defined",
                    )


if __name__ == "__main__":
    unittest.main()
