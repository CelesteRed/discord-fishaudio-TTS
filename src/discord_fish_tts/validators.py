from __future__ import annotations


class ValidationError(Exception):
    """Raised when user input fails a check. Message is user-safe."""


def check_text(text: str, *, max_len: int) -> str:
    text = text.strip()
    if not text:
        raise ValidationError("Text cannot be empty.")
    if len(text) > max_len:
        raise ValidationError(f"Text too long (max {max_len} characters).")
    return text
