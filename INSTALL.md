# Installing AgentCoop

## Prerequisites

- **Python 3.12+** — https://python.org
- **git** — https://git-scm.com (required by the installer and `upgrade` command)
- **uv** — https://docs.astral.sh/uv/getting-started/installation/
- **Agent backend** (at least one):
  - **Claude Code** — https://claude.ai/download
  - **opencode** — https://opencode.ai

---

## Quick Install

### Option A: One-line shell installer (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/HammerMei/agentcoop/main/install.sh | bash
```

> Everything below goes under `~/.agentcoop`, the runtime directory. If that
> directory is taken or unusable, set `COOP_HOME` **before the first install** —
> `COOP_HOME=/srv/coop bash install.sh` — and read `$COOP_HOME` wherever this page
> says `~/.agentcoop`. The installer exports the variable from `~/.bashrc`/`~/.zshrc`;
> a manual install must do that itself. `COOP_HOME` is for that conflict case only:
> changing it later does not move an existing installation (see the user guide,
> "Paths").

This will:
1. Clone the repo to `~/.agentcoop/repo`
2. Install dependencies with `uv sync`
3. Create symlinks at `~/.local/bin/coop` and `~/.local/bin/coop-provision`
4. Install coop-keeper, the built-in admin agent, to `~/.agentcoop/agents/builtin/coop-keeper/`

It installs files and nothing else. Configuration is done next, in a
conversation with coop-keeper (see [Set up your first bot](#set-up-your-first-bot)).

### Option B: Manual install

See the [Manual Steps](#manual-steps) section below.

---

## Manual Steps

### 1. Clone the repository

```bash
mkdir -p ~/.agentcoop
git clone https://github.com/HammerMei/agentcoop.git ~/.agentcoop/repo
```

### 2. Install dependencies

```bash
uv sync --project ~/.agentcoop/repo
```

### 3. Create the symlinks

```bash
mkdir -p ~/.local/bin
repo=~/.agentcoop/repo

# Deliberately refuses rather than replaces: it never moves or deletes anything, so
# there is no state in which your command could go missing. If a path is occupied,
# it shows you what is there and leaves it alone — decide yourself, then re-run.
for cmd in coop coop-provision; do
  link=~/.local/bin/"$cmd"
  if [ -e "$link" ] || [ -L "$link" ]; then
    echo "already exists, leaving it alone:"
    ls -ld "$link"
    continue
  fi
  ln -s "$repo/.venv/bin/$cmd" "$link"
