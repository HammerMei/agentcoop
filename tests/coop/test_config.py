"""The configuration file (design §4): what it accepts, what it refuses and why, and
the roster a room rule gives each server."""

from pathlib import Path

import pytest
import yaml

from coop.config import ConfigError, load, parse
from coop.room.attachments import Limits

BASE = Path("/srv/coop")

FULL = """
servers:
  lab-mm:
    type: mattermost
    url: https://mm.example
    team: lab
    owners: [glin]
    guests: [pat]
    bots:
      laomei:  {username: laomei_bot, token_file: servers/lab-mm/laomei.token}
      wavebro: {username: wavebro_bot, token_file: /keys/wavebro.token, owners: [wave]}
      muse:    {username: muse_bot}
  lab-rc:
    type: rocketchat
    url: https://rc.example
    bots:
      laomei: {username: laomei, token_file: servers/lab-rc/laomei.token}
agents:
  laomei:  {type: remote, token_file: agents/laomei/token}
  wavebro: {type: remote, token_file: agents/wavebro/token, heartbeat: gateway, bio_source: pushed}
gateway:
  listen: 0.0.0.0:7474
  tls: {cert: gateway/cert.pem, key: gateway/key.pem}
rooms:
  "lab-mm:team":
    agents: [laomei, wavebro, muse]
    enforce: true
    rate_limit_posts: 3
  "lab-*:*":
    agents: [laomei]
"""


def parsed(text: str = FULL):
    return parse(yaml.safe_load(text), BASE)


def test_the_attachment_limits_default_to_the_platforms_and_parse_under_gateway():
    assert parsed().gateway.attachments == Limits(max_size_mb=0, max_per_post=0, transfer_timeout_seconds=120)
    config = parsed("gateway: {attachments: {max_size_mb: 10.5, max_per_post: 3, transfer_timeout_seconds: 30}}")
    assert config.gateway.attachments == Limits(max_size_mb=10.5, max_per_post=3, transfer_timeout_seconds=30)
    assert config.gateway.attachments.max_bytes == int(10.5 * 1024 * 1024)


def test_the_full_document_parses_with_relative_paths_under_the_base():
    config = parsed()
    mm = config.servers["lab-mm"]
    assert mm.type == "mattermost" and mm.team == "lab" and mm.owners == {"glin"} and mm.guests == {"pat"}
    assert mm.bots["laomei"].token_file == BASE / "servers/lab-mm/laomei.token"
    assert mm.bots["wavebro"].token_file == Path("/keys/wavebro.token")
    assert mm.bots["wavebro"].owners == {"wave"} and mm.bots["laomei"].owners == {"glin"}  # server default
    assert not mm.bots["muse"].hosted and mm.bots["laomei"].hosted
    assert mm.bot_by_username("muse_bot").agent == "muse" and mm.bot_by_username("nobody") is None
    assert config.agents["laomei"].token_file == BASE / "agents/laomei/token"
    assert config.gateway.address == "0.0.0.0:7474"
    assert config.gateway.tls == (BASE / "gateway/cert.pem", BASE / "gateway/key.pem")
    assert config.rooms[0].params.enforce and config.rooms[0].params.rate_limit_posts == 3
    assert config.rooms[1].params.enforce is False  # the defaults otherwise


def test_the_first_matching_rule_wins_and_the_roster_is_per_server():
    config = parsed()
    assert config.rule_for("lab-mm:team").agents == ("laomei", "wavebro", "muse")
    assert config.rule_for("lab-mm:other").agents == ("laomei",)
    assert config.rule_for("lab-rc:general").agents == ("laomei",)
    assert config.rule_for("prod:team") is None
    roster = config.roster_for("lab-mm", config.rule_for("lab-mm:team"))
    assert roster.bots == ("laomei_bot", "wavebro_bot", "muse_bot")  # the platform's names, not the agents'
    assert roster.owners == {"laomei_bot": {"glin"}, "wavebro_bot": {"wave"}, "muse_bot": {"glin"}}
    assert roster.guests == {"pat"}
    assert roster.is_moderator("wave") and roster.label("laomei_bot", "wave") == "guest"
    assert roster.kind_of("muse_bot") == "bot" and roster.kind_of("laomei") == "human"  # a person named laomei
    rc_rule = config.rule_for("lab-rc:general")
    assert config.roster_for("lab-rc", rc_rule).bots == ("laomei",)
    assert config.missing_bots("lab-rc", config.rooms[0]) == ("wavebro", "muse")


