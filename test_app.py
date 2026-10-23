import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

import app


class CaptureEvents:
    def __init__(self):
        self.messages = []

    def emit(self, value):
        self.messages.append(value)


class Tests(unittest.TestCase):
    def test_resume_and_chronology(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            app.write_json(folder / 'meeting.json', {'title': 'Тест', 'started': '2026-10-22', 'offsets': {'microphone': 0.05, 'system': 0.1}})
            for name in ['microphone-00000', 'microphone-00001', 'system-00000']:
                with wave.open(str(folder / (name + '.wav')), 'wb') as wav:
                    wav.setparams((1, 2, 8000, 0, 'NONE', 'none'))
                    wav.writeframes(b'\0\0' * 8000)
            events = type('Events', (), {name: CaptureEvents() for name in ['status', 'done', 'failed']})()
            response = type('Response', (), {'ok': True, 'json': lambda self: {'text': 'Реплика'}})()
            settings = {'model': 'test', 'language': 'ru'}
            with patch('app.router_request', return_value=response) as post:
                app.transcribe(folder, settings, 'fake-test-key', events)
                self.assertEqual(post.call_count, 3)
                app.transcribe(folder, settings, 'fake-test-key', events)
                self.assertEqual(post.call_count, 3)
                self.assertEqual(post.call_args.kwargs['json']['input_audio']['format'], 'wav')
            self.assertFalse(events.failed.messages)
            text = (folder / 'Расшифровка.md').read_text('utf-8')
            self.assertLess(text.index('Звук компьютера'), text.index('00:00:01'))

    def test_failure_preserves_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            app.write_json(folder / 'meeting.json', {'title': 'Тест', 'started': 'today'})
            with wave.open(str(folder / 'microphone-00000.wav'), 'wb') as wav:
                wav.setparams((1, 2, 8000, 0, 'NONE', 'none'))
                wav.writeframes(b'\0\0' * 8000)
            events = type('Events', (), {name: CaptureEvents() for name in ['status', 'done', 'failed']})()
            response = type('Response', (), {'ok': False, 'status_code': 401})()
            with patch('app.router_request', return_value=response):
                app.transcribe(folder, {'model': 'test'}, 'fake', events)
            self.assertIn('401', events.failed.messages[0])
            self.assertTrue((folder / 'microphone-00000.wav').exists())
            self.assertFalse((folder / 'Расшифровка.md').exists())

    def test_callback_chunk_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            track = app.Track.__new__(app.Track)
            track.folder, track.source = Path(temp), 'system'
            track.rate, track.channels = 10, 1
            track.index, track.frames, track.writer, track.error = 0, 0, None, None
            track.callback(b'\x00\x00' * 600, 600, None, 0)
            self.assertEqual(track.index, 1)
            with wave.open(str(Path(temp) / 'system-00000.wav')) as wav:
                self.assertEqual(wav.getnframes(), 600)

    def test_inherited_proxy_is_ignored(self):
        import requests
        response = requests.Response()
        response.status_code = 200
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://127.0.0.1:1',
                                      'HTTP_PROXY': 'http://127.0.0.1:1',
                                      'REQUESTS_CA_BUNDLE': 'nonexistent-certificate-file'}):
            with patch('requests.Session.send', return_value=response) as send:
                app.router_request('GET', '/models', timeout=15)
                self.assertFalse(send.call_args.kwargs['proxies'])
                self.assertTrue(send.call_args.kwargs['verify'])

    def test_network_error_messages(self):
        import requests
        self.assertIn('сертификат', app.connection_error(requests.exceptions.SSLError()))
        self.assertIn('5 минут', app.connection_error(requests.exceptions.ReadTimeout()))
        self.assertIn('15 секунд', app.connection_error(requests.exceptions.ConnectTimeout()))


if __name__ == '__main__':
    unittest.main()
