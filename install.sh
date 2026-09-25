#!/usr/bin/env bash
# AgentCoop installer
# Usage:  bash install.sh [--force]
# Or:     curl -fsSL https://raw.githubusercontent.com/HammerMei/agentcoop/main/install.sh | bash
#
# Installs files and nothing else: uv/Python, the `coop` and `coop-provision`
# commands, the example context files, install_meta.json and the coop-keeper
# agent directory. Configuration is done afterwards by running coop-keeper
# with your own coding CLI — the script ends by printing the command.
#
# Everything goes under $COOP_HOME, default ~/.agentcoop — the repo clone,
# config.yaml, state, logs, coop-keeper. If that directory is taken or unusable,
# set the variable before the FIRST install:
#   COOP_HOME=/srv/coop bash install.sh
# The script then ends by printing the line to add to your shell startup file
# so later shells find it. Changing it later does not move an existing
# installation.
#
# Flags:
#   --force        Replace a `coop` command already on PATH that is not AgentCoop's
set -euo pipefail

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { printf '\033[0;36m[AgentCoop]\033[0m %s\n' "$*"; }
success() { printf '\033[0;32m[AgentCoop]\033[0m %s\n' "$*"; }
warn()    { printf '\033[0;33m[AgentCoop]\033[0m %s\n' "$*" >&2; }
error()   { printf '\033[0;31m[AgentCoop] Error:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Runtime directory — $COOP_HOME, default ~/.agentcoop (#182). The same rule as
# gateway/paths.py (COOP_HOME_RULE there): `~` expanded; then absolute,
# canonical, and spelled only with letters, digits, `.`, `_`, `-` and `/` —
# the value is embedded verbatim by every writer that follows.
# ---------------------------------------------------------------------------
coop_home_dir() {
  # Echo the runtime directory. Exit 1 (message on stderr) for a COOP_HOME that breaks the rule.
  local raw="${COOP_HOME:-}"
  if [ -z "$raw" ]; then
    printf '%s\n' "$HOME/.agentcoop"
    return 0
  fi
  case "$raw" in
    "~")   raw="$HOME" ;;
    "~/"*) raw="$HOME/${raw#\~/}" ;;
  esac
  case "$raw" in
    *[!A-Za-z0-9._/-]*|/|//*|*/|*//*|*/./*|*/.|*/../*|*/..|[!/]*)
      printf "COOP_HOME must be an absolute, canonical path (no '.', '..' or empty components, no trailing '/', not '/') using only letters, digits, '.', '_', '-' and '/' (got %s)\n" "$raw" >&2
      return 1 ;;
  esac
  printf '%s\n' "$raw"
}
coop_home_hint() {
  # $1 = runtime dir. The line that makes later shells find a non-default
  # COOP_HOME, in the login shell's own syntax ($SHELL). Printed for the
  # operator to add; the installer writes no shell startup file for it — a
  # value-carrying line in someone else's rc file has more edge cases (an
  # existing export, a commented one, quoting, a read-only file, fish/csh)
  # than the rare case is worth, and rc files never covered cron or a
  # service manager anyway.
  case "${SHELL:-}" in
    */fish)        printf "set -gx COOP_HOME '%s'\n" "$1" ;;
    */csh|*/tcsh)  printf "setenv COOP_HOME '%s'\n" "$1" ;;
    *)             printf "export COOP_HOME='%s'\n" "$1" ;;
  esac
}
runtime_dir_is_ours() {
  # $1 = runtime dir. 0 when it does not exist yet, or is a real directory owned
  # by the invoking user that other users cannot write to. Everything the
  # installer puts under it — the repo clone that `~/.local/bin/coop` runs from
  # above all — trusts the directory. This guards the accidental case: a
  # COOP_HOME typed under a shared parent such as /tmp, a stray symlink, a
  # world-writable mode. It is not a defence against another account on the
  # same host — multi-tenant hosts are outside what AgentCoop promises
  # (SECURITY.md, requirements §14.5), so group-writable directories, parent
  # directories and the like are deliberately not examined.
  [ -e "$1" ] || [ -L "$1" ] || return 0
  if [ -L "$1" ]; then
    printf '[ERROR] %s is a symbolic link; COOP_HOME must be a real directory.\n' "$1" >&2; return 1
  fi
  if [ ! -d "$1" ]; then
    printf '[ERROR] %s exists and is not a directory.\n' "$1" >&2; return 1
  fi
  if [ ! -O "$1" ]; then
    printf '[ERROR] %s is not owned by %s; COOP_HOME must be your own directory.\n' "$1" "$(id -un)" >&2; return 1
  fi
  if [ -n "$(find "$1" -maxdepth 0 -perm -o+w 2>/dev/null)" ]; then
    printf '[ERROR] %s is writable by other users; COOP_HOME must be a directory only you can write to.\n' "$1" >&2; return 1
  fi
  return 0
}
RUNTIME_DIR=$(coop_home_dir) || exit 1
runtime_dir_is_ours "$RUNTIME_DIR" || exit 1
# Only a COOP_HOME the operator set is exported (expanded) to the Python steps
# below: the default is not validated as an explicit value by gateway/paths.py,
# and a $HOME the explicit-value rule would refuse must keep installing.
if [ -n "${COOP_HOME:-}" ]; then
  export COOP_HOME="$RUNTIME_DIR"
