# -*- coding: utf-8 -*-
"""Tests for user-configurable local LLM, TTS, and ASR models and paths."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import (
    audio_specs,
    audiocpp_tts_engine,
    llama_server_engine as llama,
    llm_client,
    omnivoice_engine,
    paths,
    stt_registry,
    tts_service,
    ws_bridge,
    ws_call,
)


class CustomPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-custom-paths-'))
        self._app_cfg = paths.APP_CONFIG_PATH
        self._env_llama = os.environ.pop('LLAMA_SERVER_DIR', None)
        paths.APP_CONFIG_PATH = self.tmp / 'app.json'

    def tearDown(self):
        paths.APP_CONFIG_PATH = self._app_cfg
        os.environ.pop('LLAMA_SERVER_DIR', None)
        if self._env_llama is not None:
            os.environ['LLAMA_SERVER_DIR'] = self._env_llama
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_user_path_handles_relative_and_absolute(self):
        paths.save_app_config({'modelsRoot': str(self.tmp / 'models')})
        rel = paths.resolve_user_path('qwen/qwen3.gguf')
        self.assertEqual(rel, self.tmp / 'models' / 'qwen' / 'qwen3.gguf')
        abs_target = (self.tmp / 'elsewhere' / 'm.gguf').resolve()
        self.assertEqual(paths.resolve_user_path(str(abs_target)), abs_target)
        self.assertIsNone(paths.resolve_user_path('   '))

    def test_llama_dir_reads_app_config_dir_or_exe(self):
        runtime_dir = self.tmp / 'llama-bin'
        runtime_dir.mkdir()
        exe = runtime_dir / 'llama-server.exe'
        exe.write_bytes(b'MZ')

        paths.save_app_config({'llamaRoot': str(runtime_dir)})
        self.assertEqual(paths.llama_dir(), runtime_dir)

        paths.save_app_config({'llamaRoot': str(exe)})
        self.assertEqual(paths.llama_dir(), runtime_dir)


class AudioSpecsTest(unittest.TestCase):
    def test_tts_and_asr_specs_are_discovered(self):
        tts = {s['family']: s for s in audio_specs.list_specs('tts')}
        asr = {s['family']: s for s in audio_specs.list_specs('asr')}
        self.assertIn('omnivoice', tts)
        self.assertIn('kokoro_tts', tts)
        self.assertIn('cosyvoice3', tts)
        self.assertIn('qwen3_asr', asr)
        self.assertIn('sense_asr', asr)

    def test_audiocpp_tts_config_respects_spec_mode_and_cpu_backend(self):
        cfg = audiocpp_tts_engine.AudioCppTtsEngine._build_server_config(
            'cosyvoice3',
            Path('D:/models/cosyvoice3.gguf'),
            compute_backend='cpu',
        )
        self.assertEqual(cfg['backend'], 'cpu')
        self.assertEqual(cfg['models'][0]['family'], 'cosyvoice3')
        self.assertEqual(cfg['models'][0]['mode'], 'offline')


class CustomLocalLlmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-custom-llm-'))
        self._app_cfg = paths.APP_CONFIG_PATH
        self._local_cfg = llama.LOCAL_LLM_CONFIG_PATH
        paths.APP_CONFIG_PATH = self.tmp / 'app.json'
        llama.LOCAL_LLM_CONFIG_PATH = self.tmp / 'local_llm_models.json'

    def tearDown(self):
        paths.APP_CONFIG_PATH = self._app_cfg
        llama.LOCAL_LLM_CONFIG_PATH = self._local_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upsert_build_args_and_delete_custom_model(self):
        model_file = self.tmp / 'qwen3-8b.gguf'
        mmproj_file = self.tmp / 'mmproj-qwen3.gguf'
        model_file.write_bytes(b'gguf')
        mmproj_file.write_bytes(b'mmproj')

        llama.upsert_custom_model({
            'key': 'qwen3-8b',
            'label': 'Qwen3 8B',
            'model': str(model_file),
            'mmproj': str(mmproj_file),
            'ctx': 65536,
            'device': 'cpu',
            'kv_type': 'f16',
            'flash_attn': False,
            'jinja': False,
        })

        described = {m['key']: m for m in llama.describe_models()}
        self.assertIn('qwen3-8b', described)
        self.assertTrue(described['qwen3-8b']['available'])
        self.assertTrue(described['qwen3-8b']['has_mmproj'])

        args = llama.build_args('qwen3-8b')
        self.assertNotIn('--device', args)
        self.assertEqual(args[args.index('-ngl') + 1], '0')
        self.assertEqual(args[args.index('-c') + 1], '65536')
        self.assertEqual(args[args.index('-fa') + 1], 'off')
        self.assertEqual(args[args.index('-ctk') + 1], 'f16')
        self.assertNotIn('--jinja', args)
        self.assertIn('--mmproj', args)
        self.assertNotIn('--mmproj-device', args)

        self.assertTrue(llama.delete_custom_model('qwen3-8b'))
        self.assertNotIn('qwen3-8b', {m['key'] for m in llama.describe_models()})

    def test_scan_gguf_models_pairs_mmproj_and_skips_aux_files(self):
        sub = self.tmp / 'models' / 'llama3'
        sub.mkdir(parents=True)
        (sub / 'llama3-8b-q4.gguf').write_bytes(b'1234')
        (sub / 'mmproj-llama3.gguf').write_bytes(b'5678')
        (sub / 'mtp-draft.gguf').write_bytes(b'90')

        found = llama.scan_gguf_models(self.tmp / 'models')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['label'], 'llama3-8b-q4')
        self.assertTrue(found[0]['mmproj'].endswith('mmproj-llama3.gguf'))
        self.assertTrue(found[0]['mtp'].endswith('mtp-draft.gguf'))

    def test_changing_models_root_updates_builtin_paths_dynamically(self):
        root_a = self.tmp / 'root-a'
        root_b = self.tmp / 'root-b'
        root_a.mkdir()
        root_b.mkdir()
        paths.save_app_config({'modelsRoot': str(root_a)})
        a = {m['key']: m['path'] for m in llama.describe_models()}
        self.assertTrue(a['gemma-4-12b-heretic'].startswith(str(root_a)))

        paths.save_app_config({'modelsRoot': str(root_b)})
        b = {m['key']: m['path'] for m in llama.describe_models()}
        self.assertTrue(b['gemma-4-12b-heretic'].startswith(str(root_b)))


class CustomSttRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-custom-stt-'))
        self._stt_cfg = stt_registry.CONFIG_FILE
        self._stt_path = paths.STT_PROVIDERS_PATH
        self._custom_path = stt_registry.STT_CUSTOM_PATH
        stt_registry.CONFIG_FILE = str(self.tmp / 'stt_providers.json')
        paths.STT_PROVIDERS_PATH = self.tmp / 'stt_providers.json'
        stt_registry.STT_CUSTOM_PATH = self.tmp / 'stt_custom.json'

    def tearDown(self):
        stt_registry.CONFIG_FILE = self._stt_cfg
        paths.STT_PROVIDERS_PATH = self._stt_path
        stt_registry.STT_CUSTOM_PATH = self._custom_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_switch_and_delete_custom_http_and_audiocpp_stt(self):
        reg = stt_registry.STTRegistry()
        http_id = reg.upsert_engine({
            'id': 'local_whisper',
            'label': 'Whisper HTTP',
            'kind': 'openai_stt',
            'baseUrl': 'http://127.0.0.1:8081/v1',
            'model': 'whisper-large-v3',
        })
        reg.set_active(http_id)
        eng = reg.get_engine()
        self.assertIsNotNone(eng)
        self.assertEqual(eng.MODEL_ID, 'whisper-large-v3')

        gguf = self.tmp / 'sense.gguf'
        gguf.write_bytes(b'gguf')
        ac_id = reg.upsert_engine({
            'id': 'sense_local',
            'label': 'SenseVoice',
            'kind': 'audiocpp',
            'family': 'sense_asr',
            'modelPath': str(gguf),
            'computeBackend': 'cpu',
        })
        status = {e['id']: e for e in reg.status()['engines']}
        self.assertIn(ac_id, status)
        self.assertTrue(status[ac_id]['modelExists'])
        self.assertEqual(status[ac_id]['computeBackend'], 'cpu')

        # Reloading from disk keeps custom engines
        reg2 = stt_registry.STTRegistry()
        self.assertEqual(reg2.active_id, http_id)
        self.assertIn(ac_id, {e['id'] for e in reg2.status()['engines']})

        # Deleting active custom engine falls back to default
        reg2.delete_engine(http_id)
        self.assertEqual(reg2.active_id, stt_registry.DEFAULT_ENGINE_ID)


class LlmProviderWhitelistTest(unittest.TestCase):
    def test_local_openai_providers_supported(self):
        for p in ('ollama', 'lmstudio', 'local-openai'):
            self.assertIn(p, llm_client.SUPPORTED_PROVIDERS)


class TtsStreamingAndAlignmentTest(unittest.TestCase):
    def test_omnivoice_stream_payload_places_chunk_size_in_options(self):
        payload = omnivoice_engine._omnivoice._stream_payload(
            'hello', voice='mayathesecond', instruct='', speed=1.0, num_steps=16, chunk_size=120,
        )
        self.assertNotIn('text_chunk_size', payload)
        self.assertEqual(payload['options']['text_chunk_size'], 120)
        self.assertEqual(payload['options']['num_inference_steps'], 16)

    def test_audiocpp_tts_in_stream_sample_rates_and_resolves_per_family(self):
        self.assertIn('audiocpp_tts', tts_service.STREAM_SAMPLE_RATES)

        kokoro_req = tts_service.TtsRequest(
            text='hi', provider='audiocpp_tts', audiocpp_family='kokoro_tts',
        )
        self.assertEqual(tts_service._resolve_stream_sample_rate(kokoro_req), 24000)

        ming_req = tts_service.TtsRequest(
            text='hi', provider='audiocpp_tts', audiocpp_family='ming3_tts',
        )
        self.assertEqual(tts_service._resolve_stream_sample_rate(ming_req), 44100)

        override_req = tts_service.TtsRequest(
            text='hi', provider='audiocpp_tts', audiocpp_family='kokoro_tts', sample_rate=32000,
        )
        self.assertEqual(tts_service._resolve_stream_sample_rate(override_req), 32000)

        offline_req = tts_service.TtsRequest(
            text='hi', provider='audiocpp_tts', audiocpp_family='cosyvoice3',
        )
        self.assertIsNone(tts_service._resolve_stream_sample_rate(offline_req))

    def test_aligned_pcm_buffers_odd_trailing_bytes(self):
        import asyncio

        async def raw_gen():
            yield b'\x01\x02\x03'
            yield b'\x04'
            yield b'\x05\x06\x07'
            yield b'\x08'

        async def collect():
            return [c async for c in tts_service._aligned_pcm(raw_gen())]

        out = asyncio.run(collect())
        for chunk in out:
            self.assertEqual(len(chunk) % 2, 0)
        self.assertEqual(b''.join(out), b'\x01\x02\x03\x04\x05\x06\x07\x08')

    def test_audiocpp_build_stream_payload(self):
        payload = audiocpp_tts_engine.AudioCppTtsEngine._build_payload(
            'hello',
            family='kokoro_tts',
            voice='af_heart',
            speed=1.1,
            options={'voice_id': 'af_heart', 'unknown_key': 'drop'},
            response_format='pcm',
            stream=True,
        )
        self.assertEqual(payload['response_format'], 'pcm')
        self.assertEqual(payload['stream_format'], 'audio')
        self.assertEqual(payload['voice'], 'af_heart')
        self.assertNotIn('unknown_key', payload.get('options', {}))


class VoiceCallImprovementsTest(unittest.TestCase):
    def test_stage_direction_filter_optional(self):
        raw = '(sigh) *smiles warmly* Hello there.'
        self.assertEqual(tts_service._filter_tts_text(raw), '*smiles warmly* Hello there.')
        self.assertEqual(
            tts_service._filter_tts_text(raw, strip_stage_directions=True),
            'Hello there.',
        )

    def test_find_quiet_split_picks_silent_window_aligned(self):
        import struct

        loud = struct.pack('<1600h', *([12000, -12000] * 800))   # 0.1s loud
        quiet = struct.pack('<4800h', *([10] * 4800))            # 0.3s quiet
        pcm = loud * 10 + quiet + loud * 10
        split = ws_call._find_quiet_split(pcm, len(loud) * 5, len(loud) * 15)
        self.assertEqual(split % 2, 0)
        self.assertGreaterEqual(split, len(loud) * 10)
        self.assertLessEqual(split, len(loud) * 10 + len(quiet))

    def test_call_session_commits_oversized_prefix(self):
        import asyncio

        class FakeWs:
            def __init__(self):
                self.sent = []

            async def send_json(self, payload):
                self.sent.append(payload)

        class FakeEngine:
            def __init__(self):
                self.calls = 0

            def transcribe_bytes(self, wav, sr):
                self.calls += 1
                return 'prefix words' if self.calls == 1 else 'tail words'

        ws = FakeWs()
        eng = FakeEngine()
        sess = ws_call.CallSession(ws, eng)
        # Feed 21 seconds of silence (> SEGMENT_THRESHOLD_SECONDS=20s)
        sess.pcm.extend(b'\x00' * (ws_call.BYTES_PER_SECOND * 21))
        asyncio.run(sess._transcribe_once())
        self.assertGreater(sess.committed_bytes, 0)
        self.assertEqual(sess.committed_parts, ['prefix words'])
        self.assertEqual(sess.accum, 'prefix words tail words')
        self.assertLess(len(sess.pcm), ws_call.BYTES_PER_SECOND * 10)

    def test_ws_bridge_cancel_stops_inflight_stream(self):
        import asyncio
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse

        app = FastAPI()
        started = asyncio.Event()
        cancelled_seen = asyncio.Event()

        @app.post('/slow-stream')
        async def slow_stream():
            async def gen():
                started.set()
                try:
                    while True:
                        yield b'chunk'
                        await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    cancelled_seen.set()
                    raise

            return StreamingResponse(gen(), media_type='application/octet-stream')

        bridge = ws_bridge.WsBridge(app)

        class FakeWs:
            def __init__(self):
                self.client = ('127.0.0.1', 0)
                self.headers = {}
                self.sent = []
                self._inbox = asyncio.Queue()

            async def accept(self):
                pass

            async def send_json(self, payload):
                self.sent.append(payload)

            async def receive_text(self):
                item = await self._inbox.get()
                if item is None:
                    from fastapi import WebSocketDisconnect
                    raise WebSocketDisconnect()
                return item

        async def scenario():
            fws = FakeWs()
            await fws._inbox.put(json.dumps({'id': 7, 'method': 'POST', 'path': '/slow-stream'}))
            runner = asyncio.create_task(bridge.handle(fws))
            await asyncio.wait_for(started.wait(), timeout=2.0)
            await fws._inbox.put(json.dumps({'id': 7, 'type': 'cancel'}))
            await asyncio.wait_for(cancelled_seen.wait(), timeout=2.0)
            await fws._inbox.put(None)
            await asyncio.wait_for(runner, timeout=2.0)
            return fws.sent

        sent = asyncio.run(scenario())
        self.assertFalse(any(m.get('type') == 'end' and m.get('id') == 7 for m in sent))


if __name__ == '__main__':
    unittest.main()
