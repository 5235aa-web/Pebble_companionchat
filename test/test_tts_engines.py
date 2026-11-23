# -*- coding: utf-8 -*-
"""TTS engine lifecycle, exercised without spawning audio.cpp.

The engines are singletons built on import, so the instances here are created
with object.__new__ and given just enough state to answer the questions under
test: is a stop idempotent, and does release() tell the truth when nothing runs.
"""

import threading
import unittest

from backend import audiocpp_tts_engine, dotstts_engine, fishaudio_engine, ming3tts_engine, omnivoice_engine

ENGINES = (
    (omnivoice_engine.OmniVoiceEngine, 'omnivoice'),
    (dotstts_engine.DotTtsEngine, 'dotstts'),
    (fishaudio_engine.FishAudioEngine, 'fishaudio'),
    (ming3tts_engine.Ming3TtsEngine, 'ming3tts'),
    (audiocpp_tts_engine.AudioCppTtsEngine, 'audiocpp_tts'),
)


def _idle_instance(cls):
    """A stopped engine: no child, nothing listening, no HTTP anywhere."""
    inst = object.__new__(cls)
    inst._lock = threading.RLock()
    inst._proc = None
    inst._ready = False
    inst._port_free = lambda: True
    inst._healthy = lambda: False
    return inst


class ReleaseTest(unittest.TestCase):
    def test_nothing_running_reports_not_running(self):
        for cls, name in ENGINES:
            with self.subTest(engine=name):
                result = _idle_instance(cls).release()
                self.assertFalse(result['released'])
                self.assertEqual(result['reason'], 'not_running')

    def test_release_never_raises(self):
        for cls, name in ENGINES:
            with self.subTest(engine=name):
                self.assertIn('released', _idle_instance(cls).release())


class CloseTest(unittest.TestCase):
    def test_close_is_idempotent(self):
        for cls, name in ENGINES:
            with self.subTest(engine=name):
                inst = _idle_instance(cls)
                inst.close()
                inst.close()

    def test_close_leaves_no_child_behind(self):
        for cls, name in ENGINES:
            with self.subTest(engine=name):
                inst = _idle_instance(cls)
                inst.close()
                self.assertIsNone(inst._proc)


class BaseUrlTest(unittest.TestCase):
    def test_base_url_is_loopback(self):
        for cls, name in ENGINES:
            with self.subTest(engine=name):
                url = _idle_instance(cls)._base_url()
                self.assertTrue(url.startswith('http://127.0.0.1:'), url)


if __name__ == '__main__':
    unittest.main()
