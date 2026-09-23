"""Profile-based config for the standalone RC/MM admin CLI.

Deliberately a plain YAML file, separate from AgentCoop's own ``config.yaml`` /
``ConnectorConfig`` (see gateway/admin/__init__.py for why). Profile-based
rather than a single server/credential pair because one AgentCoop deployment can
have agents talking to multiple RC and/or MM servers — the CLI operates on
one named profile per invocation.

File shape::

    profiles:
      mm-lab:
        type: mattermost
        server_url: https://mm.labpig.com
        team: labteam          # required for mattermost, ignored for rocketchat
        token: xxx             # preferred for mattermost (PAT) — see MattermostAdmin
        # username: admin      # fallback auth mode if no token
        # password: xxx
      rc-lab:
        type: rocketchat
        server_url: https://rc.labpig.com
        username: admin
        password: xxx

Duplicate keys (a repeated profile name, or a repeated field within a profile)
are rejected rather than silently resolved last-wins — see _StrictLoader.
"""

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from gateway.config_diff import REDACTED
from gateway.paths import RUNTIME_DIR


class _DuplicateKeyError(yaml.YAMLError):
    """Raised by _StrictLoader when a mapping repeats a key.

    Subclasses yaml.YAMLError deliberately, so load_profiles()'s existing
    ``except yaml.YAMLError`` arm converts it to AdminConfigError with no new
    handling — and so a future refactor cannot accidentally let it escape as a
    raw traceback.
    """


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of last-wins.

    PyYAML (like the YAML spec's "should" rather than "must") silently keeps the
    LAST value for a repeated key. That default is actively dangerous for this
    particular tool: it drives destructive operations against multiple
    deployments selected purely by config, so a copy/paste while adding a second
    profile can silently swap the target server or the credential. Verified
    behavior of the default loader:

        profiles:
          mm-lab:
            server_url: https://prod.example.com   # <- silently discarded
            token: prod-token                      # <- silently discarded
            server_url: https://lab.example.com
            token: lab-token

    ...loads as the lab values with no warning, and every downstream validation
    passes. Duplicated *profile names* collapse the same way. Failing loudly is
    the only safe reading, since neither value is more likely to be the intended
    one.
    """


_MERGE_TAG = "tag:yaml.org,2002:merge"


def _no_duplicate_keys(loader, node, deep=False):
    """Reject duplicate keys, then hand construction back to PyYAML unchanged.

    Only the CHECK is added here; the mapping itself is still built by
    SafeConstructor.construct_mapping. An earlier version of this reimplemented
    the construction loop, which silently dropped the merge-key handling
    SafeConstructor does via flatten_mapping() — so `<<: *anchor` stopped
    working entirely and raised "could not determine a constructor for the tag
    'tag:yaml.org,2002:merge'". That idiom is a natural fit for this very file
    (several profiles sharing a type and credentials), so delegating is not just
    tidier, it is the difference between working and broken.

    Duplicates are checked against the keys as LITERALLY WRITTEN in this
    mapping, deliberately BEFORE merge expansion:

      - `<<: *base` plus an explicit key that also appears in *base* is not a
        duplicate — overriding an inherited value is the entire point of a merge
        key, and YAML specifies the explicit key wins.
      - the merge key itself is skipped rather than constructed: its node carries
        the merge tag, which has no scalar constructor, so constructing it is
        what produced the error above. Multiple merges (`<<: [*a, *b]`, or `<<`
        appearing twice) are left to PyYAML, which supports them.
    """
    seen = set()
    for key_node, _value_node in node.value:
        if key_node.tag == _MERGE_TAG:
            continue
        key = loader.construct_object(key_node, deep=deep)
        # No guard for an unhashable key (a YAML complex key such as
        # `? [a, b]`): the membership test below raises TypeError, which
        # load_profiles()'s backstop arm already turns into a clean
        # AdminConfigError naming the real cause. An explicit try/except here
        # produced the identical message one line later, so it was five lines
        # that changed nothing.
        if key in seen:
            raise _DuplicateKeyError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1} "
                "— refusing to guess which value was intended"
            )
        seen.add(key)
    # yaml.SafeLoader.construct_mapping is SafeConstructor's implementation,
    # which is what calls flatten_mapping() to resolve the merge keys.
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


# Beside config.yaml, not in the current directory: coop-keeper runs
# coop-provision from its own directory, which an upgrade replaces wholesale
# (coop-keeper design §3.10). `--config` and COOP_ADMIN_CONFIG still override.
DEFAULT_CONFIG_PATH = RUNTIME_DIR / "admin-profiles.yaml"
CONFIG_PATH_ENV_VAR = "COOP_ADMIN_CONFIG"

SUPPORTED_TYPES = ("rocketchat", "mattermost")

# The fields a profile authenticates with. `init` writes them empty for the
# operator to fill; `profiles --json` shows each as "" (unfilled) or "***"
# (filled), never the value.
CREDENTIAL_FIELDS = ("username", "password", "token")
MASKED = REDACTED  # the one sentinel every masked view in this project prints


class AdminConfigError(Exception):
    """Raised for missing/malformed config files or unknown/invalid profiles."""


@dataclass
class AdminProfile:
    """One named RC or MM server + admin credentials.

    Auth precedence (mirrors MattermostREST's own dual-mode support):
    ``token`` wins if set; otherwise ``username``/``password`` are used.
    Rocket.Chat has no equivalent token-only constructor path today (see
    RocketChatAdmin), so ``token`` is effectively mattermost-only for now.
    """

    name: str
    type: str
    server_url: str
    team: str | None = None
    username: str | None = None
    password: str | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        # Type gate, before any of the semantic checks below. Every field is
        # declared `str` / `str | None` and everything downstream relies on
        # that — but nothing enforced it, and YAML hands over plenty of
        # non-strings for what looks like a string: `123`/`1.5` (numeric
        # scalar), `true`/`yes` (the classic "Norway problem", same footgun
        # load_profiles() already documents for profile *names*),
        # `2026-08-11` (unquoted date/timestamp), a nested list/mapping from
        # a bad indent, even bytes via `!!binary`.
        #
        # The truthiness checks below cannot catch these: a non-empty int,
        # float, list, dict, set, date or bytes is truthy, so it sails
        # straight through `if not self.server_url`. That mattered most for
        # server_url, whose only consumers are
        # RocketChatREST/MattermostREST.__init__ doing
        # `server_url.rstrip("/")` — reached from admin_factory(), which
        # cli._run() guards with `except AdminConfigError` ONLY, so the
        # resulting AttributeError (TypeError for bytes, which *has*
        # .rstrip) escaped as a raw traceback, violating the CLI's
        # "ordinary config mistakes print Error: <message>" contract.
        # The remaining fields didn't traceback (they're consumed after that
        # block, where a broad `except Exception` catches everything) but
        # reached the server as nonsense and failed with a message that
        # named neither the field nor the real cause — e.g. a dict `team`
        # produced "Team '{'a': 'b'}' not found among the caller's own
        # teams", and a date `username` produced "Object of type date is not
        # JSON serializable". Rejecting all of them here, by shape, at the
        # one chokepoint both the CLI and direct library construction pass
        # through, is what makes those a single fix rather than seven.
        #
        # `None` is deliberately allowed through: it's the legitimate
        # default for team/username/password/token, and for the required
        # fields the checks below already reject it with a more specific
        # message ("server_url is required", "unknown type None"). The
        # offending value is described by type only, never echoed —
        # `password`/`token` would otherwise be printed verbatim to stderr
        # (an unquoted all-digit password parses as an int and would land in
        # this very message), and RocketChatREST.__repr__ already sets the
        # opposite convention with `password=***`.
        for field_name in ("name", "type", "server_url", "team", "username", "password", "token"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise AdminConfigError(
                    f"Profile '{self.name}': '{field_name}' must be a string, got "
                    f"{type(value).__name__} (quote it in YAML if it looks like a "
                    "number, boolean, or date)"
                )
        if self.type not in SUPPORTED_TYPES:
            raise AdminConfigError(
                f"Profile '{self.name}': unknown type {self.type!r}, "
                f"must be one of {SUPPORTED_TYPES}"
            )
        if not self.server_url:
            raise AdminConfigError(f"Profile '{self.name}': server_url is required")
        if not self.token and not (self.username and self.password):
            raise AdminConfigError(
                f"Profile '{self.name}': must set either 'token', or both "
                "'username' and 'password'"
            )
        if self.type == "mattermost" and not self.team:
            raise AdminConfigError(
                f"Profile '{self.name}': 'team' is required for type=mattermost "
                "(Mattermost channels are scoped to a team; see MattermostAdmin.connect)"
            )


def _resolve_config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def load_raw_profiles(path: str | Path | None = None) -> tuple[Path, dict[str, dict]]:
    """The file's profiles as written — name → field mapping — checked for
    shape only (a mapping of mappings with string names), not for content.

    This is the read `load_profile`, `load_profiles`, `init_profile` and
    `masked_profiles` share. Content validation is per profile, in
    `AdminProfile`, and deliberately NOT done here: a skeleton `init` wrote
    and the operator has not filled yet has empty credentials, and reading
    the whole file must not fail on it while another profile is being used.

    Resolution order for the file path: explicit ``path`` argument, then
    the ``COOP_ADMIN_CONFIG`` env var, then ``~/.agentcoop/admin-profiles.yaml``.

    The file is opened in *binary* mode and handed to PyYAML undecoded, so
    PyYAML applies its own YAML-spec encoding detection (UTF-8/16/32, BOM
    sniffing). Two reasons, both about the CLI's "no raw tracebacks"
    contract: a file in any non-UTF-8 encoding (a latin-1 accented
    password, a UTF-16 file from a Windows editor) would otherwise raise
    ``UnicodeDecodeError`` out of the text-mode ``read()`` — a ``ValueError``,
    so neither the ``OSError`` nor the ``yaml.YAMLError`` arm below caught
    it — and in binary mode the same file instead yields a
    ``yaml.reader.ReaderError`` (a YAMLError, with the byte offset). As a
    bonus, genuinely UTF-16/32-encoded config files now load rather than
    merely failing cleanly.
    """
    config_path = _resolve_config_path(path)
    # No os.path.exists() pre-check: it bought a nicer "not found" message
    # at the cost of a whole second class of escaping errors. Path.exists()
    # only swallows ENOENT/ENOTDIR/EBADF/ELOOP (pathlib._IGNORED_ERRNOS) and
    # re-raises every other OSError — so an unsearchable parent directory
    # (EACCES) or an over-long pasted path (ENAMETOOLONG) raised *before*
    # the try block below existed to convert it. open() reports all of those
    # as OSError subclasses anyway, with FileNotFoundError preserving the
    # original message, so the check was pure liability.
    try:
        with open(config_path, "rb") as f:
            # _StrictLoader, not safe_load: same safe tag set, but duplicate
            # mapping keys raise instead of silently last-winning. See its
            # docstring for why that default is unacceptable here.
            raw = yaml.load(f, Loader=_StrictLoader) or {}  # noqa: S506 - safe subclass
    except FileNotFoundError as e:
        raise AdminConfigError(
            f"Admin config file not found: {config_path} "
            f"(pass --config, set {CONFIG_PATH_ENV_VAR}, or create it with "
            f"'coop-provision init <profile> ...')"
        ) from e
    except OSError as e:
        # e.g. --config pointing at a directory (IsADirectoryError), an
        # unreadable file (PermissionError), an unsearchable parent dir, a
        # symlink loop, or a too-long path — ordinary configuration mistakes
        # that open() surfaces before YAML parsing even starts.
        raise AdminConfigError(f"{config_path}: could not read config file: {e}") from e
    except yaml.YAMLError as e:
        raise AdminConfigError(f"{config_path}: invalid YAML: {e}") from e
    except Exception as e:
        # Backstop, because yaml.safe_load's exception surface is not
        # enumerable: PyYAML's SafeConstructor leaks several raw exceptions
        # that are NOT YAMLError subclasses for explicitly-tagged scalars —
        # ValueError ("!!int abc", "!!float abc"), AttributeError
        # ("!!timestamp nonsense"), KeyError ("!!bool maybe") — and its
        # recursive composer raises RecursionError past ~500 levels of
        # nesting. Enumerating today's list would silently rot with the next
        # PyYAML release, and every one of them is a malformed *config file*,
        # which the CLI contract says must print "Error: ..." rather than a
        # traceback. Deliberately narrow in scope: the try body is only
        # open() + safe_load(), so this cannot mask a logic bug in the
        # validation code below. KeyboardInterrupt/SystemExit are
        # BaseException and still propagate.
        raise AdminConfigError(
            f"{config_path}: could not parse config file: {type(e).__name__}: {e}"
        ) from e

    if not isinstance(raw, dict):
        raise AdminConfigError(
            f"{config_path}: expected a mapping at the top level, got {type(raw).__name__}"
        )

    raw_profiles = raw.get("profiles")
    if raw_profiles is None:
        raise AdminConfigError(f"{config_path}: no 'profiles' section found")
    if not isinstance(raw_profiles, dict):
        raise AdminConfigError(
            f"{config_path}: 'profiles' must be a mapping, got {type(raw_profiles).__name__}"
        )

    for name, fields in raw_profiles.items():
        if not isinstance(name, str):
            # An unquoted YAML scalar that looks numeric/boolean (123, true,
            # yes/no — the classic "Norway problem") parses as that type,
            # not a string. argparse always hands the CLI's profile argument
            # back as a str, so the lookup in get_profile() would silently
            # miss even for what looks like the same name, and — worse —
            # ", ".join(sorted(profiles)) in that function's error message
            # raises TypeError on a non-str key, escaping AdminConfigError
            # handling as a traceback. Reject it here instead.
            raise AdminConfigError(
                f"{config_path}: profile name {name!r} must be a string "
                f"(quote it in YAML if it looks like a number or boolean)"
            )
        if not isinstance(fields, dict):
            raise AdminConfigError(f"{config_path}: profile '{name}' must be a mapping")
    return config_path, raw_profiles


def _build_profile(config_path: Path, name: str, fields: dict) -> AdminProfile:
    try:
        return AdminProfile(name=name, **fields)
    except TypeError as e:
        # A misspelled/unsupported key, or a redundant 'name' key inside
        # the profile body (colliding with the name=name passed above),
        # makes this raise TypeError — not caught by _run()'s
        # `except AdminConfigError`, so left alone this was a raw
        # traceback for a common config typo instead of a clean error.
        raise AdminConfigError(f"{config_path}: profile '{name}' has invalid fields: {e}") from e


def load_profiles(path: str | Path | None = None) -> dict[str, AdminProfile]:
    """Every profile in the file, each validated — for callers that want them
    all. The CLI uses `load_profile` instead, so one unfilled skeleton does
    not stop another profile from being used."""
    config_path, raw_profiles = load_raw_profiles(path)
    return {name: _build_profile(config_path, name, fields) for name, fields in raw_profiles.items()}


def load_profile(path: str | Path | None, name: str) -> AdminProfile:
    """The one named profile, validated; the others are only checked for
    shape. Unknown names list what IS there (AdminConfigError)."""
    config_path, raw_profiles = load_raw_profiles(path)
    if name not in raw_profiles:
        available = ", ".join(sorted(raw_profiles)) or "(none defined)"
        raise AdminConfigError(f"Unknown profile '{name}'. Available profiles: {available}")
    return _build_profile(config_path, name, raw_profiles[name])


def masked_profiles(path: str | Path | None = None, *, missing_ok: bool = False) -> list[dict]:
    """Every profile's name, type, server URL and team, with each credential
    field shown as "" when unfilled and `MASKED` when filled — so a caller
    can tell a skeleton from a usable profile without seeing a value.
    Reads the file as written: an unfilled skeleton is listed, not refused.
    With `missing_ok`, no file yet is no profiles — the state bootstrap
    starts from (coop-keeper design §3.2) — rather than an error."""
    if missing_ok and not _resolve_config_path(path).exists():
        return []
    _, raw_profiles = load_raw_profiles(path)
    out = []
    for name, fields in raw_profiles.items():
        entry = {
            "name": name,
            "type": fields.get("type"),
            "server_url": fields.get("server_url"),
            "team": fields.get("team"),
        }
        for key in CREDENTIAL_FIELDS:
            entry[key] = MASKED if fields.get(key) else ""
        out.append(entry)
    return out


def init_profile(
    path: str | Path | None, name: str, *, profile_type: str, server_url: str, team: str | None,
) -> Path:
    """Write profile `name` with its credential fields empty, for the operator
    to fill in an editor (coop-keeper design §3.2). Creates the file when it
    is absent; refuses to touch a profile that already exists. The file is
    re-serialized, so comments in it are not preserved, and it is chmod'd
    0600 because it is about to hold administrative credentials.

    Rocket.Chat profiles get username/password; Mattermost ones also get
    token, which MattermostAdmin prefers when set.
    """
    if profile_type not in SUPPORTED_TYPES:
        raise AdminConfigError(f"unknown type {profile_type!r}, must be one of {SUPPORTED_TYPES}")
    if not server_url:
        raise AdminConfigError("server_url is required")
    if profile_type == "mattermost" and not team:
        raise AdminConfigError("'--team' is required for type=mattermost")
    config_path = _resolve_config_path(path)
    if config_path.exists():
        config_path, raw_profiles = load_raw_profiles(config_path)
    else:
        raw_profiles = {}
    if name in raw_profiles:
        raise AdminConfigError(
            f"{config_path}: profile '{name}' already exists — it is not rewritten; "
            "edit the file, or choose another name"
        )
    skeleton: dict = {"type": profile_type, "server_url": server_url}
    if team:
        skeleton["team"] = team
    skeleton["username"] = ""
    skeleton["password"] = ""
    if profile_type == "mattermost":
        skeleton["token"] = ""
    raw_profiles = dict(raw_profiles)
    raw_profiles[name] = skeleton
    # Serialize beside the file and replace it only once the write succeeded:
    # `open(path, "w")` would truncate the store of every administrative
    # credential before the dump ran, and a disk-full or interruption there
    # would leave nothing behind.
    tmp = config_path.with_name(config_path.name + ".tmp")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            yaml.safe_dump({"profiles": raw_profiles}, f, sort_keys=False, allow_unicode=True)
        tmp.chmod(0o600)
        os.replace(tmp, config_path)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise AdminConfigError(f"{config_path}: could not write: {e}") from e
    return config_path


def get_profile(profiles: dict[str, AdminProfile], name: str) -> AdminProfile:
    """Look up a profile by name, raising AdminConfigError with the
    available names if it's not found (rather than a bare KeyError)."""
    try:
        return profiles[name]
    except KeyError:
        available = ", ".join(sorted(profiles)) or "(none defined)"
        raise AdminConfigError(
            f"Unknown profile '{name}'. Available profiles: {available}"
        ) from None
