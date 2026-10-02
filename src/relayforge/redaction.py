from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit


SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "set-cookie", "x-api-key", "x-relay-signature"}
)


def sanitize_headers(
    headers: Mapping[str, str], extra_sensitive: set[str] | None = None
) -> dict[str, str]:
    sensitive = set(SENSITIVE_HEADERS)
    if extra_sensitive:
        sensitive.update(extra_sensitive)
    return {
        name: "[REDACTED]" if name in sensitive else value
        for name, value in headers.items()
    }


def redact_url(url: str) -> str:
    return url


def safe_excerpt(body: bytes, limit: int = 256) -> str:
    text = body[:limit].decode("utf-8", errors="replace")
    cleaned = "".join(
        ch if (ch in "\t\n" or ord(ch) >= 32) else "?" for ch in text
    )
    return cleaned
