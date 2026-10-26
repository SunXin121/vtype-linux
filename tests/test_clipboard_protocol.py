"""按 wx-ime-sdk 的字段定义核对合成报文，不访问官方服务器。"""
import fcntl
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud'))
import 官方同步探针 as p
import io
from 剪贴板协议 import (OfficialClipboard, GroupSnapshot, NotSent, SendUnknown, ClipboardImage, cos_url,
                         download_image, download_text, image_cipher, image_reference)
import base64
import hashlib


class SyntheticProbe:
    def __init__(self, identity):
        self.identity = identity
        self.offset = 0
        self.calls = []
        self.sent_response = bytes.fromhex('08001006')
        self.group_response = p.blob(1, bytes.fromhex('08141202080a1202080b')) + p.number(2, 0)
        self.cos_response = (p.blob(4, 'https://bucket.cos.ap-shanghai.myqcloud.com/clip/get?sign=d')
                             + p.blob(5, 'https://bucket.cos.ap-shanghai.myqcloud.com/clip/put?sign=u') + p.number(6, 10240))

    def timestamp(self):
        return 1000000

    def request(self, path, body):
        return p.number(1, 1000000)

    def api(self, command, body, **kwargs):
        self.calls.append((command, body))
        return {34: self.group_response, 60: self.cos_response}.get(command, self.sent_response)


class ClipboardProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'identity.json').write_text(json.dumps({'uin': 10, 'server_key': 'synthetic'}))
        self.clients = []
        def factory(identity):
            client = SyntheticProbe(identity)
            self.clients.append(client)
            return client
        self.protocol = OfficialClipboard(self.root, probe_factory=factory)

    def tearDown(self):
        self.temp.cleanup()

    def test_group_query_and_text_send_match_reference_wire_fields(self):
        snapshot = self.protocol.fetch()
        self.assertEqual((snapshot.group, snapshot.own, snapshot.members), (20, 10, frozenset({10, 11})))
        self.assertEqual(self.clients[0].calls[0], (34, bytes.fromhex('080a100118032000')))
        self.assertEqual(self.protocol.send(snapshot, 'hi', 'a'), 6)
        self.assertEqual(self.clients[0].calls[-1], (36, bytes.fromhex('080a10011a091001320268694a01612014')))

    def test_receiving_reference_message_preserves_source_expiry_and_utf8(self):
        self.protocol.fetch()
        message = bytes.fromhex('0805100120d00f32026869380b4a0172')
        self.clients[0].group_response = p.blob(1, bytes.fromhex('08141202080a1202080b') + p.blob(3, message))
        snapshot = self.protocol.fetch()
        self.assertEqual((snapshot.message.version, snapshot.message.source, snapshot.message.expires,
                          snapshot.message.text, snapshot.message.trace, snapshot.now_ms), (5, 11, 2000, 'hi', 'r', 1000000))

    def test_undecodable_remote_message_is_ignored_instead_of_failing_every_poll(self):
        self.protocol.fetch()
        message = p.number(1, 5) + p.number(2, 1) + p.number(4, 2000) + p.blob(6, b'\xff\xfe') + p.number(7, 11) + p.blob(9, b'\xff')
        self.clients[0].group_response = p.blob(1, bytes.fromhex('08141202080a1202080b') + p.blob(3, message))
        snapshot = self.protocol.fetch()
        self.assertEqual((snapshot.message.text, snapshot.message.trace), ('', ''))

    def test_cos_text_link_is_kept_until_accepted_and_only_official_https_is_downloaded(self):
        self.protocol.fetch()
        url = 'https://bucket.cos.ap-shanghai.myqcloud.com/clip?sign=x'
        message = p.number(1, 5) + p.number(2, 2) + p.number(4, 2000) + p.blob(6, url) + p.number(7, 11) + p.blob(9, 'r')
        self.clients[0].group_response = p.blob(1, bytes.fromhex('08141202080a1202080b') + p.blob(3, message))
        snapshot = self.protocol.fetch()
        self.assertEqual((snapshot.message.kind, snapshot.message.text, snapshot.message.url), (2, '', url))
        for bad in ('http://bucket.myqcloud.com/a', 'https://evil.example/a', 'https://u:p@bucket.myqcloud.com/a',
                    'https://bucket.myqcloud.com:8443/a', 'https://bucket.myqcloud.com.evil.example/a'):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                cos_url(bad)
        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        self.assertEqual(download_text(url, opener=lambda *a, **k: Response('长文本'.encode())), '长文本')
        with self.assertRaises(ValueError):
            download_text(url, limit=4, opener=lambda *a, **k: Response(b'12345'))

    def test_image_reference_is_decrypted_verified_and_untrusted_fields_rejected(self):
        png = b'\x89PNG\r\n\x1a\n' + b'synthetic image' + b'IEND\xaeB`\x82'
        key = bytes(range(16))
        url = 'https://bucket.cos.ap-shanghai.myqcloud.com/clip/img?sign=x'
        raw = json.dumps({'version': 1, 'path': url, 'md5': hashlib.md5(png).hexdigest(),
                          'key': base64.b64encode(key).decode()})
        self.protocol.fetch()
        message = p.number(1, 5) + p.number(2, 6) + p.number(4, 2000) + p.blob(6, raw) + p.number(7, 11) + p.blob(9, 'r')
        self.clients[0].group_response = p.blob(1, bytes.fromhex('08141202080a1202080b') + p.blob(3, message))
        reference = self.protocol.fetch().message.image
        self.assertEqual((reference['url'], reference['key']), (url, key))
        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        cipher = image_cipher(key, png)
        self.assertNotEqual(cipher, png)
        self.assertEqual(download_image(reference, opener=lambda *a, **k: Response(cipher)), ClipboardImage(png, 'image/png'))
        self.assertIsNone(download_image(reference, opener=lambda *a, **k: Response(image_cipher(key, png + b'x'))))
        truncated = png[:-4]
        self.assertIsNone(download_image(dict(reference, md5=hashlib.md5(truncated).hexdigest()),
                                         opener=lambda *a, **k: Response(image_cipher(key, truncated))))
        gif = b'GIF89a' + b'x' * 8
        self.assertIsNone(download_image(dict(reference, md5=hashlib.md5(gif).hexdigest()),
                                         opener=lambda *a, **k: Response(image_cipher(key, gif))))
        bad = json.loads(raw)
        for change in ({'path': 'https://evil.example/a'}, {'version': 2}, {'key': 'AAAA'}, {'md5': 'zz' * 16}):
            with self.subTest(change=change):
                self.assertIsNone(image_reference(json.dumps(dict(bad, **change))))
        self.assertIsNone(image_reference(b'not json'))

    def test_image_send_uploads_ciphertext_then_publishes_reference(self):
        snapshot = self.protocol.fetch()
        png = ClipboardImage(b'\x89PNG\r\n\x1a\n' + b'synthetic image' + b'IEND\xaeB`\x82', 'image/png')
        uploads = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        def opener(request, timeout):
            uploads.append((request.get_method(), request.full_url, request.data))
            return Response()
        self.assertEqual(self.protocol.send_image(snapshot, png, 'trace', opener=opener), 6)
        commands = [command for command, _ in self.clients[0].calls]
        self.assertEqual(commands, [34, 60, 34, 36])
        credential = p.fields(self.clients[0].calls[1][1])
        self.assertEqual((p.one(credential, 1), p.one(credential, 3)), (6, 3))
        method, url, body = uploads[0]
        self.assertEqual((method, url), ('PUT', 'https://bucket.cos.ap-shanghai.myqcloud.com/clip/put?sign=u'))
        message = p.fields(p.one(p.fields(self.clients[0].calls[-1][1]), 3))
        self.assertEqual((p.one(message, 2), p.one(message, 9)), (6, b'trace'))
        reference = image_reference(p.one(message, 6))
        self.assertEqual(reference['url'], 'https://bucket.cos.ap-shanghai.myqcloud.com/clip/get?sign=d')
        self.assertEqual(image_cipher(reference['key'], body), png.data)
        self.assertEqual(reference['md5'], hashlib.md5(png.data).hexdigest())

    def test_image_upload_failure_and_invalid_image_are_not_sent(self):
        snapshot = self.protocol.fetch()
        png = ClipboardImage(b'\x89PNG\r\n\x1a\n' + b'x' + b'IEND\xaeB`\x82', 'image/png')
        def broken(request, timeout):
            raise OSError('synthetic')
        with self.assertRaises(NotSent):
            self.protocol.send_image(snapshot, png, 't', opener=broken)
        with self.assertRaises(NotSent):
            self.protocol.send_image(snapshot, ClipboardImage(b'GIF89a', 'image/png'), 't', opener=broken)
        self.clients[0].cos_response = self.clients[0].cos_response.replace(p.number(6, 10240), p.number(6, 0))
        with self.assertRaises(NotSent):
            self.protocol.send_image(snapshot, png, 't', opener=broken)
        self.assertFalse(any(command == 36 for command, _ in self.clients[0].calls))

    def test_identity_lock_is_released_before_network_requests(self):
        observed = []
        def factory(identity):
            client = SyntheticProbe(identity)
            original = client.api
            def api(*args, **kwargs):
                with (self.root / 'identity.lock').open('a+') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed.append(True)
                return original(*args, **kwargs)
            client.api = api
            return client
        protocol = OfficialClipboard(self.root, probe_factory=factory)
        snapshot = protocol.fetch()
        protocol.send(snapshot, 'hi', 'a')
        self.assertEqual(len(observed), 3)

    def test_held_identity_lock_does_not_start_a_request(self):
        with (self.root / 'identity.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.protocol.fetch()
        self.assertEqual(self.clients, [])

    def test_group_change_is_checked_before_sending_and_identity_rotation_is_reloaded(self):
        snapshot = self.protocol.fetch()
        self.clients[0].group_response = p.blob(1, bytes.fromhex('08151202080a1202080b'))
        with self.assertRaises(NotSent):
            self.protocol.send(snapshot, 'hi', 'a')
        self.assertFalse(any(command == 36 for command, _ in self.clients[0].calls))
        (self.root / 'identity.json').write_text(json.dumps({'uin': 10, 'server_key': 'rotated'}))
        self.protocol.fetch()
        self.assertEqual(len(self.clients), 2)

    def test_missing_send_version_is_unknown(self):
        snapshot = self.protocol.fetch()
        self.clients[0].sent_response = bytes.fromhex('0800')
        with self.assertRaises(SendUnknown):
            self.protocol.send(snapshot, 'hi', 'a')

    def test_malformed_send_confirmation_is_unknown_not_safe_to_retry(self):
        snapshot = self.protocol.fetch()
        self.clients[0].sent_response = p.blob(2, b'invalid version')
        with self.assertRaises(SendUnknown):
            self.protocol.send(snapshot, 'hi', 'a')

    def test_clipboard_errors_never_save_raw_response(self):
        client = object.__new__(p.Probe)
        client.identity = {'uin': 10, 'device_name': 'synthetic'}
        client.offset = 0
        client.request = lambda *args: p.blob(6, b'[synthetic private clipboard]')
        with patch.object(p, 'save_private') as save:
            with self.assertRaises(ValueError) as caught:
                client.api(34, b'', persist_errors=False)
            save.assert_not_called()
        self.assertNotIn('synthetic private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
