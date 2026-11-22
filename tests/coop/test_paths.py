"""``COOP_HOME`` spelling: the rule is ``gateway/paths.py``'s, checked at the source."""

from pathlib import Path

import pytest

from coop.paths import runtime_dir_from_env
from tests.helpers import COOP_HOME_SPELLINGS


@pytest.mark.parametrize("value, ok", COOP_HOME_SPELLINGS)
def test_coop_home_spellings(value, ok):
    if ok:
        assert runtime_dir_from_env({"COOP_HOME": value}).is_absolute()
    else:
        with pytest.raises(ValueError, match="COOP_HOME must be"):
            runtime_dir_from_env({"COOP_HOME": value})


def test_unset_coop_home_is_the_home_default():
    assert runtime_dir_from_env({}) == Path.home() / ".agentcoop"
