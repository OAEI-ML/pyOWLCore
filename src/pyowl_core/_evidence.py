"""Shared bounded/redacted text rules for public diagnostic evidence."""

from __future__ import annotations


def redact_credentials(value: str) -> str:
    """Redact URI userinfo without interpreting or normalizing the URI."""

    selected: list[str] = []
    cursor = 0
    while (scheme_end := value.find("://", cursor)) >= 0:
        authority_start = scheme_end + 3
        authority_end = len(value)
        for delimiter in "/?#\t\r\n \"'<>[](){}":
            found = value.find(delimiter, authority_start)
            if found >= 0:
                authority_end = min(authority_end, found)
        at = value.rfind("@", authority_start, authority_end)
        if at < 0:
            selected.append(value[cursor:authority_start])
            cursor = authority_start
            continue
        selected.append(value[cursor:authority_start])
        selected.append("<redacted>@")
        selected.append(value[at + 1 : authority_end])
        cursor = authority_end
    selected.append(value[cursor:])
    return "".join(selected)


def bounded_evidence_text(value: str, *, max_bytes: int = 4_096) -> str:
    """Return printable, credential-free UTF-8 text within an exact byte cap."""

    redacted = redact_credentials(value)
    sanitized = "".join(character if character.isprintable() else "?" for character in redacted)
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return sanitized
    selected = encoded[: max_bytes - 3]
    while True:
        try:
            return selected.decode("utf-8") + "..."
        except UnicodeDecodeError as error:
            selected = selected[: error.start]


__all__ = []
