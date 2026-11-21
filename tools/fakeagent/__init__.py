"""fakeagent: a scripted agent on the doorbell.

It answers wakes through the same :class:`~coop.client.Doorbell` the real CLI and
the utility use — poll, read, send, done — from a script of rules, so the gateway can
be driven and measured without any real agent: the mechanism tests in ``tests/coop``
host it in process, the scenario runner (``python -m tools.fakeagent``) replays the
simulator's scenarios through the gateway core, and the same loop runs against a
live daemon once the HTTP doorbell exists.
"""