def test_load_reads_yaml_and_an_empty_file_is_an_empty_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(FULL)
    assert load(path, BASE) == parsed()
    path.write_text("")
    empty = load(path, BASE)
    assert empty.servers == {} and empty.rooms == () and empty.gateway.address == "127.0.0.1:7474"
    path.write_text("- a list")
    with pytest.raises(ConfigError, match="not a mapping"):
        load(path, BASE)


@pytest.mark.parametrize("text, message", [
    ("watchers: {}", "unknown keys \\['watchers'\\]"),
    ("servers: {x: {type: slack, url: u}}", "'slack' is not one of"),
    ("servers: {x: {type: mattermost, url: u}}", "names its team"),
    ("servers: {x: {type: mattermost, url: u, team: t, bots: {a: {username: n, extra: 1}}}}", "unknown keys \\['extra'\\]"),
    ("servers: {x: {type: rocketchat, url: u, bots: {a: {username: n}, b: {username: n}}}}", "share username 'n'"),
    ("agents: {a: {type: claude, token_file: t}}", "every agent is `remote` now"),
    ("agents: {a: {type: remote, token_file: t, permissions: {}}}", "not gateway settings any more"),
    ("agents: {a: {type: remote, token_file: t, heartbeat: agent}}", "only `gateway` is defined"),
    ("agents: {a: {type: remote}}", "token_file: a non-empty string"),
    ("servers: {x: {type: rocketchat, url: u, bots: {a: {username: n, token_file: t, password_file: p}}}}",
     "token_file or password_file, not both"),
    ("servers: {x: {type: rocketchat, url: u, bots: {a: {username: n, password_file: 1}}}}", "password_file: not a string"),
    ("servers: {x: {type: rocketchat, url: u, verify_tls: 1}}", "verify_tls: 1 is not a boolean"),
    ("servers: {x: {type: rocketchat, url: u, bots: {a: {username: n, token_file: t}}}}", "no agents.a entry"),
    ("servers: {x: {type: rocketchat, url: u, bots: {a: {username: n}}}}\nagents: {a: {type: remote, token_file: t}}",
     "cannot post as a bot it cannot log in as"),
    ("rooms: {'x:*': {agents: [ghost]}}", "neither agents nor bots"),
    ("rooms: {'x:*': {agents: []}}", "non-empty list"),
    ("rooms: {'x:*': {agents: [a, a]}}", "names repeat"),
    ("rooms: {'x:*': {agents: [a], typing: true}}", "unknown settings \\['typing'\\]"),
    ("rooms: {'x:*': {agents: [a], classifier_fallback: never}}", "classifier_fallback"),
    ("rooms: {'x:[': {agents: [a]}}", "rooms.'x:\\['"),
    ("gateway: {listen: 7474}", "host:port"),
    ("gateway: {tls: {cert: c}}", "gateway.tls.key"),
    ("gateway: {attachments: {max_size_mb: -1}}", "gateway.attachments.max_size_mb: a number of at least 0"),
    ("gateway: {attachments: {max_per_post: 1.5}}", "gateway.attachments.max_per_post: a whole number"),
    ("gateway: {attachments: {max_per_post: true}}", "gateway.attachments.max_per_post: a number"),
    ("gateway: {attachments: {transfer_timeout_seconds: 0}}", "transfer_timeout_seconds: a number of at least 1"),
    ("gateway: {attachments: {cache_dir: x}}", "gateway.attachments: unknown keys \\['cache_dir'\\]"),
    ("gateway: {attachments: {max_size_mb: .nan}}", "gateway.attachments.max_size_mb: a number of at least 0"),
    ("gateway: {attachments: {max_per_post: .inf}}", "gateway.attachments.max_per_post: a number of at least 0"),
])
def test_what_the_loader_refuses_and_where_it_points(text, message):
    doc = yaml.safe_load(text)
    if "rooms" in doc and "agents" not in doc and "ghost" not in text:
        doc["agents"] = {"a": {"type": "remote", "token_file": "t"}}
    with pytest.raises(ConfigError, match=message):
        parse(doc, BASE)


