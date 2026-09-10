"""Standalone RC/MM administrative CLI (user/channel provisioning).

Deliberately decoupled from AgentCoop's own config.yaml / Connector runtime:
this package reads its own profile file (see config.py) and drives the
existing RocketChatREST / MattermostREST clients directly, for three
reasons (see the profile-based design in config.py):

  1. Seeding lab/test environments with users and channels.
  2. Reusable as a skill for a future onboarding agent.
  3. A future home for on-demand agent provisioning (create a scoped
     RC/MM account + persona on the fly, tear it down afterward) — this
     is why PlatformAdmin is a small, generic ABC rather than baking in
     lab-specific assumptions.

Not yet wired into gateway/cli.py or config.yaml; run via
``python -m gateway.admin`` or the ``coop-provision`` console script.
"""
