"""Tests for app/language/experience_shape.py (P6D-5). Pure.
    docker compose exec python-api python -m unittest tests.test_experience_shape -v
"""
import unittest

from app.language.experience_shape import GroupKey, canonical_form, group_key, shape_hash


class TestCanonicalForm(unittest.TestCase):
    def test_entity_values_are_masked(self):
        a = canonical_form("where is order #A1042?", ["#A1042"])
        b = canonical_form("where is order #B9981?", ["#B9981"])
        self.assertEqual(a, b)
        self.assertIn("<e>", a)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(canonical_form("Price Koto?", []), canonical_form("price   koto?  ", []))

    def test_no_entities_is_plain_normalization(self):
        self.assertEqual(canonical_form("Hello World", []), "hello world")

    def test_negation_is_never_masked_or_collapsed(self):
        pos = canonical_form("I want the red one", [])
        neg = canonical_form("I do NOT want the red one", [])
        self.assertNotEqual(pos, neg)

    def test_longer_span_matched_before_shorter_substring_span(self):
        # "order 42" contains "42"; the longer span must be masked first
        # so "42" alone doesn't leave a stray placeholder inside another.
        out = canonical_form("please check order 42 now", ["order 42", "42"])
        self.assertEqual(out.count("<e>"), 1)

    def test_multiple_distinct_entities_each_masked(self):
        out = canonical_form("send 3 red mugs to Dhaka", ["3", "red mugs", "Dhaka"])
        self.assertEqual(out.count("<e>"), 3)

    def test_none_and_missing_inputs_are_safe(self):
        self.assertEqual(canonical_form(None, None), "")
        self.assertEqual(canonical_form(None, ["x"]), "")
        self.assertEqual(canonical_form("hi", None), "hi")

    def test_non_string_entity_in_list_does_not_raise(self):
        out = canonical_form("value is 5", [5, None, "5"])  # noqa: type
        self.assertIn("<e>", out)

    def test_blank_entity_spans_ignored(self):
        self.assertEqual(canonical_form("hello", ["", "   "]), "hello")


class TestShapeHash(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(shape_hash("price koto?", []), shape_hash("price koto?", []))

    def test_different_shapes_different_hash(self):
        self.assertNotEqual(shape_hash("I want it", []), shape_hash("I do not want it", []))

    def test_same_shape_different_entity_values_same_hash(self):
        self.assertEqual(
            shape_hash("order #A1042 status", ["#A1042"]),
            shape_hash("order #B9981 status", ["#B9981"]),
        )

    def test_is_a_hex_sha256(self):
        h = shape_hash("hi", [])
        self.assertEqual(len(h), 64)
        int(h, 16)  # raises if not hex

    def test_never_reversible_to_original_text(self):
        h = shape_hash("some very specific customer sentence", [])
        self.assertNotIn("customer", h)
        self.assertNotIn("specific", h)


class TestGroupKey(unittest.TestCase):
    def test_same_shape_different_tenant_is_a_different_key(self):
        a = group_key(tenant_id=1, intent="PRICE_INQUIRY", language="en", normalized_message="how much?", entity_spans=[])
        b = group_key(tenant_id=2, intent="PRICE_INQUIRY", language="en", normalized_message="how much?", entity_spans=[])
        self.assertNotEqual(a.as_key(), b.as_key())

    def test_same_shape_different_intent_is_a_different_key(self):
        # This is the critical safety property: a minimal pair that was
        # CONFIRMED to two different intents must never merge just
        # because their shapes happen to be close/identical.
        a = group_key(tenant_id=1, intent="CREATE_ORDER", language="en",
                       normalized_message="i want the red one", entity_spans=[])
        b = group_key(tenant_id=1, intent="PRODUCT_INQUIRY", language="en",
                       normalized_message="i want the red one", entity_spans=[])
        self.assertNotEqual(a.as_key(), b.as_key())

    def test_same_tenant_intent_language_and_shape_is_the_same_key(self):
        a = group_key(tenant_id=1, intent="ORDER_STATUS", language="en",
                       normalized_message="order #A1042 status", entity_spans=["#A1042"])
        b = group_key(tenant_id=1, intent="ORDER_STATUS", language="en",
                       normalized_message="order #B9981 status", entity_spans=["#B9981"])
        self.assertEqual(a.as_key(), b.as_key())

    def test_missing_language_defaults_to_und(self):
        k = group_key(tenant_id=1, intent="X", language=None, normalized_message="hi", entity_spans=[])
        self.assertEqual(k.language, "und")

    def test_frozen_and_comparable(self):
        k1 = GroupKey(1, "X", "en", "h")
        k2 = GroupKey(1, "X", "en", "h")
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
