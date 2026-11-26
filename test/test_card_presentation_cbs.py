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

    def test_risu_assetlist_each_array_spread_tempvar_and_when(self):
        tpl = (
            '{{settempvar::exactAsset::{{pick::{{spread::{{array\n\n'
            '{{#each {{assetlist}} item}}\n\n'
            '{{#if {{equal::{{slot::item}}::Motoko_painful}}}}::{{slot::item}}{{/if}}\n\n'
            '{{/each}}\n\n'
            '}}}}}}}}\n\n'
            '{{#when::{{gettempvar::exactAsset}}::isnot::}}\n\n'
            '{{:else}}\n\n'
            '("Motoko_painful" - 존재하지 않는 에셋입니다.)\n'
            '{{/when}}'
            '{{gettempvar::exactAsset}}'
        )
        found = cbs.evaluate_cbs(
            tpl,
            seed='motoko:main',
            asset_names=['Motoko_happy', 'Motoko_painful', 'Motoko_angry'],
        )
        self.assertNotIn('존재하지 않는 에셋입니다', found)
        self.assertNotIn('{{', found)
        self.assertEqual(found.strip(), 'Motoko_painful')

        missing = cbs.evaluate_cbs(
            tpl,
            seed='motoko:main',
            asset_names=['Motoko_happy', 'Motoko_angry'],
        )
        self.assertIn('("Motoko_painful" - 존재하지 않는 에셋입니다.)', missing)
        self.assertNotIn('{{', missing)

    def test_each_nested_when_operators_and_tempvars(self):
        nested = (
            '{{#when::{{getvar::mode}}::is::combat::and::{{getvar::hp}}::>::0}}'
            '{{#if {{equal::{{getvar::ok}}::1}}}}fight{{:else}}wait{{/if}}'
            '{{:else}}'
            'rest'
            '{{/when}}'
        )
        self.assertEqual(
            cbs.evaluate_cbs(nested, variables={'mode': 'combat', 'hp': '10', 'ok': '1'}),
            'fight',
        )
        self.assertEqual(
            cbs.evaluate_cbs(nested, variables={'mode': 'combat', 'hp': '10', 'ok': '0'}),
            'wait',
        )
        self.assertEqual(
            cbs.evaluate_cbs(nested, variables={'mode': 'combat', 'hp': '0', 'ok': '1'}),
            'rest',
        )

        loop_tpl = (
            '{{settempvar::n::1}}{{addtempvar::n::2}}'
            '{{#each {{array::a::b}} as row}}'
            '[{{slot::row}}:{{#each {{array::1::2}} col}}{{slot::col}}{{/each}}]'
            '{{/each}}'
            ':{{gettempvar::n}}'
        )
        self.assertEqual(cbs.evaluate_cbs(loop_tpl), '[a:12][b:12]:3')

    def test_spread_into_pick_and_empty_spread(self):
        self.assertEqual(cbs.evaluate_cbs('{{pick::{{spread::{{array}}}}}}'), '')
        picked = cbs.evaluate_cbs('{{pick::{{spread::{{array::x::y}}}}}}', seed='s1')
        self.assertIn(picked, ('x', 'y'))
        self.assertEqual(cbs.evaluate_cbs('{{spread::{{array::x::y}}}}'), 'x::y')

    def test_chat_context_time_and_text_macros(self):
        tpl = (
            '{{#if {{greater_equal::{{chat_index}}::{{? {{lastmessageid}}-5}}}}}}show{{/if}}|'
            '{{lastmessage}}|{{lastcharmessage}}|{{lastusermessage}}|'
            '{{replace::{{lower::Hello World}}::world::motoko}}|'
            '{{upper::ok}}|{{contains::abcde::cd}}|{{startswith::Motoko::Mo}}|'
            '{{endswith::Motoko::ko}}|{{length::{{array::a::b::c}}}}|'
            '{{join::{{split::a-b-c::-}}::,}}|{{idle_duration}}'
        )
        out = cbs.evaluate_cbs(
            tpl,
            chat_index=6,
            last_message_id=10,
            last_message='latest',
            last_char_message='ai_msg',
            last_user_message='user_msg',
            idle_duration='5m',
        )
        self.assertEqual(
            out,
            'show|latest|ai_msg|user_msg|hello motoko|OK|1|1|1|3|a,b,c|5m',
        )
        hidden = cbs.evaluate_cbs(
            '{{#if {{greater_equal::{{chat_index}}::{{? {{lastmessageid}}-5}}}}}}show{{/if}}',
            chat_index=4,
            last_message_id=10,
        )
        self.assertEqual(hidden, '')
        self.assertRegex(cbs.evaluate_cbs('{{date}} {{time}}'), r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')


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

    def test_companion_macro_context_injects_asset_names(self):
        companion_db.add_companion_assets('c_twu', [{
            'asset_type': 'emotion',
            'name': 'Motoko_painful',
            'ext': 'png',
            'mime_type': 'image/png',
            'rel_path': 'uploads/companion_assets/c_twu/0000_Motoko_painful.png',
            'source_uri': 'embeded://assets/Motoko_painful.png',
            'size_bytes': 123,
            'sort_order': 0,
        }])
        c = companion.Companion('c_twu')
        sub = c._macro_context('main')
        self.assertIn('Motoko_painful', sub('{{assetlist}}'))

    def test_editoutput_and_fuzzy_asset_typo_normalization(self):
        raw = json.dumps({
            'spec': 'chara_card_v3',
            'data': {
                'name': 'Motoko',
                'extensions': {
                    'risuai': {
                        'customScripts': [
                            {'type': 'editoutput', 'in': '`<', 'out': '<', 'ableFlag': False},
                            {'type': 'editoutput', 'in': '>`', 'out': '>', 'ableFlag': False},
                            {'type': 'editoutput', 'in': '(<img) src="([^"]*)">', 'out': '$1="$2">', 'ableFlag': False},
                            {'type': 'editoutput', 'in': '_flustered"', 'out': '_embarrassed"', 'ableFlag': False},
                        ],
                    },
                },
            },
        })
        companion_db.create_companion('c_motoko_fix', {'name': 'Motoko'}, raw_card_data=raw)
        companion_db.add_companion_assets('c_motoko_fix', [
            {
                'asset_type': 'x-risu-asset',
                'name': name,
                'ext': 'webp',
                'mime_type': 'image/webp',
                'rel_path': f'uploads/companion_assets/c_motoko_fix/{name}.webp',
                'source_uri': f'embeded://assets/{name}.webp',
                'size_bytes': 10,
                'sort_order': i,
            }
            for i, name in enumerate([
                'Motoko_smile', 'Motoko_kissing', 'Motoko_seducing',
                'Motoko_embarrassed', 'Motoko_angry', 'Motoko_happy',
            ])
        ])
        c = companion.Companion('c_motoko_fix')
        sample = (
            'Hello `<img src="Motoko_flustered">>`\n'
            '<img="Motoki_smile">\n'
            '<img="Motokkissing">\n'
            '<img="Motoko_seductive">\n'
            '<img="Motoko_completely_unknown">'
        )
        cleaned = c._finalize_assistant_output(sample, 'main')
        self.assertIn('<img="Motoko_embarrassed">', cleaned)
        self.assertIn('<img="Motoko_smile">', cleaned)
        self.assertIn('<img="Motoko_kissing">', cleaned)
        self.assertIn('<img="Motoko_seducing">', cleaned)
        self.assertIn('<img="Motoko_completely_unknown">', cleaned)

    def test_malformed_llm_image_tags_repaired_deduped_and_stripped(self):
        assets = [
            'Motoko_smile',
            'Motoko_seducing',
            'Motoko_lustful',
            'Motoko_touched',
            'Motoko_upright_straddle_penetration',
        ]
        sample = (
            'Para 1.\n\n'
            '<img="Motoko_smile">\n'
            '<mg("Moti koko seducing")>\n\n'
            'Para 2.\n\n'
            '<mg("Moti koko touched")>\n\n'
            'Para 3.\n\n'
            '<img="Motoko_lustful">\n'
            '<mg("Moti koko upright_straddle")>\n\n'
            'Para 4.\n\n'
            '<mgr("Motoko_unknown_extra")>\n'
            '<imb= Motoko_unknown_2>\n\n'
            '```html\n<mg("Motoko_smile")>\n```'
        )
        out = card_presentation.normalize_asset_tag_refs(sample, assets)
        # Duplicate malformed tag right after <img="Motoko_smile"> is dropped
        self.assertIn('<img="Motoko_smile">', out)
        self.assertNotIn('Motoko_seducing', out)
        # Standalone resolvable <mg("Moti koko touched")> is repaired to <img="Motoko_touched">
        self.assertIn('<img="Motoko_touched">', out)
        # Unresolvable <mg>, <mgr>, <imb=> outside code fences are stripped completely
        prose_only = out.split('```html', 1)[0]
        self.assertNotIn('<mg(', prose_only)
        self.assertNotIn('<mgr(', prose_only)
        self.assertNotIn('<imb=', prose_only)
        # Fenced code block stays untouched
        self.assertIn('```html\n<mg("Motoko_smile")>\n```', out)
        # Compact fuzzy match works for split character names
        self.assertEqual(
            card_presentation.resolve_fuzzy_asset_name('Moti koko upright_straddle_penetration', assets),
            'Motoko_upright_straddle_penetration',
        )
        self.assertIsNone(
            card_presentation.resolve_fuzzy_asset_name('Moti koko upright_straddle', assets)
        )


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


class SceneRulesStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-scene-rules-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH
        companion_db.create_companion('c_sr', {'name': 'Aoi', 'system_prompt': 'Base sys'})

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idle_minutes_and_hour_macros(self):
        out = cbs.evaluate_cbs(
            '{{#when::greater_equal::{{idle_minutes}}::120}}LONG:{{idle_duration}}{{:else}}SHORT{{/when}}|{{hour}}',
            idle_duration='2h 5m',
            idle_minutes=125,
        )
        prefix, hour_str = out.split('|')
        self.assertEqual(prefix, 'LONG:2h 5m')
        self.assertTrue(0 <= int(hour_str) <= 23)

    def test_scene_rules_merge_and_prompt_injection(self):
        res = asyncio.run(api_routes.update_scene_rules(
            'c_sr',
            api_routes.SceneRulesUpdateRequest(
                scene_rules=[
                    {
                        'id': 'r_sys',
                        'name': 'Late milestone',
                        'summary': 'Inject after turn 2',
                        'enabled': True,
                        'trigger_type': 'chat_turn',
                        'condition_cbs': '{{greater_equal::{{chat_index}}::2}}',
                        'prompt': 'Be warmer at turn {{chat_index}}.',
                        'placement': 'system',
                    },
                    {
                        'id': 'r_post',
                        'name': 'Post guard',
                        'summary': 'Always remind style',
                        'enabled': True,
                        'trigger_type': 'always',
                        'condition_cbs': '',
                        'prompt': 'Keep replies vivid for {{user}}.',
                        'placement': 'post_history',
                    },
                ],
                display_regex=[
                    {
                        'id': 'd1',
                        'name': 'Hide bracket',
                        'summary': 'Hide [note]',
                        'pattern': r'\[note:[^\]]*\]',
                        'flags': 'gi',
                        'replace': '',
                    }
                ],
            ),
        ))
        self.assertEqual(res['status'], 'success')
        self.assertEqual(len(res['card_presentation']['scene_rules']), 2)
        self.assertEqual(len(res['card_presentation']['display_regex']), 1)

        # Updating only output_regex must NOT clobber scene_rules or display_regex.
        res2 = asyncio.run(api_routes.update_scene_rules(
            'c_sr',
            api_routes.SceneRulesUpdateRequest(
                output_regex=[{
                    'id': 'o1',
                    'name': 'Clean OOC',
                    'pattern': r'\(OOC:[^)]*\)',
                    'flags': 'gi',
                    'replace': '',
                }],
            ),
        ))
        pres2 = res2['card_presentation']
        self.assertEqual(len(pres2['scene_rules']), 2)
        self.assertEqual(len(pres2['display_regex']), 1)
        self.assertEqual(len(pres2['output_regex']), 1)

        comp = companion.Companion('c_sr')
        short_hist = [{'role': 'user', 'content': 'hi'}]
        msgs_early = comp._build_messages(short_hist, branch_id='main')
        self.assertNotIn('Be warmer at turn', msgs_early[0]['content'])
        self.assertIn('Keep replies vivid', msgs_early[-1]['content'])

        long_hist = [
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hey'},
            {'role': 'user', 'content': 'again'},
        ]
        msgs_late = comp._build_messages(long_hist, branch_id='main')
        self.assertIn('Be warmer at turn 2.', msgs_late[0]['content'])
        cleaned = comp._finalize_assistant_output('Hello (OOC: skip)', branch_id='main')
        self.assertEqual(cleaned.strip(), 'Hello')

    def test_compile_scene_rule_auto_repairs_on_sample_mismatch(self):
        from backend import scene_rules_ai

        replies = [
            json.dumps({
                'rule_kind': 'display',
                'rule': {
                    'name': 'Thought Card',
                    'summary': 'Style inner thought',
                    'pattern': r'\[thought:(.*?)\]',
                    'flags': 'gi',
                    'replace': '<em>$1</em>',
                },
            }),
            json.dumps({
                'rule_kind': 'display',
                'rule': {
                    'name': '心声气泡',
                    'summary': '将【心声：...】变成斜体',
                    'pattern': r'【心声[:：]([\s\S]*?)】',
                    'flags': 'gi',
                    'replace': '<em>$1</em>',
                },
            }),
        ]

        async def _fake_collect(cfg, msgs):
            return replies.pop(0)

        orig = scene_rules_ai._collect_llm_text
        scene_rules_ai._collect_llm_text = _fake_collect
        try:
            res = asyncio.run(scene_rules_ai.compile_scene_rule(
                instruction='把【心声：...】变成斜体',
                sample_text='她微笑着。\n【心声：其实有点紧张。】',
                llm_config={'provider_type': 'openai', 'model': 'gpt-4o-mini'},
            ))
        finally:
            scene_rules_ai._collect_llm_text = orig

        self.assertTrue(res['repaired'])
        self.assertTrue(res['matched_sample'])
        self.assertIn('<em>其实有点紧张。</em>', res['preview_after'])

    def test_prompt_only_and_editprocess_with_depth_filtering(self):
        raw = json.dumps({
            'spec': 'chara_card_v3',
            'data': {
                'name': 'ContextTest',
                'extensions': {
                    'risuai': {
                        'customScripts': [
                            {'type': 'editprocess', 'in': '<think>[\\s\\S]*?</think>', 'out': '', 'flag': 'gi'},
                        ],
                    },
                    'regex_scripts': [
                        {
                            'id': 'st_p1',
                            'scriptName': 'Strip old status',
                            'findRegex': r'/\[status:[^\]]*\]/gi',
                            'replaceString': '',
                            'promptOnly': True,
                            'placement': [2],
                            'minDepth': 1,
                        },
                    ],
                },
            },
        })
        companion_db.create_companion('c_ctx_proc', {'name': 'ContextTest'}, raw_card_data=raw)
        comp = companion.Companion('c_ctx_proc')
        hist = [
            {'role': 'assistant', 'content': 'Old reply <think>secret</think> [status: tired]'},
            {'role': 'user', 'content': 'Hi'},
            {'role': 'assistant', 'content': 'Latest reply <think>hidden</think> [status: bright]'},
        ]
        built = comp._build_messages(hist, branch_id='main')
        # First AI reply has depth=2 (>=1): both <think> and [status: ...] are stripped for LLM.
        self.assertEqual(built[1]['content'].strip(), 'Old reply')
        # Latest AI reply has depth=0 (<1): <think> stripped by editprocess, [status: bright] preserved.
        self.assertEqual(built[3]['content'].strip(), 'Latest reply  [status: bright]')

    def test_standalone_risum_preserves_output_input_and_process_scripts(self):
        from backend import risum

        mod = {
            'name': 'FullScriptsMod',
            'regex': [
                {'type': 'editdisplay', 'comment': 'Disp', 'in': 'a', 'out': 'b', 'flag': 'g'},
                {'type': 'editoutput', 'comment': 'Out', 'in': 'x', 'out': 'y', 'flag': 'g'},
                {'type': 'editinput', 'comment': 'In', 'in': 'u', 'out': 'v', 'flag': 'g'},
                {'type': 'editprocess', 'comment': 'Proc', 'in': 'm', 'out': 'n', 'flag': 'g'},
            ],
        }
        risum_bytes = _build_test_risum_bytes(mod)
        companion_db.create_companion('c_risum_full', {'name': 'FullTest'})
        res = risum.merge_risum_into_companion('c_risum_full', risum_bytes)
        self.assertEqual(res['added_regex'], 4)
        pres = res['card_presentation']
        self.assertEqual(len(pres['display_regex']), 1)
        self.assertEqual(len(pres['output_regex']), 1)
        self.assertEqual(len(pres['input_regex']), 1)
        self.assertEqual(len(pres['prompt_regex']), 1)

    def test_hud_macros_in_cbs_and_pinned_vars_endpoint(self):
        companion_db.create_companion('c_hud_cbs', {'name': 'Luna'})
        companion_db.update_branch_hud(
            'c_hud_cbs',
            'main',
            enabled=True,
            hud={
                'mood': 'tender',
                'affection': 72,
                'location': 'rooftop_garden',
                '心跳理由': '记得约定',
            },
        )
        res = asyncio.run(api_routes.update_scene_rules(
            'c_hud_cbs',
            api_routes.SceneRulesUpdateRequest(
                hud_pinned_vars=['stamina', 'stamina', 'mood_tag'],
                scene_rules=[{
                    'id': 'hud_aff',
                    'name': '好感升温',
                    'trigger_type': 'variable',
                    'condition_cbs': '{{greater_equal::{{hud_affection}}::65}}',
                    'prompt': 'At {{hud_location}}, mood={{hud_mood}}, reason={{hud::心跳理由}}, expr={{? hud_affection >= 70}}',
                    'placement': 'system',
                    'enabled': True,
                }],
            ),
        ))
        self.assertEqual(res['card_presentation']['hud_pinned_vars'], ['stamina', 'mood_tag'])
        prompt = companion.Companion('c_hud_cbs').build_system_prompt(history=[])
        self.assertIn('At rooftop_garden, mood=tender, reason=记得约定, expr=1', prompt)


if __name__ == '__main__':
    unittest.main()