fi

# ---------------------------------------------------------------------------
# OS / architecture detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin) OS_NAME="macos" ;;
  Linux)  OS_NAME="linux" ;;
  *)      error "Unsupported OS: $OS. This installer supports macOS and Linux." ;;
esac

info "Detected OS: $OS_NAME ($ARCH)"

# ---------------------------------------------------------------------------
# uv check / install (must come before Python check — uv can manage Python)
# ---------------------------------------------------------------------------
if ! command -v uv &>/dev/null; then
  warn "uv not found. Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Add to PATH for this session
  export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
  if ! command -v uv &>/dev/null; then
    error "uv installation failed. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
  fi
  success "uv installed."
else
  info "uv found: $(uv --version)"
fi

# ---------------------------------------------------------------------------
# Python 3.12+ check — use uv to install if system Python is too old
# ---------------------------------------------------------------------------
PYTHON_OK=false
if command -v python3 &>/dev/null; then
  PYTHON_VERSION="$(python3 --version 2>&1 | awk '{print $2}')"
  PYTHON_MAJOR="$(echo "$PYTHON_VERSION" | cut -d. -f1)"
  PYTHON_MINOR="$(echo "$PYTHON_VERSION" | cut -d. -f2)"
  if [ "$PYTHON_MAJOR" -gt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -ge 12 ]; }; then
    PYTHON_OK=true
    info "Python $PYTHON_VERSION — OK"
  fi
fi

if [ "$PYTHON_OK" = false ]; then
  warn "Python 3.12+ not found on system. Using uv to install Python 3.12..."
  uv python install 3.12
  success "Python 3.12 installed via uv."
fi

# ---------------------------------------------------------------------------
# Determine repo directory
# Detect curl|bash mode: BASH_SOURCE[0] is empty or is /dev/stdin or "bash"
# ---------------------------------------------------------------------------
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
CURL_PIPE=false

if [ -z "$SCRIPT_SOURCE" ] || [ "$SCRIPT_SOURCE" = "/dev/stdin" ] || [ "$SCRIPT_SOURCE" = "bash" ] || [ "$SCRIPT_SOURCE" = "-bash" ]; then
  CURL_PIPE=true
fi

