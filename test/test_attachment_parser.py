# -*- coding: utf-8 -*-
"""Attachment classification and extraction (images, text, documents)."""

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import attachment_parser as ap
from backend import paths


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

    def test_videos_are_classified(self):
        for name in ('clip.mp4', 'movie.MOV', 'stream.mkv', 'rec.webm'):
            self.assertEqual(ap.classify(name), 'video', name)

    def test_unknown_extensions_are_rejected(self):
        for name in ('a.exe', 'b', 'c.tar.gz'):
            self.assertIsNone(ap.classify(name), name)

    def test_supported_extensions_lists_all_groups(self):
        groups = ap.supported_extensions()
        self.assertIn('png', groups['image'])
        self.assertIn('mp4', groups['video'])
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


class VideoAttachmentTest(unittest.TestCase):
    def _frame_data_url(self) -> str:
        raw = _png_bytes((4, 4), (20, 120, 220))
        return 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')

    def test_video_with_frontend_frames_and_stt(self):
        frames_json = json.dumps([
            {'time': 0.2, 'data_url': self._frame_data_url()},
            {'time': 2.5, 'data_url': self._frame_data_url()},
        ])
        item = ap.build_attachment(
            'demo.mp4',
            b'fake-mp4-bytes',
            frames_json=frames_json,
            duration=5.0,
            width=1280,
            height=720,
            wav_bytes=b'RIFF' + b'\x00' * 60,
            enable_stt=True,
            stt_fn=lambda _wav: '你好，这是一段测试视频',
        )
        self.addCleanup(ap.delete_image, item['url'])
        self.addCleanup(ap.delete_video_assets, item)

        self.assertEqual(item['kind'], 'video')
        self.assertTrue(item['url'].startswith('/uploads/attachments/'))
        self.assertTrue(item['poster_url'].startswith('/uploads/attachments/'))
        self.assertEqual(len(item['frames']), 2)
        self.assertEqual(item['duration'], 5.0)
        self.assertEqual(item['width'], 1280)
        self.assertEqual(item['height'], 720)
        self.assertEqual(item['transcript'], '你好，这是一段测试视频')

        prompt_block = ap.format_video_for_prompt(item)
        self.assertIn('demo.mp4', prompt_block)
        self.assertIn('5.0s', prompt_block)
        self.assertIn('1280x720', prompt_block)
        self.assertIn('00:00, 00:02', prompt_block)
        self.assertIn('你好，这是一段测试视频', prompt_block)

        removed = ap.delete_video_assets(item)
        self.assertEqual(removed, 2)

    def test_video_without_frames_or_ffmpeg_fails_with_fallback_error(self):
        with mock.patch.object(ap, 'is_ffmpeg_usable', return_value=False):
            with self.assertRaises(ap.FfmpegFallbackError):
                ap.build_attachment('clip.mkv', b'fake-mkv-bytes')

    def test_video_prefers_ffmpeg_when_usable(self):
        fake_ff = Path('C:/tools/ffmpeg.exe')
        with (
            mock.patch.object(ap, 'is_ffmpeg_usable', return_value=True),
            mock.patch.object(ap, 'ffmpeg_path', return_value=fake_ff),
            mock.patch.object(ap, 'probe_video_ffmpeg', return_value=(6.0, 640, 360)),
            mock.patch.object(
                ap,
                'extract_frames_ffmpeg',
                return_value=[{'time': 0.2, 'url': '/uploads/attachments/vf_mock.jpg'}],
            ),
            mock.patch.object(ap, 'extract_audio_wav_ffmpeg', return_value=b'RIFF' + b'\x00' * 50),
        ):
            item = ap.build_attachment(
                'clip.mov',
                b'fake-mov-bytes',
                enable_stt=True,
                stt_fn=lambda _wav: 'fallback audio',
            )
            self.addCleanup(ap.delete_image, item['url'])

        self.assertEqual(item['kind'], 'video')
        self.assertEqual(item['duration'], 6.0)
        self.assertEqual(item['width'], 640)
        self.assertEqual(item['height'], 360)
        self.assertEqual(item['poster_url'], '/uploads/attachments/vf_mock.jpg')
        self.assertEqual(item['transcript'], 'fallback audio')

    def test_video_skips_stt_by_default(self):
        stt_mock = mock.Mock(return_value='should not run')
        frames_json = json.dumps([{'time': 0.1, 'data_url': self._frame_data_url()}])
        with mock.patch.object(ap, 'is_ffmpeg_usable', return_value=False):
            item = ap.build_attachment(
                'silent.mp4',
                b'fake-mp4-bytes',
                frames_json=frames_json,
                wav_bytes=b'RIFF' + b'\x00' * 60,
                stt_fn=stt_mock,
            )
        self.addCleanup(ap.delete_image, item['url'])
        self.addCleanup(ap.delete_video_assets, item)

        stt_mock.assert_not_called()
        self.assertEqual(item['transcript'], '')
        self.assertFalse(item['stt_enabled'])
        prompt_block = ap.format_video_for_prompt(item)
        self.assertIn('未附带音频转录', prompt_block)

    def test_transcribe_stored_video_on_demand(self):
        frames_json = json.dumps([{'time': 0.1, 'data_url': self._frame_data_url()}])
        with mock.patch.object(ap, 'is_ffmpeg_usable', return_value=False):
            item = ap.build_attachment(
                'clip.mp4',
                b'fake-mp4-bytes',
                frames_json=frames_json,
            )
        self.addCleanup(ap.delete_image, item['url'])
        self.addCleanup(ap.delete_video_assets, item)

        text = ap.transcribe_stored_video(
            item['url'],
            wav_bytes=b'RIFF' + b'\x00' * 60,
            stt_fn=lambda _wav: '按需识别出的台词',
        )
        self.assertEqual(text, '按需识别出的台词')

    def test_video_ffmpeg_runtime_failure_raises_fallback_error(self):
        fake_ff = Path('C:/tools/ffmpeg.exe')
        with (
            mock.patch.object(ap, 'is_ffmpeg_usable', return_value=True),
            mock.patch.object(ap, 'ffmpeg_path', return_value=fake_ff),
            mock.patch.object(ap, 'probe_video_ffmpeg', return_value=(0.0, 0, 0)),
            mock.patch.object(ap, 'extract_frames_ffmpeg', return_value=[]),
            mock.patch.object(ap, 'extract_audio_wav_ffmpeg', return_value=None),
        ):
            with self.assertRaises(ap.FfmpegFallbackError):
                ap.build_attachment('broken.mp4', b'fake-mp4-bytes')

    def test_ffmpeg_path_resolves_env_and_dir_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = Path(tmpdir) / 'bin'
            bin_dir.mkdir()
            exe = bin_dir / paths.ffmpeg_exe_name()
            exe.write_bytes(b'exe')
            with mock.patch.dict(os.environ, {'PEBBLE_FFMPEG_PATH': tmpdir}):
                resolved = paths.ffmpeg_path()
            self.assertEqual(resolved, exe)

    def test_continuous_video_sampling_density_and_prompt(self):
        ts_short = ap._sample_timestamps(5.0)
        ts_mid = ap._sample_timestamps(35.13)
        ts_long = ap._sample_timestamps(180.0)
        self.assertEqual(len(ts_short), 20)
        self.assertEqual(len(ts_mid), 90)
        self.assertEqual(len(ts_long), ap.MAX_VIDEO_FRAMES)
        self.assertLessEqual(len(ts_short), len(ts_mid))

        dense_frames = [
            {'time': t, 'url': f'/uploads/attachments/vf_{i}.jpg'}
            for i, t in enumerate(ts_mid)
        ]
        prompt_block = ap.format_video_for_prompt({
            'kind': 'video',
            'name': 'dance.mp4',
            'duration': 35.13,
            'width': 540,
            'height': 960,
            'frames': dense_frames,
            'transcript': '',
        })
        self.assertIn('90 帧视频画面流', prompt_block)
        self.assertIn('连续播放的动态视频流', prompt_block)
        self.assertIn('00:00.2 ~ 00:35.0', prompt_block)


if __name__ == '__main__':
    unittest.main()
