import unittest

from hearth.chunking import chunk_page
from hearth.domain import ExtractedPage


def _page(text: str) -> ExtractedPage:
    return ExtractedPage(page_number=1, text=text)


class ChunkingTests(unittest.TestCase):
    def _chunks(self, text: str, **kwargs):
        chunks = chunk_page(_page(text), **kwargs)
        for chunk_text, start, end in chunks:
            self.assertLess(start, end)
            self.assertLessEqual(end, len(text))
            self.assertEqual(text[start:end].split(), chunk_text.split())
        return chunks

    def test_markdown_list_is_not_one_unbounded_chunk(self):
        text = "# Notes\n\n" + "\n".join(f"- item {index} worth retrieving" for index in range(200))
        chunks = self._chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(max(len(chunk[0].split()) for chunk in chunks), 460)

    def test_heading_is_its_own_unit(self):
        chunks = self._chunks("# Title\n\nSome prose follows here.", target_words=4, overlap_words=1)
        self.assertTrue(any(chunk[0].startswith("# Title") for chunk in chunks))

    def test_text_without_punctuation_or_structure_is_bounded(self):
        chunks = self._chunks("word " * 5000)
        self.assertLessEqual(max(len(chunk[0].split()) for chunk in chunks), 460)

    def test_prose_still_chunks_on_sentence_boundaries(self):
        chunks = self._chunks(" ".join("This is a normal sentence with words." for _ in range(200)))
        self.assertLessEqual(max(len(chunk[0].split()) for chunk in chunks), 400)

    def test_overlap_never_exceeds_its_budget(self):
        # A unit larger than the overlap budget carries nothing back.
        chunks = self._chunks("word " * 1000, target_words=100, overlap_words=10)
        self.assertLessEqual(max(len(chunk[0].split()) for chunk in chunks), 110)

    def test_rejects_invalid_sizes(self):
        for target, overlap in ((0, 0), (10, 10), (10, -1)):
            with self.assertRaises(ValueError):
                chunk_page(_page("text"), target_words=target, overlap_words=overlap)


if __name__ == "__main__":
    unittest.main()