done
```

`coop-provision` creates Rocket.Chat / Mattermost users and channels; the loop links
it alongside the gateway.

> **Occupied path?** Look at what `ls -ld` printed. A stale symlink can just be
> deleted (`rm ~/.local/bin/<name>`) — the file it pointed at is untouched. Something
> you wrote should be moved somewhere you choose, not deleted. Then re-run the block.
>
> `install.sh` does replace an occupied path — it moves a real file to
> `<name>.<timestamp>.bak` and reports where it went. This block does not try to
> match that: a snippet pasted into a shell cannot be made reliably transactional,
> and refusing is both shorter and impossible to get wrong.
>
> **If a link ends up missing or wrong** — a full disk, a read-only home, an
> interrupted run — nothing here needs unpicking by hand. Re-run `bash install.sh`
> from the repo, or `coop upgrade` on an existing install: both are
> idempotent and will put the links right. `ls -l ~/.local/bin/*.bak` shows anything
> that was moved aside.

> **Both commands are part of the installation.** `coop-provision` is not an
> optional extra: `install.sh` links it alongside the gateway, and
> `coop upgrade` keeps both links current. Leaving it out here does
> not stick — the next upgrade creates it — so link both, or link neither and use
> `<repo>/.venv/bin/<command>` directly.
>
> Re-running the block is safe and does nothing: a path it already linked is
> occupied, so the second run reports it and moves on.

Add `~/.local/bin` to your PATH if needed (add to `~/.zshrc` or `~/.bashrc`):
```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 4. Install coop-keeper

```bash
mkdir -p ~/.agentcoop/agents/builtin ~/.agentcoop/agents/user
cp -R ~/.agentcoop/repo/agents/coop-keeper ~/.agentcoop/agents/builtin/coop-keeper
```

(On a fresh install that is what `install.sh` does; afterwards `coop upgrade`
refreshes only the files coop-keeper ships and leaves anything you add there
alone.)

### 5. Record the install for `coop upgrade`

`coop upgrade` reads `~/.agentcoop/install_meta.json` to find the checkout;
`install.sh` writes it, a manual install has to:

```bash
cat > ~/.agentcoop/install_meta.json <<META
{
  "method": "git",
  "repo_path": "$HOME/.agentcoop/repo",
  "version": "$(grep '^version' ~/.agentcoop/repo/pyproject.toml | sed 's/version = "\(.*\)"/\1/')"
}
META
```

---

## Set up your first bot

Configuration is a conversation with **coop-keeper**, the built-in admin agent.
Run it from its directory with whichever coding CLI you use — OpenCode works
with any model it supports, including free ones:

```bash
cd ~/.agentcoop/agents/builtin/coop-keeper && opencode     # or: claude
```

Claude Code asks you to trust the directory the first time you open it there;
accept, or its permission rules are ignored and every command prompts.

Then say what you want — "add a bot called bob to my Mattermost at
https://mm.example, team lab, as a friendly release-notes writer". On a fresh
machine it first asks for the server and your own username there, writes an
empty profile to `~/.agentcoop/admin-profiles.yaml` for you to fill in with an
administrator's credentials in an editor, and checks that they work. It then
shows the whole plan — the account it will create, the rooms it will join, the
persona it will write, the exact configuration it will save, and whether the
gateway will be reloaded or started — and asks once. It never asks for, reads
or repeats a password: bot passwords are generated into a file it passes by
path, and it reads configuration only through commands that mask credentials.

What it manages, in `~/.agentcoop/`:

| Path | Purpose |
|------|---------|
| `config.yaml` | Connector, agent and watcher definitions — including credentials, stored directly as plain values, `0600` |
| `admin-profiles.yaml` | Administrator credentials per server, for `coop-provision`; you fill these in, `0600` |
| `agents/user/<agent>/` | Each agent's persona (`AGENTS.md`, plus a one-line `CLAUDE.md`) and working directory; `opencode.json`/`.claude/settings.json` pin the bot's CLI to its built-in agent |
| `agents/builtin/coop-keeper/` | Coop-keeper itself; `coop upgrade` refreshes the files it ships and leaves everything else there alone |
| `install_meta.json` | Install method and version (used by `upgrade`) |

Both platforms are supported the same way. The same commands coop-keeper drives
— `coop config add/remove/patch`, `coop-provision` — are documented in the
[user guide](docs/user-guide.md#editing-configuration-from-the-command-line)
for use by hand or from a script.

### Watcher room formats

- `@username` — direct message room with that user (both platforms)
- `roomname` — a Rocket.Chat channel or private group
- `channelname` — a Mattermost channel within the connector's configured `server.team`

---

## Upgrade

```bash
coop upgrade
```

This stops the daemon, runs `git pull` + `uv sync`, runs the pulled release's
post-upgrade steps, and restarts the daemon automatically.

> **One-time note for installs that predate `coop-provision`:** the post-upgrade
> step that puts new commands on your PATH is itself delivered by an upgrade, so
> the first upgrade that lands it cannot run it. If `coop-provision` is not found
> after upgrading, link it once:
>
> ```bash
> # Locate the managed virtualenv. Two things this deliberately avoids:
> #   * assuming ~/.agentcoop/repo — running `./install.sh` from a local
> #     checkout uses that checkout as the repo and records it in install_meta.json;
> #   * needing a system `python3` — install.sh may have installed Python with
> #     `uv python install`, which provides `python3.12` and not `python3`.
> # Both cases describe installs that predate coop-provision, i.e. this note's readers.
> bin=$(dirname "$(readlink ~/.local/bin/coop 2>/dev/null)" 2>/dev/null)
> if [ ! -x "$bin/coop-provision" ]; then
>   # The entrypoint is not a managed symlink (a wrapper of your own, say), so use
>   # the path the installer recorded.
>   repo=$(sed -n 's/.*"repo_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' ~/.agentcoop/install_meta.json)
>   bin=$repo/.venv/bin
> fi
> echo "$bin"          # sanity-check this before continuing
>
> link=~/.local/bin/coop-provision
> # Refuses rather than replaces, like the manual setup block above: nothing is moved
> # or deleted, so nothing can go missing.
> if [ -e "$link" ] || [ -L "$link" ]; then
>   echo "already exists, leaving it alone:"; ls -ld "$link"
> else
>   ln -s "$bin/coop-provision" "$link"
> fi
> ```
>
> Or run it without linking anything at all — same `$bin` as above:
> `"$bin/python" -m gateway.admin --help`.
> Later upgrades handle new commands on their own.

---

## Uninstall

```bash
# Stop the daemon
coop stop

# Remove the symlinks this install created. The -L test leaves a hand-written
# wrapper of your own at either path alone — uninstalling should remove what was
# installed, not something you wrote.
if [ -L ~/.local/bin/coop ]; then rm -f ~/.local/bin/coop; fi
if [ -L ~/.local/bin/coop-provision ];     then rm -f ~/.local/bin/coop-provision;     fi

# If the installer ever moved something of yours aside, it is still here. Check
# before deleting — this is the only copy, and nothing else cleans it up.
ls -l ~/.local/bin/*.bak 2>/dev/null

# Remove all data — repo, config, logs (this deletes everything!)
rm -rf ~/.agentcoop
```

---

## Troubleshooting

### `coop: command not found`

`~/.local/bin` is not in your PATH. Add it:
```bash
export PATH="$HOME/.local/bin:$PATH"
```
Then add the same line to your `~/.zshrc` or `~/.bashrc` so it persists.

### Gateway won't start

Check the log file:
```bash
tail -50 ~/.agentcoop/gateway.log
```

Common causes:
- Invalid config YAML — run `coop config validate` to check syntax, cross-references,
  and per-connector credentials without starting the daemon (add `--lint` to also flag redundant
  defaults)
- Wrong Rocket.Chat credentials — verify `server.url`/`server.username`/`server.password` in `config.yaml`
- Wrong Mattermost credentials — verify `server.url`/`server.team`/`server.token` (or `username`/`password`) in `config.yaml`
- Bot account not added to the watched room in Rocket.Chat, or not a member of the configured `server.team` in Mattermost

### Changing a bot later

Run coop-keeper again from its directory and say what should change — a
persona, the rooms a bot serves, a second server for an existing agent, or a
removal. Every change is shown as a plan before anything is written.
