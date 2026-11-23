"""The connector-shared normalisation: bot names in both directions, mentions read
from the text with control lines left out."""

from coop.connectors.normalize import Names, mentions_in
from coop.room.model import ALL

NAMES = Names({"laomei": "laomei_bot", "wavebro": "wavebro_bot"})


def test_a_bots_username_maps_to_its_agent_name_and_back_others_pass_through():
    assert NAMES.agent_of("laomei_bot") == "laomei" and NAMES.agent_of("glin") == "glin"
    assert NAMES.username_of("laomei") == "laomei_bot" and NAMES.username_of("glin") == "glin"


def test_text_is_rewritten_both_ways_only_where_a_bot_is_named():
    assert NAMES.inbound("@laomei_bot and @glin: see @wavebro_bot") == "@laomei and @glin: see @wavebro"
    assert NAMES.outbound("@laomei and @glin: see @wavebro") == "@laomei_bot and @glin: see @wavebro_bot"
    assert NAMES.outbound("mail me at a@b.example") == "mail me at a@b.example"
    assert Names({}).inbound("@x") == "@x"


def test_mentions_come_from_the_text_among_known_names_room_wide_is_all():
    known = {"laomei", "wavebro", "glin"}
    assert mentions_in("@laomei look, @nobody too", known) == {"laomei"}
    assert mentions_in("@channel heads up", known) == {ALL}
    assert mentions_in("@here @glin", known) == {ALL, "glin"}
    assert mentions_in("mute: @laomei\n@wavebro take over", known) == {"wavebro"}  # a control line targets
    assert mentions_in("nothing here", known) == frozenset()


def test_a_person_with_an_agents_name_is_a_collision_the_bot_itself_is_not():
    assert NAMES.collides("laomei") and not NAMES.collides("laomei_bot") and not NAMES.collides("glin")
    assert not Names({"muse": "muse"}).collides("muse")  # a bot whose username is its agent name
