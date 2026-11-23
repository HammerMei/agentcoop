"""The connector-shared normalisation: mentions read from the text, among the room's
usernames, with control lines left out."""

from coop.connectors.normalize import mentions_in
from coop.room.model import ALL


def test_mentions_come_from_the_text_among_known_names_room_wide_is_all():
    known = {"laomei_bot", "wavebro_bot", "glin"}
    assert mentions_in("@laomei_bot look, @nobody too", known) == {"laomei_bot"}
    assert mentions_in("@channel heads up", known) == {ALL}
    assert mentions_in("@here @glin", known) == {ALL, "glin"}
    assert mentions_in("mute: @laomei_bot\n@wavebro_bot take over", known) == {"wavebro_bot"}  # a control line targets
    assert mentions_in("nothing here", known) == frozenset()
    assert mentions_in("mail me at a@b.example", known) == frozenset()
