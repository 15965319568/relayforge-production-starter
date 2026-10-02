from __future__ import annotations

import json
import unittest
from datetime import timedelta

from _enterprise_support import EnterpriseCase
from _support import BASE_TIME
from relayforge.audit_export import AuditExporter
from relayforge.cursors import CursorCodec
from relayforge.errors import ValidationError


class CursorCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.codec = CursorCodec("cursor-secret")
        self.query = {"tenant": "org-a", "kinds": ["request"]}

    def token(self):
        return self.codec.encode(
            scope="audit",
            position={"id": 42},
            query=self.query,
            expires_at=BASE_TIME + timedelta(minutes=5),
        )

    def test_round_trip_returns_position(self) -> None:
        self.assertEqual(
            self.codec.decode(
                self.token(), scope="audit", query=self.query, now=BASE_TIME
            ),
            {"id": 42},
        )

    def test_tampering_is_rejected(self) -> None:
        token = self.token()
        replacement = ("A" if token[0] != "A" else "B") + token[1:]
        with self.assertRaises(ValidationError):
            self.codec.decode(
                replacement, scope="audit", query=self.query, now=BASE_TIME
            )

    def test_scope_and_query_are_bound(self) -> None:
        with self.assertRaises(ValidationError):
            self.codec.decode(
                self.token(), scope="dead-letter", query=self.query, now=BASE_TIME
            )
        with self.assertRaises(ValidationError):
            self.codec.decode(
                self.token(),
                scope="audit",
                query={"tenant": "org-b"},
                now=BASE_TIME,
            )

    def test_expiry_boundary_is_exclusive(self) -> None:
        with self.assertRaises(ValidationError):
            self.codec.decode(
                self.token(),
                scope="audit",
                query=self.query,
                now=BASE_TIME + timedelta(minutes=5),
            )


class AuditExporterTests(EnterpriseCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.delivery = self.make_succeeded()
        for index in range(4):
            self.store.append_audit(
                occurred_at=BASE_TIME + timedelta(seconds=index + 2),
                kind="operator.note" if index % 2 else "request",
                delivery_id=self.delivery,
                details={"index": index, "headers": {"Authorization": "secret"}},
            )
        self.exporter = AuditExporter(
            self.database,
            cursor_codec=CursorCodec("cursor-secret"),
            cursor_ttl_seconds=60,
        )

    def test_pages_are_stable_without_duplicates(self) -> None:
        first = self.exporter.page(now=BASE_TIME, tenant_id="org-a", limit=2)
        second = self.exporter.page(
            now=BASE_TIME,
            tenant_id="org-a",
            limit=2,
            cursor=first.next_cursor,
        )
        first_ids = [item["id"] for item in first.items]
        second_ids = [item["id"] for item in second.items]
        self.assertEqual(len(first_ids), 2)
        self.assertTrue(set(first_ids).isdisjoint(second_ids))
        self.assertEqual(first_ids + second_ids, sorted(first_ids + second_ids))

    def test_cursor_cannot_be_reused_for_different_filter(self) -> None:
        first = self.exporter.page(now=BASE_TIME, tenant_id="org-a", limit=1)
        with self.assertRaises(ValidationError):
            self.exporter.page(
                now=BASE_TIME,
                tenant_id="org-a",
                kinds=("request",),
                limit=1,
                cursor=first.next_cursor,
            )

    def test_kind_and_time_filters_are_applied(self) -> None:
        page = self.exporter.page(
            now=BASE_TIME,
            tenant_id="org-a",
            kinds=("operator.note",),
            after=BASE_TIME + timedelta(seconds=3),
            before=BASE_TIME + timedelta(seconds=6),
            limit=10,
        )
        self.assertTrue(page.items)
        self.assertEqual({item["kind"] for item in page.items}, {"operator.note"})

    def test_export_redacts_nested_sensitive_headers(self) -> None:
        page = self.exporter.page(
            now=BASE_TIME, tenant_id="org-a", kinds=("request",), limit=10
        )
        rendered = json.dumps(page.items)
        self.assertNotIn("secret", rendered)
        for item in page.items:
            self.assertEqual(
                item["details"]["headers"]["Authorization"], "[REDACTED]"
            )

    def test_ndjson_has_one_object_per_line_and_trailing_newline(self) -> None:
        page = self.exporter.page(now=BASE_TIME, tenant_id="org-a", limit=3)
        encoded = self.exporter.ndjson(page)
        self.assertTrue(encoded.endswith(b"\n"))
        rows = [json.loads(line) for line in encoded.decode().splitlines()]
        self.assertEqual(len(rows), len(page.items))

    def test_limit_is_bounded(self) -> None:
        for limit in (0, 501):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                self.exporter.page(now=BASE_TIME, limit=limit)


if __name__ == "__main__":
    unittest.main()

