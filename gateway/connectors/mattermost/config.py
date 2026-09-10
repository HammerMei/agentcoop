"""Mattermost-specific configuration dataclass."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ...config import AttachmentConfig, ConnectorConfig
from ...core.agent_chain import AgentChainConfig
from ...paths import ATTACHMENTS_DIR_DEFAULT

logger = logging.getLogger(__name__)


@dataclass
class MattermostConfig:
    """All Mattermost platform configuration in one place.

    Separates Mattermost-specific concerns (server URL, team, credentials,
    user allow-lists) from the generic gateway config (agent type, timeout,
    etc.).

    Construct via from_connector_config() to derive from a ConnectorConfig,
    or build directly for testing.

    Unlike Rocket.Chat, Mattermost channels are scoped to a team — one
    MattermostConfig (and therefore one connector instance) serves exactly
    one team.  Multi-team deployments run one connector instance per team,
    the same tradeoff RC already has for multi-server deployments.

    Auth is dual-mode and mutually exclusive: either ``token`` (a Personal
    Access Token or Bot Account access token — used directly, no login call,
    no expiry) or ``username``/``password`` (session login via
    ``POST /api/v4/users/login``, with re-login-on-401 handling analogous to
    RocketChatConfig). Exactly one mode must be configured.
    """

    server_url: str
    team: str
    token: str = ""
    username: str = ""
    password: str = ""
    name: str = ""  # connector name — used to namespace the global attachment cache dir
    owners: list[str] = field(default_factory=list)
    guests: list[str] = field(default_factory=list)
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    reply_in_thread: bool = False
    # When True, top-level (non-threaded) messages trigger a proactive thread:
    # the agent reply is posted as root_id=triggering_post_id, starting a new thread.
    permission_reply_in_thread: bool = True
    # When True, 🔐 permission notifications are posted in a thread anchored to the
    # triggering message (keeps the main channel clean). Independent of reply_in_thread.
    require_mention: bool = True
    # When False, the bot responds to all messages in channels without needing
    # an explicit @mention. Agents bypass this check regardless.
    filter_sender: bool = True
    # When False, messages from senders not in the allow-list are still accepted
    # (open mode). Agents bypass this check and are always accepted if listed in
    # agent_chain.agent_usernames.
    agent_chain: AgentChainConfig = field(default_factory=AgentChainConfig)
    # Configuration for controlled agent-to-agent communication with loop protection.
    timezone: str = ""
    # IANA timezone for formatting message timestamps in the agent prompt prefix
    # (e.g. "America/Los_Angeles", "Asia/Taipei").  Empty = server local timezone.

    def __post_init__(self) -> None:
        has_token = bool(self.token)
        has_password_login = bool(self.username) and bool(self.password)
        if has_token and has_password_login:
            raise ValueError(
                "MattermostConfig: configure either 'token' or "
                "'username'+'password', not both."
            )
        if not has_token and not has_password_login:
            raise ValueError(
                "MattermostConfig: must configure either 'token' (Personal "
                "Access Token / bot access token) or 'username'+'password'."
            )

    @property
    def allow_senders(self) -> list[str]:
        """All users permitted to interact — owners + guests."""
        return self.owners + self.guests

    def role_of(self, username: str) -> str:
        """Return 'owner' or 'guest' for a given username.

        If the username is not in ``owners``, 'guest' is returned as a fallback —
        even if the username is not in ``guests`` either.  Unknown users are
        treated as guests rather than raising an error.  The caller is expected
        to have already filtered messages via ``filter_mm_message`` / ``allow_senders``
        before calling this method.
        """
        if username in self.owners:
            return "owner"
        if username not in self.guests:
            logger.debug("role_of: unknown user %r — defaulting to 'guest'", username)
        return "guest"

    @classmethod
    def from_connector_config(cls, cc: ConnectorConfig) -> "MattermostConfig":
        """Build a MattermostConfig from a ConnectorConfig.

        The ConnectorConfig.raw dict is expected to contain:
            server:        {url, team, token} or {url, team, username, password}
            allowed_users: {owners: [...], guests: [...]}
            attachments:   {max_file_size_mb, download_timeout, cache_dir}  (all optional)
        """
        raw = cc.raw
        server = raw.get("server", {})
        allowed_users = raw.get("allowed_users", {})
        attach_raw = raw.get("attachments", {})

        attach_cfg = AttachmentConfig(
            max_file_size_mb=attach_raw.get("max_file_size_mb", 10.0),
            download_timeout=attach_raw.get("download_timeout", 30),
            cache_dir=attach_raw.get("cache_dir", "agent-chat.cache"),
            cache_dir_global=attach_raw.get(
                "cache_dir_global", ATTACHMENTS_DIR_DEFAULT
            ),
        )

        agent_chain_raw = raw.get("agent_chain", {})
        agent_chain_cfg = AgentChainConfig(
            agent_usernames=agent_chain_raw.get("agent_usernames", []),
            max_turns=agent_chain_raw.get("max_turns", 5),
            ttl_seconds=agent_chain_raw.get("ttl_seconds", 3600.0),
        )

        return cls(
            server_url=server.get("url", "").rstrip("/"),
            team=server.get("team", ""),
            token=server.get("token", ""),
            username=server.get("username", ""),
            password=server.get("password", ""),
            name=cc.name,
            owners=allowed_users.get("owners", []),
            guests=allowed_users.get("guests", []),
            attachments=attach_cfg,
            reply_in_thread=raw.get("reply_in_thread", False),
            permission_reply_in_thread=raw.get("permission_reply_in_thread", True),
            require_mention=raw.get("require_mention", True),
            filter_sender=raw.get("filter_sender", True),
            agent_chain=agent_chain_cfg,
            timezone=raw.get("timezone", ""),
        )
