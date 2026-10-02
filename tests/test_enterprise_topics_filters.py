from __future__ import annotations

import unittest

from _support import ROOT
from relayforge.errors import ValidationError
from relayforge.filters import MISSING, compile_filter
from relayforge.topics import topic_matches, validate_pattern, validate_topic


class TopicTests(unittest.TestCase):
    def test_literal_pattern_matches_exact_topic(self) -> None:
        self.assertTrue(topic_matches("payment.captured", "payment.captured"))
        self.assertFalse(topic_matches("payment.captured", "payment.failed"))

    def test_single_wildcard_matches_one_segment_only(self) -> None:
        self.assertTrue(topic_matches("payment.*", "payment.captured"))
        self.assertFalse(topic_matches("payment.*", "payment.card.captured"))
        self.assertFalse(topic_matches("payment.*", "payment"))

    def test_tail_wildcard_matches_zero_or_more_segments(self) -> None:
        self.assertTrue(topic_matches("payment.#", "payment"))
        self.assertTrue(topic_matches("payment.#", "payment.card.captured"))
        self.assertFalse(topic_matches("payment.#", "invoice.payment"))

    def test_hash_must_be_final(self) -> None:
        with self.assertRaises(ValidationError):
            validate_pattern("payment.#.created")

    def test_topics_are_lowercase_and_bounded(self) -> None:
        for value in ("Payment.created", "payment..created", "9payment.created"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_topic(value)


class FilterTests(unittest.TestCase):
    def test_empty_filter_matches_every_payload(self) -> None:
        self.assertTrue(compile_filter({}).matches(None))
        self.assertTrue(compile_filter({}).matches({"a": 1}))

    def test_nested_path_distinguishes_missing_from_null(self) -> None:
        exists = compile_filter({"customer.email": {"$exists": True}})
        equals_null = compile_filter({"customer.email": {"$eq": None}})
        self.assertFalse(exists.matches({"customer": {}}))
        self.assertTrue(exists.matches({"customer": {"email": None}}))
        self.assertFalse(equals_null.matches({"customer": {}}))
        self.assertTrue(equals_null.matches({"customer": {"email": None}}))

    def test_logical_operators_compose(self) -> None:
        compiled = compile_filter(
            {
                "$and": [
                    {"currency": {"$in": ["CNY", "USD"]}},
                    {"$or": [{"amount": {"$gte": 100}}, {"vip": True}]},
                    {"$not": {"status": "blocked"}},
                ]
            }
        )
        self.assertTrue(compiled.matches({"currency": "CNY", "amount": 120}))
        self.assertTrue(
            compiled.matches({"currency": "USD", "amount": 2, "vip": True})
        )
        self.assertFalse(
            compiled.matches({"currency": "CNY", "amount": 120, "status": "blocked"})
        )

    def test_prefix_and_ordered_types_are_strict(self) -> None:
        compiled = compile_filter(
            {"reference": {"$prefix": "SO-"}, "amount": {"$lte": 500}}
        )
        self.assertTrue(compiled.matches({"reference": "SO-1", "amount": 499}))
        self.assertFalse(compiled.matches({"reference": "RF-1", "amount": 499}))
        self.assertFalse(compiled.matches({"reference": "SO-1", "amount": "499"}))

    def test_filter_json_is_canonical(self) -> None:
        left = compile_filter({"z": 1, "a": {"$in": [2, 1]}})
        right = compile_filter('{"a":{"$in":[2,1]},"z":1}')
        self.assertEqual(left.to_json(), right.to_json())

    def test_rejects_unsupported_operators_and_nonfinite_values(self) -> None:
        invalid = [
            {"amount": {"$gt": 3}},
            {"$or": []},
            {"amount": float("nan")},
            {"amount": {"nested": 1}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                compile_filter(value)

    def test_rejects_excessive_depth(self) -> None:
        value = {"a": 1}
        for _ in range(10):
            value = {"$not": value}
        with self.assertRaises(ValidationError):
            compile_filter(value)


if __name__ == "__main__":
    unittest.main()

