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

This will:
1. Clone the repo to `~/agentcoop`
2. Install dependencies with `uv sync`
3. Create symlinks at `~/.local/bin/coop` and `~/.local/bin/coop-provision`
4. Launch the interactive setup wizard

### Option B: AI-guided install with Claude Code

Ask Claude Code to install AgentCoop:

```
claude "Please install AgentCoop by following the instructions at https://raw.githubusercontent.com/HammerMei/agentcoop/main/docs/install-agent.md"
```

Claude will read the install guide and walk you through the setup interactively.

### Option C: AI-guided install with opencode

```
opencode "Please install AgentCoop by following the instructions at https://raw.githubusercontent.com/HammerMei/agentcoop/main/docs/install-agent.md"
```

### Option D: Manual install

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

### 4. Run the setup wizard

```bash
coop onboard --repo-path ~/.agentcoop/repo
```

---

## Configuration

The `onboard` wizard creates two files in `~/.agentcoop/`:

| File | Purpose |
|------|---------|
| `config.yaml` | Connector, agent, and watcher definitions — including credentials, stored directly as plain values |
| `install_meta.json` | Install method and version (used by `upgrade`) |

`config.yaml` is chmod'd `0600` automatically (by the wizard, by `coop start`, and by the config TUI on every save), so putting credentials directly in it is safe as long as you don't commit your filled-in copy to version control. `$VAR`/`${VAR}` references are not expanded — if you're upgrading from an older setup that used a `.env` file, the next `coop start` (or opening `coop config`) migrates it into `config.yaml` automatically, one-time.

**Mattermost:** the `onboard` wizard only walks through Rocket.Chat setup today — it does not
yet generate a Mattermost `connectors:` block. To add a Mattermost connector, run the wizard
for your first (Rocket.Chat) connector as usual, then hand-edit `config.yaml` to add a second
connector with `type: mattermost` — see the [Connectors](user-guide.md#connectors) section of
the user guide for the full field reference and a worked example (including the
`server.team`/`server.token` fields Mattermost needs that Rocket.Chat doesn't).

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
- Wrong Rocket.Chat credentials — verify RC_URL, RC_USERNAME, RC_PASSWORD in `~/.agentcoop/.env`
- Wrong Mattermost credentials — verify `server.url`/`server.team`/`server.token` (or `username`/`password`) in `config.yaml`
- Bot account not added to the watched room in Rocket.Chat, or not a member of the configured `server.team` in Mattermost

### Permission denied errors

The `.env` file should be readable only by you:
```bash
chmod 600 ~/.agentcoop/.env
```

### Running onboard again

Re-running `onboard` when a config already exists offers three options:
1. Update existing (keeps old values, you can change them)
2. Start fresh (backs up old files with a timestamp)
3. Cancel

```bash
coop onboard
```