if [ "$CURL_PIPE" = true ]; then
  REPO_DIR="$RUNTIME_DIR/repo"
  mkdir -p "$RUNTIME_DIR"
  info "Running via curl|bash — will clone to $REPO_DIR"
  if [ -d "$REPO_DIR/.git" ]; then
    info "Repo already exists at $REPO_DIR — pulling latest..."
    git -C "$REPO_DIR" pull
  else
    git clone https://github.com/HammerMei/agentcoop.git "$REPO_DIR"
  fi
else
  # Running as a local script — use the script's own directory
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_DIR="$SCRIPT_DIR"
  info "Running locally — using repo at $REPO_DIR"
fi

# ---------------------------------------------------------------------------
# Is `coop` on PATH already someone else's? Decided BEFORE uv sync, so a refusal
# costs nothing but the clone.
#
# `coop` is a short name and at least one other tool (AndrewDryga/coop, a sandbox
# runner for coding agents) installs a binary by that name into the same
# directory. link_console_script() would move such a file to a .bak and take the
# name — correct for a stale copy of OUR script, wrong for someone else's
# working command. "Ours" is a symlink shaped like the one this installer makes,
# `<some repo>/.venv/bin/coop` — from THIS repo or an earlier install elsewhere,
# dangling or not (a moved or deleted repo leaves exactly that, and re-running
# the installer is how it gets repaired). Anything else at that path belongs to
# something else. gateway/upgrade.py applies the same test.
# ---------------------------------------------------------------------------
is_ours_console_script() {
  # $1 = a path. 0 = a symlink shaped like AgentCoop's console script.
  [ -L "$1" ] || return 1
  case "$(readlink "$1")" in
    */.venv/bin/coop) return 0 ;;
  esac
  return 1
}

is_foreign_command() {
  # $1 = path on PATH. 0 = foreign (someone else's), 1 = ours or nothing there.
  [ -e "$1" ] || [ -L "$1" ] || return 1
  is_ours_console_script "$1" && return 1
  return 0
}

VENV_BIN="$REPO_DIR/.venv/bin/coop"
COOP_LINK="$HOME/.local/bin/coop"
if [ "$FORCE" != true ] && is_foreign_command "$COOP_LINK"; then
  warn "$COOP_LINK already exists and is not AgentCoop's:"
  warn "  $(ls -l "$COOP_LINK" 2>/dev/null | sed 's/^/  /')"
  warn "Either keep that command and, after installing, link AgentCoop under another name:"
  warn "    ln -s $VENV_BIN \$HOME/.local/bin/agentcoop"
  warn "or re-run the installer with --force to replace it (a regular file is kept as a .bak;"
  warn "a symlink is replaced outright)."
  error "Refusing to replace a command that is not ours (use --force)."
fi
# A `coop` found EARLIER on PATH than ~/.local/bin (say /usr/local/bin/coop) is a
# different problem: our link would be created cleanly and then never run,
# because the shell resolves the other one first. Nothing is clobbered, so this
# is a warning with the same alternate-name way out, not a refusal.
SHADOWING_COOP="$(command -v coop 2>/dev/null || true)"
if [ -n "$SHADOWING_COOP" ] && [ "$SHADOWING_COOP" != "$COOP_LINK" ] \
   && ! is_ours_console_script "$SHADOWING_COOP"; then
  warn "Another \`coop\` is on your PATH ahead of ~/.local/bin: $SHADOWING_COOP"
  warn "After installing, typing \`coop\` will run THAT program, not AgentCoop."
  warn "Either put ~/.local/bin earlier in PATH, or link AgentCoop under another name:"
  warn "    ln -s $VENV_BIN \$HOME/.local/bin/agentcoop"
fi

# ---------------------------------------------------------------------------
# uv sync
# ---------------------------------------------------------------------------
info "Installing Python dependencies (uv sync)..."
uv sync --project "$REPO_DIR"

