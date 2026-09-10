# Docker (internal testing only)

The image and compose example in this directory exist for AgentCoop's own E2E
and CI runs. **They are not a supported way to install AgentCoop for
production**: the supported install is the shell installer on the host (see
[INSTALL.md](../INSTALL.md)), which is what the daemon, `coop upgrade` and the
documentation assume.

If you use the compose example anyway, you are on your own for upgrades,
volumes and secrets handling. `Dockerfile.coop` builds the image the
workflows push to `ghcr.io/hammermei/agentcoop`; `entrypoint.coop.sh`
generates a `config.yaml` from environment variables at container start.