@pytest.mark.parametrize("text, message", [
    ("rooms: {'x:*': {agents: [a], enforce: 'false'}}", "enforce: 'false' is not bool"),
    ("rooms: {'x:*': {agents: [a], rate_limit_posts: '6'}}", "rate_limit_posts: '6' is not int"),
    ("rooms: {'x:*': {agents: [a], rate_limit_posts: 2.5}}", "rate_limit_posts: 2.5 is not int"),
    ("rooms: {'x:*': {agents: [a], cooldown_seconds: true}}", "cooldown_seconds: True is not float"),
    ("rooms: {'x:*': {agents: [a], classifier_fallback: 3}}", "classifier_fallback: 3 is not str"),
])
def test_room_parameters_are_type_checked(text, message):
    doc = yaml.safe_load(text)
    doc["agents"] = {"a": {"type": "remote", "token_file": "t"}}
    with pytest.raises(ConfigError, match=message):
        parse(doc, BASE)


def test_an_int_serves_a_float_parameter_and_a_bad_yaml_file_is_a_config_error(tmp_path):
    doc = yaml.safe_load("rooms: {'x:*': {agents: [a], cooldown_seconds: 5}}")
    doc["agents"] = {"a": {"type": "remote", "token_file": "t"}}
    assert parse(doc, BASE).rooms[0].params.cooldown_seconds == 5
    path = tmp_path / "config.yaml"
    path.write_text("servers: [unclosed")
    with pytest.raises(ConfigError, match="not YAML"):
        load(path)


@pytest.mark.parametrize("text, message", [
    ("gateway: {listen: 'localhost:not-a-port'}", "not a port"),
    ("gateway: {listen: 'localhost:70000'}", "not a port"),
    ("rooms: []", "rooms: not a mapping"),
    ("agents: false", "agents: not a mapping"),
    ("servers: {x: {type: rocketchat, url: u, bots: []}}", "servers.x.bots: not a mapping"),
    ("gateway: []", "gateway: not a mapping"),
])
def test_bad_ports_and_falsy_sections_are_refused(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(yaml.safe_load(text), BASE)


def test_port_zero_and_null_sections_are_fine():
    config = parse(yaml.safe_load("gateway: {listen: '127.0.0.1:0'}\nrooms:\nagents: null"), BASE)
    assert config.gateway.address == "127.0.0.1:0" and config.rooms == () and config.agents == {}


def test_a_password_file_hosts_the_bot_and_verify_tls_is_the_servers():
    doc = yaml.safe_load("""
servers:
  lab: {type: mattermost, url: https://mm, team: t, verify_tls: false,
        bots: {a: {username: a_bot, password_file: a.pw}, c: {username: c_bot}}}
agents:
  a: {type: remote, token_file: a.agent}
""")
    config = parse(doc, BASE)
    lab = config.servers["lab"]
    assert lab.verify_tls is False
    assert lab.bots["a"].hosted and lab.bots["a"].password_file == BASE / "a.pw" and lab.bots["a"].token_file is None
    assert not lab.bots["c"].hosted and lab.bots["c"].password_file is None
    doc["servers"]["lab"].pop("verify_tls")
    assert parse(doc, BASE).servers["lab"].verify_tls is True
