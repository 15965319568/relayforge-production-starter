from __future__ import annotations

import unittest

from _support import ROOT
from relayforge.redaction import redact_url, safe_excerpt, sanitize_headers


class RedactionTests(unittest.TestCase):
    def test_header_names_are_matched_case_insensitively(self) -> None:
        actual = sanitize_headers(
            {"authorization": "Bearer secret", "X-API-KEY": "key", "Accept": "json"}
        )
        self.assertEqual(actual["authorization"], "[REDACTED]")
        self.assertEqual(actual["X-API-KEY"], "[REDACTED]")
        self.assertEqual(actual["Accept"], "json")

    def test_custom_sensitive_header_is_folded(self) -> None:
        actual = sanitize_headers({"X-Custom": "secret"}, {"x-custom"})
        self.assertEqual(actual["X-Custom"], "[REDACTED]")

    def test_url_drops_userinfo_query_and_fragment(self) -> None:
        self.assertEqual(
            redact_url("https://user:pass@example.test:8443/hook?a=secret#frag"),
            "https://example.test:8443/hook",
        )

    def test_url_formats_ipv6_host(self) -> None:
        self.assertEqual(
            redact_url("https://user:pass@[2001:db8::1]:9443/a?token=x"),
            "https://[2001:db8::1]:9443/a",
        )

    def test_excerpt_replaces_controls_and_truncates_unicode(self) -> None:
        value = safe_excerpt(("你" * 300 + "\x00").encode("utf-8"), limit=256)
        self.assertEqual(len(value), 256)
        self.assertEqual(value, "你" * 256)

    def test_excerpt_preserves_tab_and_newline(self) -> None:
        self.assertEqual(safe_excerpt(b"a\tb\nc\x01"), "a\tb\nc?")


if __name__ == "__main__":
    unittest.main()

