# 3. The runtime directory is the base of every relative path

- **Status:** Accepted
- **Date:** 2026-09-25
- **Issue:** #182 (from PR #181 round 9, `docs/agents/finding-triage-log.md`)

## Context

`config.yaml` holds paths — `agents.*.working_directory`, `context_inject_files` at
three layers, `attachments.cache_dir_global` — and a relative one has to be resolved
against something. The loader used the directory of the file it was handed
(`config_dir = Path(path).parent`), which `docs/requirements.md` §8.2 stated as a
requirement.

The file is read in more than one way, and the base moved with each:

- the daemon reads `~/.agentcoop/config.yaml` unresolved, because a repointed
  symlink must be followed on `config reload`;
- the config tool validates a temp copy **beside the resolved target** before
  writing (PR #181), so through the Docker mode-1 symlink
  (`~/.agentcoop/config.yaml → ~/.agentcoop/config/config.yaml`) a relative
  `working_directory` resolved against `config/` at save time and against
  `~/.agentcoop/` at run time — a valid config was refused as "introduces a new
  problem", naming a directory the operator never wrote;
- `coop config validate --config <elsewhere>` resolved against `<elsewhere>`.

The candidate fixes in the save path — a probe file beside the link, copying across
directories, reverting to writing the link — each traded one failure for another
(EXDEV across a bind mount; the silent loss of an edit to a container-local file).
The layer that owns the base is the loader, and the only base that makes every
reader agree is one that does not depend on the path at all.

## Decision

Every relative path in `config.yaml` resolves against the **runtime directory**:
`$COOP_HOME` when set, otherwise `~/.agentcoop`. The same directory is the base of
every runtime path — state, logs, the control socket, `config.yaml`'s own default
location, `admin-profiles.yaml`, the attachment cache default, coop-keeper, the
`repo/` clone. `gateway/paths.py` is its one definition; `gateway/config.py`
exposes it as `config_base_dir()` and `resolve_working_directory()`, and the config
tool's inline hint calls the loader rather than mirroring it.

`COOP_HOME` must be absolute (a relative base would reintroduce the ambiguity by
another route) and is read once, at import. `install.sh` installs under it and
exports a non-default value from the shell rc files. `COOP_CONFIG` remains an
override of the config *file* alone.

Absolute paths and `~` paths are used as written: `~` is the user's home, which is
what the operator typed, not a synonym for the base.

## Consequences

- Symlinks, `--config` locations and where a validation copy is written stop
  mattering; `config_edit.validate_document` no longer needs to place its temp file
  beside the config. `EditableConfig.save()` still writes beside the target, for
  `os.replace` atomicity, not for path resolution.
- A released-semantics change with no compatibility layer (owner's standing ruling):
  a config kept outside the runtime directory that relied on relative paths must
  make them absolute. Recorded in the changelog and the user guide.
- Shipped examples write `working_directory: work` rather than
  `~/.agentcoop/work`, so they follow a non-default `COOP_HOME`.
- Docker keeps `/root/.agentcoop` throughout (owner's ruling: internal testing only);
  the entrypoint does not read `COOP_HOME`.
