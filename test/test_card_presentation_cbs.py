# -*- coding: utf-8 -*-
"""CBS macro/variable evaluator, card presentation extraction, and branch variable APIs."""

import asyncio
import base64
import json
import shutil
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend import api_routes, card_presentation, cbs, charx, companion, companion_db, paths, rpack, world_book_db


class CbsEvaluatorTest(unittest.TestCase):
    def test_identity_and_unknown_macros(self):
        out = cbs.evaluate_cbs(
            'Hi {{user}}, I am {{char}}.{{// hidden}} Keep {{unknown_macro}}.',
            char_name='Aria',
            user_name='Bob',
        )
        self.assertEqual(out, 'Hi Bob, I am Aria. Keep {{unknown_macro}}.')

    def test_twu_nested_conditional_with_variables(self):
        tpl = (
            '1. Emotion: `<img="<Char>_<Emo>">`\n'
            '{{#if_pure {{? {{getvar::SexualTWU}}!=0}} }}\n'
            '2. Sexual: `<img="<Breast>_<Act>">`\n'
            '{{/if_pure}}'
        )
        off = cbs.evaluate_cbs(tpl, variables={'SexualTWU': '0'})
        self.assertNotIn('Sexual:', off)
        self.assertIn('Emotion:', off)

        on = cbs.evaluate_cbs(tpl, variables={'SexualTWU': '1'})
        self.assertIn('2. Sexual:', on)

    def test_if_else_and_arithmetic(self):
        tpl = '{{#if {{? {{getvar::hp}} > 10 && {{getvar::ok}} == 1}}}}alive{{:else}}down{{/if}}'
        self.assertEqual(cbs.evaluate_cbs(tpl, variables={'hp': '25', 'ok': '1'}), 'alive')
        self.assertEqual(cbs.evaluate_cbs(tpl, variables={'hp': '5', 'ok': '1'}), 'down')
        self.assertEqual(cbs.evaluate_cbs('{{calc::(2 + 3) * 4}}'), '20')

    def test_setvar_and_addvar_mutation_opt_in(self):
        vars_map = {'score': '10'}
        # Read-only pass must not mutate caller map
        cbs.evaluate_cbs('{{addvar::score::5}}', variables=vars_map, allow_mutation=False)
        self.assertEqual(vars_map['score'], '10')

        cbs.evaluate_cbs('{{addvar::score::5}}{{setvar::flag::yes}}', variables=vars_map, allow_mutation=True)
        self.assertEqual(vars_map['score'], '15')
        self.assertEqual(vars_map['flag'], 'yes')

    def test_pick_is_deterministic_per_seed(self):
        a = cbs.evaluate_cbs('{{pick::red::blue::green}}', seed='c1:main')
        b = cbs.evaluate_cbs('{{pick::red::blue::green}}', seed='c1:main')
        self.assertEqual(a, b)
        self.assertIn(a, ('red', 'blue', 'green'))


class CardPresentationExtractionTest(unittest.TestCase):
    def test_extracts_risu_and_st_metadata(self):
        raw = {
            'spec': 'chara_card_v3',
            'data': {
                'extensions': {
                    'risuai': {
                        'defaultVariables': 'FutureTWU=1\nSexualTWU=0\n',
                        'backgroundHTML': '<style>.twu-asset { color: red; }</style>',
                        'customScripts': [
                            {'type': 'editdisplay', 'comment': 'Frame', 'in': '<img="([^"]+)">', 'out': '<img src="$1">', 'flag': 'gi', 'ableFlag': True},
                            {'type': 'editinput', 'comment': 'Skip', 'in': 'x', 'out': 'y'},
                        ],
                    },
                    'regex_scripts': [
                        {'scriptName': 'ST Bar', 'findRegex': '/\\[HP:(\\d+)\\]/g', 'replaceString': '<b>$1</b>', 'placement': [2], 'markdownOnly': True},
                        {'scriptName': 'Disabled', 'findRegex': 'abc', 'replaceString': 'def', 'disabled': True},
                    ],
                },
            },
        }
        pres = card_presentation.extract_card_presentation(json.dumps(raw))
        self.assertEqual(pres['default_variables'], {'FutureTWU': '1', 'SexualTWU': '0'})
        self.assertIn('.twu-asset { color: red; }', pres['card_css'])
        self.assertEqual(len(pres['display_regex']), 3)
        self.assertEqual(pres['display_regex'][0]['name'], 'Frame')
        self.assertEqual(pres['display_regex'][1]['name'], 'ST Bar')
        self.assertEqual(pres['display_regex'][1]['pattern'], r'\[HP:(\d+)\]')
        self.assertFalse(pres['display_regex'][2]['enabled'])

    def test_overrides_win_over_raw_card(self):
        raw = {'data': {'extensions': {'risuai': {'backgroundHTML': '<style>.a{}</style>'}}}}
        pres = card_presentation.extract_card_presentation(
            raw,
            card_ui={'card_css': '.b{}', 'display_regex': [{'pattern': 'foo', 'replace': 'bar'}]},
        )
        self.assertEqual(pres['card_css'], '.b{}')
        self.assertEqual(len(pres['display_regex']), 1)
        self.assertEqual(pres['display_regex'][0]['pattern'], 'foo')

    def test_auto_frame_bridge_synthesizes_twu_wrapper_when_missing(self):
        twu_css = (
            '<style>'
            '.twu-asset{position:relative;}'
            '.twu-asset-inner{border:1px solid gold;}'
            '.twu-asset-trigger{display:none;}'
            '.twu-asset-profile{position:absolute;}'
            '</style>'
        )
        raw = {'data': {'extensions': {'risuai': {'backgroundHTML': twu_css}}}}
        pres = card_presentation.extract_card_presentation(raw)
        ids = [r['id'] for r in pres['display_regex']]
        self.assertIn('auto_twu_frame', ids)
        self.assertTrue(any('twu-asset-profile' in r['replace'] for r in pres['display_regex']))

        # Does not duplicate if an <img regex rule already exists
        raw_with_img = {
            'data': {
                'extensions': {
                    'risuai': {
                        'backgroundHTML': twu_css,
                        'customScripts': [
                            {'type': 'editdisplay', 'comment': 'Custom Img', 'in': '<img src="([^"]+)">', 'out': '<div class="custom"><img src="$1"></div>', 'ableFlag': True}
                        ],
                    }
                }
            }
        }
        pres_with_img = card_presentation.extract_card_presentation(raw_with_img)
        self.assertNotIn('auto_twu_frame', [r['id'] for r in pres_with_img['display_regex']])


class BranchVariablesIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-cbs-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH
        raw = json.dumps({
            'spec': 'chara_card_v3',
            'data': {
                'name': 'TWU',
                'extensions': {
                    'risuai': {
                        'defaultVariables': 'SexualTWU=0\nEventTWU=1',
                        'backgroundHTML': '<style>.twu-hud { padding: 4px; }</style>',
                    },
                },
            },
        })
        companion_db.create_companion(
            'c_twu',
            {
                'name': 'TWU',
                'post_history_instructions': 'Base\n{{#if_pure {{? {{getvar::SexualTWU}}!=0}} }}NSFW{{/if_pure}}',
            },
            raw_card_data=raw,
        )

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_companion_prompt_and_api_follow_branch_variables(self):
        c = companion.Companion('c_twu')
        msgs_off = c._build_messages([], branch_id='main')
        self.assertNotIn('NSFW', msgs_off[-1]['content'])

        asyncio.run(api_routes.put_companion_branch_variables(
            'c_twu',
            'main',
            api_routes.BranchVariablesUpdateRequest(variables={'SexualTWU': '1', 'EventTWU': '1'}),
        ))
        msgs_on = c._build_messages([], branch_id='main')
        self.assertIn('NSFW', msgs_on[-1]['content'])

        reset_res = asyncio.run(api_routes.reset_companion_branch_variables('c_twu', 'main'))
        self.assertEqual(reset_res['variables']['SexualTWU'], '0')

        detail = companion_db.get_companion('c_twu')
        self.assertEqual(detail['card_presentation']['default_variables']['SexualTWU'], '0')
        self.assertIn('.twu-hud', detail['card_presentation']['card_css'])


def _build_test_risum_bytes(module_dict, asset_blobs=()):
    main_payload = json.dumps({'type': 'risuModule', 'module': module_dict}, ensure_ascii=False).encode('utf-8')
    encoded_main = rpack.encode_rpack(main_payload)
    buf = bytearray()
    buf.append(rpack.RPACK_MAGIC)
    buf.append(rpack.RPACK_VERSION)
    buf.extend(struct.pack('<I', len(encoded_main)))
    buf.extend(encoded_main)
    for blob in asset_blobs:
        encoded_blob = rpack.encode_rpack(blob)
        buf.append(1)
        buf.extend(struct.pack('<I', len(encoded_blob)))
        buf.extend(encoded_blob)
    buf.append(0)
    return bytes(buf)


class RisumAndCharxModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble_risum_test_'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        orig_companion = companion_db.DB_PATH
        orig_world = world_book_db.DB_PATH
        orig_data_root = paths.DATA_ROOT
        orig_assets_dir = paths.COMPANION_ASSETS_DIR

        def _restore_paths():
            companion_db.DB_PATH = orig_companion
            world_book_db.DB_PATH = orig_world
            paths.DATA_ROOT = orig_data_root
            paths.COMPANION_ASSETS_DIR = orig_assets_dir

        self.addCleanup(_restore_paths)

        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH
        paths.DATA_ROOT = self.tmp
        paths.COMPANION_ASSETS_DIR = self.tmp / 'companion_assets'

        companion_db.create_companion('c_risum', {'name': 'Module Host'})

    def test_rpack_roundtrip_json_and_error(self):
        png_bytes = b'\x89PNG\r\n\x1a\nfakepngpayload'
        mod = {'name': 'TestMod', 'regex': [], 'assets': [['badge', '', 'png']]}
        raw = _build_test_risum_bytes(mod, [png_bytes])

        parsed_mod, blobs = rpack.parse_risum_bytes(raw)
        self.assertEqual(parsed_mod['name'], 'TestMod')
        self.assertEqual(blobs, [png_bytes])

        json_raw = json.dumps({'type': 'risuModule', 'module': {'name': 'PlainJsonMod'}}).encode('utf-8')
        parsed_json, json_blobs = rpack.parse_risum_bytes(json_raw)
        self.assertEqual(parsed_json['name'], 'PlainJsonMod')
        self.assertEqual(json_blobs, [])

        with self.assertRaises(rpack.RisumError):
            rpack.parse_risum_bytes(b'BAD_MAGIC_BYTES')

    def test_api_base64_risum_import_merges_and_dedups(self):
        png_bytes = b'\x89PNG\r\n\x1a\nmoduleasset'
        mod = {
            'name': 'FramePack',
            'backgroundEmbedding': '<style>.mod-frame { border: 2px solid #cba; }</style>',
            'defaultVariables': 'FrameMode=1\nHudOpen=0\n',
            'regex': [
                {
                    'type': 'editdisplay',
                    'comment': 'Wrap Img',
                    'in': '<img src="([^"]+)">',
                    'out': '<div class="mod-frame"><img src="$1"></div>',
                    'flag': 'gi',
                    'ableFlag': True,
                }
            ],
            'assets': [['frame_bg', '', 'png']],
        }
        risum_bytes = _build_test_risum_bytes(mod, [png_bytes])
        req = api_routes.RisumBase64ImportRequest(
            filename='frame.risum',
            data_b64=base64.b64encode(risum_bytes).decode('ascii'),
        )
        res = asyncio.run(api_routes.import_risum_base64_for_companion('c_risum', req))
        self.assertEqual(res['status'], 'success')
        self.assertEqual(res['module_name'], 'FramePack')
        self.assertEqual(res['added_regex'], 1)
        self.assertTrue(res['added_css'])
        self.assertEqual(res['added_assets'], 1)
        self.assertIn('.mod-frame', res['card_presentation']['card_css'])
        self.assertEqual(res['card_presentation']['default_variables']['FrameMode'], '1')

        assets = companion_db.get_companion_assets('c_risum')
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]['name'], 'frame_bg')

        # Re-importing same .risum must deduplicate rules, CSS, and assets
        res2 = asyncio.run(api_routes.import_risum_base64_for_companion('c_risum', req))
        self.assertEqual(res2['added_regex'], 0)
        self.assertFalse(res2['added_css'])
        self.assertEqual(res2['added_assets'], 0)

    def test_charx_auto_extracts_embedded_risum_module(self):
        png_bytes = b'\x89PNG\r\n\x1a\nembeddedbadge'
        mod = {
            'name': 'BundledMod',
            'backgroundEmbedding': '<style>.bundled-hud { color: cyan; }</style>',
            'defaultVariables': 'BundledFlag=true\n',
            'regex': [
                {
                    'type': 'editdisplay',
                    'comment': 'Bundled Img Rule',
                    'in': '<img src="([^"]+)">',
                    'out': '<figure class="bundled-hud"><img src="$1"></figure>',
                    'flag': 'gi',
                    'ableFlag': True,
                }
            ],
            'assets': [['bundled_badge', '', 'png']],
        }
        risum_bytes = _build_test_risum_bytes(mod, [png_bytes])
        card_json = {
            'spec': 'chara_card_v3',
            'data': {
                'name': 'CharxWithModule',
                'description': 'Hello',
                'extensions': {},
            },
        }
        charx_path = self.tmp / 'with_module.charx'
        with zipfile.ZipFile(charx_path, 'w') as zf:
            zf.writestr('card.json', json.dumps(card_json))
            zf.writestr('module.risum', risum_bytes)

        raw_card, extracted_assets, _ = charx.extract_charx_package(charx_path, 'c_charx_mod')
        pres = card_presentation.extract_card_presentation(raw_card)
        self.assertIn('.bundled-hud', pres['card_css'])
        self.assertEqual(pres['default_variables'].get('BundledFlag'), 'true')
        self.assertTrue(any(r['name'] == 'Bundled Img Rule' for r in pres['display_regex']))
        self.assertTrue(any(a['name'] == 'bundled_badge' for a in extracted_assets))


if __name__ == '__main__':
    unittest.main()
