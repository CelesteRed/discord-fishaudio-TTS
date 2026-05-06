import pytest
from discord_fish_tts.validators import (
    ValidationError, check_guild, check_text, parse_int_set,
)


def test_check_guild_passes_when_in_allowlist():
    check_guild(123, allowed={123, 456})  # no raise


def test_check_guild_raises_when_not_in_allowlist():
    with pytest.raises(ValidationError):
        check_guild(789, allowed={123, 456})


def test_check_guild_raises_when_none():
    with pytest.raises(ValidationError):
        check_guild(None, allowed={123})


def test_check_text_strips_and_returns():
    assert check_text("  hello  ", max_len=200) == "hello"


def test_check_text_rejects_empty():
    with pytest.raises(ValidationError):
        check_text("   ", max_len=200)


def test_check_text_rejects_too_long():
    with pytest.raises(ValidationError):
        check_text("x" * 201, max_len=200)


def test_parse_int_set_handles_csv():
    assert parse_int_set("123,456,789") == {123, 456, 789}


def test_parse_int_set_handles_whitespace():
    assert parse_int_set(" 1 , 2,  3  ") == {1, 2, 3}


def test_parse_int_set_handles_empty():
    assert parse_int_set("") == set()
    assert parse_int_set(None) == set()
