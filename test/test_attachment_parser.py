# -*- coding: utf-8 -*-
"""Attachment classification and extraction (images, text, documents)."""

import io
import unittest

from backend import attachment_parser as ap


def _png_bytes(size=(2, 3), colour=(200, 30, 30)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', size, colour).save(buf, 'PNG')
    return buf.getvalue()


class ClassifyTest(unittest.TestCase):
    def test_images_are_classified(self):
        for name in ('a.png', 'b.JPG', 'c.webp'):
            self.assertEqual(ap.classify(name), 'image', name)

    def test_documents_are_classified(self):
        for name in ('a.txt', 'b.md', 'c.pdf', 'd.docx', 'e.xlsx'):
            self.assertEqual(ap.classify(name), 'document', name)

    def test_unknown_extensions_are_rejected(self):
        for name in ('a.exe', 'b', 'c.tar.gz'):
            self.assertIsNone(ap.classify(name), name)

    def test_supported_extensions_lists_both_groups(self):
        groups = ap.supported_extensions()
        self.assertIn('png', groups['image'])
        self.assertIn('pdf', groups['document'])


class TextExtractionTest(unittest.TestCase):
    def test_plain_text_round_trips(self):
        self.assertEqual(ap.extract_document_text('a.txt', 'héllo'.encode('utf-8')), 'héllo')

    def test_bytes_that_are_not_utf8_do_not_raise(self):
        # The decoder walks utf-8 -> utf-8-sig -> gb18030 -> latin-1, so any byte
        # string has to come back as text rather than an exception.
        self.assertIsInstance(ap.extract_document_text('a.txt', b'\xff\xfe\x00bad'), str)

    def test_json_is_pretty_printed(self):
        text = ap.extract_document_text('a.json', b'{"b":1,"a":2}')
        self.assertIn('"a": 2', text)

    def test_broken_json_falls_back_to_raw_text(self):
        self.assertEqual(ap.extract_document_text('a.json', b'{broken'), '{broken')

    def test_unsupported_extension_raises(self):
        with self.assertRaises(ap.AttachmentError):
            ap.extract_document_text('a.exe', b'x')


class BuildAttachmentTest(unittest.TestCase):
    def test_empty_payload_is_rejected(self):
        with self.assertRaises(ap.AttachmentError):
            ap.build_attachment('a.txt', b'')

    def test_unknown_extension_is_rejected(self):
        with self.assertRaises(ap.AttachmentError):
            ap.build_attachment('a.exe', b'x')

    def test_text_document_descriptor(self):
        item = ap.build_attachment('notes.txt', b'hello world')
        self.assertEqual(item['kind'], 'document')
        self.assertEqual(item['name'], 'notes.txt')
        self.assertEqual(item['text'], 'hello world')
        self.assertEqual(item['chars'], 11)
        self.assertFalse(item['truncated'])

    def test_oversized_text_is_truncated(self):
        item = ap.build_attachment('big.txt', b'x' * (ap.MAX_TEXT_CHARS + 500))
        self.assertTrue(item['truncated'])
        self.assertEqual(len(item['text']), ap.MAX_TEXT_CHARS)

    def test_image_descriptor_carries_size_and_url(self):
        item = ap.build_attachment('pic.png', _png_bytes())
        self.assertEqual(item['kind'], 'image')
        self.assertEqual(item['width'], 2)
        self.assertEqual(item['height'], 3)
        self.assertTrue(item['url'].startswith('/uploads/attachments/'))

    def test_unreadable_image_is_rejected(self):
        with self.assertRaises(ap.AttachmentError):
            ap.build_attachment('fake.png', b'not really a png')


class PromptFormattingTest(unittest.TestCase):
    def test_documents_are_rendered_with_their_names(self):
        block = ap.format_documents_for_prompt([
            {'kind': 'document', 'name': 'a.txt', 'text': 'body'},
        ])
        self.assertIn('a.txt', block)
        self.assertIn('body', block)

    def test_images_are_skipped(self):
        block = ap.format_documents_for_prompt([
            {'kind': 'image', 'name': 'p.png', 'url': '/x'},
        ])
        self.assertEqual(block, '')

    def test_truncation_is_announced(self):
        block = ap.format_documents_for_prompt([
            {'kind': 'document', 'name': 'a.txt', 'text': 'body', 'truncated': True},
        ])
        self.assertIn('已截断', block)

    def test_empty_list_renders_nothing(self):
        self.assertEqual(ap.format_documents_for_prompt([]), '')


if __name__ == '__main__':
    unittest.main()
