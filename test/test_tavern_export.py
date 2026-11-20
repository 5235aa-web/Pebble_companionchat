# -*- coding: utf-8 -*-
"""Character card export (Tavern V3 JSON and PNG with a tEXt chunk)."""

import base64
import io
import json
import unittest

from backend import tavern_exporter


def _companion(**over):
    data = {
        'name': 'Aria',
        'description': 'a bard',
        'personality': 'warm',
        'scenario': 'a tavern',
        'first_mes': 'hello',
        'mes_example': '',
        'creator_notes': 'note',
        'system_prompt': '',
        'post_history_instructions': '',
        'creator': 'me',
        'character_version': '1.0',
        'tags': '["music", "bard"]',
        'alternate_greetings': '["hi again"]',
        'depth_prompt': '{"prompt": "stay in character", "depth": 4}',
    }
    data.update(over)
    return data


class V3CardTest(unittest.TestCase):
    def test_version_is_declared(self):
        card = tavern_exporter._create_v3_card(_companion())
        self.assertEqual(card['version'], 3)
        self.assertIn('data', card)

    def test_fields_are_carried_over(self):
        card = tavern_exporter._create_v3_card(_companion())
        data = card['data']
        self.assertEqual(data['name'], 'Aria')
        self.assertEqual(data['description'], 'a bard')
        self.assertEqual(data['first_mes'], 'hello')

    def test_json_string_fields_are_parsed(self):
        data = tavern_exporter._create_v3_card(_companion())['data']
        self.assertEqual(data['tags'], ['music', 'bard'])
        self.assertEqual(data['alternate_greetings'], ['hi again'])

    def test_broken_json_fields_fall_back_to_empty(self):
        data = tavern_exporter._create_v3_card(_companion(tags='{not json'))['data']
        self.assertEqual(data['tags'], [])

    def test_missing_name_falls_back_to_unknown(self):
        self.assertEqual(tavern_exporter._create_v3_card({})['data']['name'], 'Unknown')


class JsonExportTest(unittest.TestCase):
    def test_export_is_parsable_json(self):
        payload = json.loads(tavern_exporter.export_to_json(_companion()))
        self.assertEqual(payload['data']['name'], 'Aria')

    def test_bytes_export_matches_the_string_export(self):
        as_text = tavern_exporter.export_to_json(_companion())
        as_bytes = tavern_exporter.export_to_json_bytes(_companion())
        self.assertEqual(json.loads(as_bytes)['data']['name'], json.loads(as_text)['data']['name'])


class PngExportTest(unittest.TestCase):
    def _open(self):
        from PIL import Image

        png = tavern_exporter.export_to_png(_companion())
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        return Image.open(io.BytesIO(png))

    def test_output_is_a_png_with_embedded_card_data(self):
        keys = {k.lower() for k in (self._open().info or {})}
        self.assertTrue({'chara', 'ccv3'} & keys, keys)

    def test_embedded_payload_is_the_v3_card(self):
        info = self._open().info or {}
        raw = info.get('ccv3') or info.get('chara')
        self.assertTrue(raw, info.keys())
        payload = json.loads(base64.b64decode(raw).decode('utf-8'))
        self.assertEqual(payload['data']['name'], 'Aria')
        self.assertEqual(payload['version'], 3)


class FilenameTest(unittest.TestCase):
    def test_json_extension(self):
        self.assertTrue(tavern_exporter.get_export_filename(_companion()).endswith('.json'))

    def test_png_extension(self):
        self.assertTrue(tavern_exporter.get_export_filename(_companion(), 'png').endswith('.png'))


if __name__ == '__main__':
    unittest.main()
