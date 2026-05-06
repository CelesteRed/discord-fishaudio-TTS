import pytest
from discord_fish_tts.validators import ValidationError, check_text


def test_check_text_strips_and_returns():
    assert check_text("  hello  ", max_len=200) == "hello"


def test_check_text_rejects_empty():
    with pytest.raises(ValidationError):
        check_text("   ", max_len=200)


def test_check_text_rejects_too_long():
    with pytest.raises(ValidationError):
        check_text("x" * 201, max_len=200)