# ---------------------------------------------------------------------------
# Symlink into ~/.local/bin
#
# link_console_script <target> <link>
#   Points <link> at <target>, never destroying anything the user put there.
#
#   already exactly this link -> nothing to do. Compares the readlink TEXT, so a
#                                relative or differently-spelled link to the same
#                                file counts as "not ours" and gets rewritten.
#                                (upgrade.py compares resolve() instead and would
#                                leave it alone — same end state, one extra
#                                rewrite here.)
#   any other symlink        -> removed and its old target reported. Nothing is
#                               preserved by keeping it: the file it pointed at is
#                               untouched, and a .bak symlink would accumulate on
#                               every re-run.
#   a real file or directory -> moved aside to <link>.<YYYYmmddHHMMSS>.bak — or
#                               <link>.<ts>-N.bak if that name is taken — and
#                               reported, THEN replaced. Note the timestamp comes
#                               BEFORE .bak: a cleanup glob is `*.bak`, never
#                               `*.bak.*`.
#
#   Backing up rather than refusing is deliberate. Refusing sounds safer but
#   leaves a worse state: install_meta.json is written unconditionally further
#   down, so `coop upgrade` would manage $REPO_DIR while PATH ran
#   whatever occupied the destination — a repo whose code never executes.
#   Backing up keeps the managed command working AND loses nothing.
#
#   `mv` on a symlink moves the link itself, it does not follow it, so the file
#   a foreign symlink pointed at is never touched.
#
#   `rm -f` + `ln -s` rather than `ln -sf`: when the destination is a symlink to
#   a DIRECTORY, `ln -sf` dereferences it and creates the link *inside* that
#   directory, then reports success while the path still resolves to a
#   directory. Reproduced on both BSD/macOS and GNU ln. `-n`/`-h` also fix it
#   but are not in POSIX, so the explicit unlink is the portable form.
#
#   Returns 0 if <link> now points at <target>, 1 otherwise. Every step is
#   tested inside an `if` because of `set -e`: run bare, a failing mv or ln
#   would abort the installer outright, and only the CALLER knows whether that
#   is warranted (fatal for the entrypoint, survivable for coop-provision).
# ---------------------------------------------------------------------------
link_console_script() {
  local target="$1" link="$2" bak="" n ts oldtarget=""

  if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
    success "Symlink already current: $link → $target"
    return 0
  fi

  if [ -L "$link" ]; then
    # A symlink, but not ours. Nothing is preserved by backing it up: the file
    # it points at is not touched, and a stale .bak symlink is pure litter. So
    # replace it — but SAY what it pointed at, because the actual complaint
    # about the old behaviour was that the change was silent, not that it
    # happened. Covers a dangling link too (readlink still reports the target).
    oldtarget="$(readlink "$link")"
    warn "$link pointed at $oldtarget — repointing it"
    if ! rm -f "$link"; then
      warn "Could not remove the existing symlink at $link"
      return 1
    fi
  elif [ -e "$link" ]; then
    # A real file or directory — something the user made by hand. This is what
    # gets preserved, because it cannot be reconstructed from a printed path.
    # Timestamped so repeated installs accumulate distinct backups instead of
    # one clobbering the next. The collision loop only matters for two runs
    # inside the same second, but `mv` overwrites silently, and losing a
    # previous backup is exactly the data loss this branch exists to prevent.
    ts="$(date +%Y%m%d%H%M%S)"
    bak="$link.$ts.bak"
    n=1
    while [ -e "$bak" ] || [ -L "$bak" ]; do
      bak="$link.$ts-$n.bak"
      n=$((n + 1))
    done
    if mv "$link" "$bak"; then
      warn "$link was not a symlink — backed it up before replacing:"
      warn "  $link → $bak"
    else
      warn "Could not back up $link — leaving it untouched."
      return 1
    fi
  fi

  if rm -f "$link" && ln -s "$target" "$link"; then
    success "Symlink created: $link → $target"
    return 0
  fi

  # Roll back. Getting here means the destination was cleared but the new link
  # could not be made (no inodes, a filesystem without symlinks, a race), so
  # without this the user is left with LESS than they started with — their
  # working command moved into a .bak, and for the entrypoint the caller then
  # aborts the install outright. A failure must never cost them the command.
  warn "Could not create $link"
  if [ -n "$bak" ]; then
    if mv "$bak" "$link"; then
      warn "  Restored what was there before: $link"
    else
      warn "  Could not restore it — your original is still at $bak"
    fi
  elif [ -n "$oldtarget" ]; then
    if ln -s "$oldtarget" "$link"; then
      warn "  Restored the previous symlink: $link → $oldtarget"
    else
      warn "  Could not restore the previous symlink to $oldtarget"
    fi
  fi
  return 1
}

