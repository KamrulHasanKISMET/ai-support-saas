import unittest

from app.knowledge.chunker import chunk_text


class TestChunker(unittest.TestCase):
    def test_empty_text_returns_no_chunks(self):
        self.assertEqual(chunk_text("", chunk_size=100, overlap=10), [])
        self.assertEqual(chunk_text("   \n\n  ", chunk_size=100, overlap=10), [])

    def test_short_text_fits_in_a_single_chunk(self):
        chunks = chunk_text("A short paragraph.", chunk_size=1000, overlap=150)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].index, 0)
        self.assertEqual(chunks[0].content, "A short paragraph.")

    def test_paragraphs_are_packed_until_they_would_exceed_chunk_size(self):
        paragraphs = [f"Paragraph number {i} with some filler text." for i in range(10)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text, chunk_size=120, overlap=20)

        self.assertGreater(len(chunks), 1)
        # No chunk (except possibly ones from hard-splitting) exceeds chunk_size.
        for c in chunks:
            self.assertLessEqual(len(c.content), 120)
        # Indices are sequential starting at 0.
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))

    def test_no_content_is_dropped_across_chunks(self):
        paragraphs = [f"P{i}" for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text, chunk_size=15, overlap=3)
        joined = " ".join(c.content for c in chunks)
        for p in paragraphs:
            self.assertIn(p, joined)

    def test_oversized_single_paragraph_is_hard_split_with_overlap(self):
        long_paragraph = "x" * 250
        chunks = chunk_text(long_paragraph, chunk_size=100, overlap=20)

        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.content), 100)
        # Consecutive hard-split pieces share `overlap` characters.
        self.assertEqual(chunks[0].content[-20:], chunks[1].content[:20])

    def test_deterministic_same_input_same_output(self):
        text = "\n\n".join(f"Paragraph {i}. " * 5 for i in range(8))
        first = chunk_text(text, chunk_size=200, overlap=30)
        second = chunk_text(text, chunk_size=200, overlap=30)
        self.assertEqual([c.content for c in first], [c.content for c in second])

    def test_rejects_invalid_chunk_size(self):
        with self.assertRaises(ValueError):
            chunk_text("some text", chunk_size=0, overlap=0)
        with self.assertRaises(ValueError):
            chunk_text("some text", chunk_size=-10, overlap=0)

    def test_rejects_overlap_not_smaller_than_chunk_size(self):
        with self.assertRaises(ValueError):
            chunk_text("some text", chunk_size=100, overlap=100)
        with self.assertRaises(ValueError):
            chunk_text("some text", chunk_size=100, overlap=150)
        with self.assertRaises(ValueError):
            chunk_text("some text", chunk_size=100, overlap=-1)


if __name__ == "__main__":
    unittest.main()
