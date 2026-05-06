from __future__ import annotations


class ValidationError(Exception):
    """Raised when user input fails a check. Message is user-safe."""


def check_guild(guild_id: int | None, *, allowed: set[int]) -> None:
    if guild_id is None or guild_id not in allowed:
        raise ValidationError("This server is not authorized.")


def check_text(text: str, *, max_len: int) -> str:
    text = text.strip()
    if not text:
        raise ValidationError("Text cannot be empty.")
    if len(text) > max_len:
        raise ValidationError(f"Text too long (max {max_len} characters).")
    return text


def parse_int_set(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out