if [ ! -f "$VENV_BIN" ]; then
  error "Expected binary not found: $VENV_BIN"
fi

mkdir -p "$HOME/.local/bin"

if ! link_console_script "$VENV_BIN" "$COOP_LINK"; then
  error "Could not install ~/.local/bin/coop"
fi

# coop-provision (RC/MM account & channel provisioning). A first-class command, not
# an optional extra: it is linked here and kept current by `coop
# upgrade`, and INSTALL.md documents both links together.
#
# Deliberately a WARNING rather than error() if absent, which is about INSTALL
# CRITICALITY and not about the command being dispensable: the gateway daemon can
# start and serve without it, so a missing or unlinkable provisioning CLI must not
# throw away an install that otherwise succeeded. A missing ENTRYPOINT is fatal
# because nothing works without that one.
PROVISION_BIN="$REPO_DIR/.venv/bin/coop-provision"
PROVISION_LINK="$HOME/.local/bin/coop-provision"
PROVISION_LINKED=false
if [ ! -f "$PROVISION_BIN" ]; then
  warn "coop-provision not found at $PROVISION_BIN — skipping its symlink."
elif link_console_script "$PROVISION_BIN" "$PROVISION_LINK"; then
  PROVISION_LINKED=true
else
  # NOT fatal, unlike the entrypoint above — see the criticality note at the top
  # of this block. The command still exists; only its PATH entry is missing.
  warn "  Run it directly at $PROVISION_BIN, or link it manually later."
fi

# ---------------------------------------------------------------------------
# PATH setup — add ~/.local/bin if missing
# ---------------------------------------------------------------------------
case ":$PATH:" in
  *":$HOME/.local/bin:"*)
    info "~/.local/bin is already in PATH."
    ;;
  *)
    warn "~/.local/bin is not in PATH. Adding to shell rc files..."
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
      if [ -f "$RC" ]; then
        # Only add if not already present
        if ! grep -qF '.local/bin' "$RC" 2>/dev/null; then
          printf '\n# Added by AgentCoop installer\n%s\n' "$PATH_LINE" >> "$RC"
          info "  Added to $RC"
        fi
      fi
    done
    export PATH="$HOME/.local/bin:$PATH"
    ;;
esac

# ---------------------------------------------------------------------------
# Ensure runtime dir exists (needed by both context copy and install_meta.json)
# ---------------------------------------------------------------------------
mkdir -p "$RUNTIME_DIR"
if [ "$RUNTIME_DIR" != "$HOME/.agentcoop" ]; then
  info "Runtime directory: $RUNTIME_DIR (COOP_HOME)"
fi

# ---------------------------------------------------------------------------
# Copy user-facing example context files to runtime dir.
# Built-in system files (rc-gateway-context.md, scheduling-context.md) are
# bundled inside the gateway Python package and auto-injected at runtime —
# they no longer need to be copied here.
# Only user-editable examples (e.g. rc-room-profiles.example.md) are copied.
# ---------------------------------------------------------------------------
if [ -d "$REPO_DIR/contexts" ] && [ -n "$(ls "$REPO_DIR/contexts/" 2>/dev/null)" ]; then
    mkdir -p "$RUNTIME_DIR/contexts"
    cp "$REPO_DIR/contexts/"* "$RUNTIME_DIR/contexts/"
    success "Copied example context files to $RUNTIME_DIR/contexts/"
fi

# ---------------------------------------------------------------------------
# Write install_meta.json — required by `coop upgrade` to locate the repo.
# ---------------------------------------------------------------------------
COOP_VERSION=$(grep '^version' "$REPO_DIR/pyproject.toml" | sed 's/version = "\(.*\)"/\1/')
cat > "$RUNTIME_DIR/install_meta.json" << EOF
{
  "method": "git",
  "repo_path": "$REPO_DIR",
  "version": "$COOP_VERSION"
}
EOF
success "Wrote install_meta.json (version=$COOP_VERSION, repo=$REPO_DIR)"

# ---------------------------------------------------------------------------
# Install the coop-keeper agent directory.
# Same code path as `coop upgrade`: the paths agents/coop-keeper/manifest.yaml
# lists are written, anything else already in the directory is left alone —
# so re-running the installer over an existing install keeps the operator's
# files there. On a first install the directory is absent and this is a copy.
# ---------------------------------------------------------------------------
install_keeper_dir() {
  # $1 = repo dir, $2 = runtime dir. Exit 0 = installed, 2 = nothing shipped, 1 = failed.
  local src="$1/agents/coop-keeper"
  [ -d "$src" ] || return 2
  (cd "$1" && "$1/.venv/bin/python" -c 'import pathlib, sys, gateway.upgrade as u; u.sync_keeper_dir(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))' "$1" "$2")
}
if install_keeper_dir "$REPO_DIR" "$RUNTIME_DIR"; then
  success "Installed coop-keeper to $RUNTIME_DIR/agents/builtin/coop-keeper/"
elif [ $? -eq 2 ]; then
  info "No coop-keeper directory in this checkout — skipped"
else
  error "coop-keeper could not be installed (see above). install_meta.json is written, so 'coop upgrade' can retry it once the cause is fixed."
fi

# ---------------------------------------------------------------------------
# Detect shell config file for source hint
# ---------------------------------------------------------------------------
case "$SHELL" in
  */zsh)  SHELL_RC="~/.zshrc" ;;
  */fish) SHELL_RC="~/.config/fish/config.fish" ;;
  *)      SHELL_RC="~/.bashrc" ;;
esac

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------
printf '\n'
success "Installation complete!"
printf '\n'
printf '  Repository cloned to:    %s\n' "$REPO_DIR"
printf '  Executable installed at: ~/.local/bin/coop\n'
if [ "$PROVISION_LINKED" = true ]; then
  printf '  Provisioning CLI:        ~/.local/bin/coop-provision\n'
elif [ -f "$PROVISION_BIN" ]; then
  # Built but not linked (destination occupied) — point at the real binary so
  # the command is still discoverable.
  printf '  Provisioning CLI:        %s\n' "$PROVISION_BIN"
fi
printf '\n'
printf '  To use AgentCoop in your current shell, run:\n'
printf '    source %s\n' "$SHELL_RC"
printf '  Or restart your terminal.\n'
if [ "$RUNTIME_DIR" != "$HOME/.agentcoop" ]; then
  printf '\n'
  printf '  This install lives in %s. Add this line to %s so every\n' "$RUNTIME_DIR" "$SHELL_RC"
  printf '  later shell (and anything that starts coop) finds it:\n'
  printf '    %s\n' "$(coop_home_hint "$RUNTIME_DIR")"
fi
printf '\n'
printf '  Set up your first bot with coop-keeper, using either CLI:\n'
printf '    cd %s/agents/builtin/coop-keeper && opencode\n' "$RUNTIME_DIR"
printf '    cd %s/agents/builtin/coop-keeper && claude\n' "$RUNTIME_DIR"
printf '\n'
printf '  Then:  coop status          tail -f %s/gateway.log\n' "$RUNTIME_DIR"
printf '\n'
