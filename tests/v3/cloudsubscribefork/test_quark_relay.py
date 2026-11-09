"""quark-relay 端到端测试：假 MP 取链接口 + 假夸克直链（要求 Cookie、支持 Range）。"""

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location('quark_relay_under_test', ROOT / 'tools' / 'quark-relay' / 'relay.py')
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

_strm_spec = importlib.util.spec_from_file_location(
    'strm_for_relay_test', ROOT / 'plugins.v3' / 'cloudsubscribefork' / 'utils' / 'strm.py')
strm = importlib.util.module_from_spec(_strm_spec)
_strm_spec.loader.exec_module(strm)

SECRET = 'test-secret-0123456789'
CONTENT = bytes(range(256)) * 4096 + b'tail'  # ~1MB
APIKEY = 'mp-token'


def _serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FakeQuark(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        if self.headers.get('Cookie') != 'kps=abc':
            self.send_response(403); self.send_header('Content-Length', '0'); self.end_headers(); return
        start, end = self.headers['Range'][6:].split('-')
        start = int(start)
        end = min(int(end) if end else len(CONTENT) - 1, len(CONTENT) - 1)
        body = CONTENT[start:end + 1]
        self.send_response(206)
        self.send_header('Content-Type', 'video/x-matroska')
        self.send_header('Content-Range', f'bytes {start}-{end}/{len(CONTENT)}')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeMP(BaseHTTPRequestHandler):
    calls = []
    quark_url = ''

    def do_GET(self):
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        type(self).calls.append(query)
        ok = query.get('apikey') == APIKEY and query.get('sig') == strm.StrmGenerator.relay_signature(
            SECRET, query.get('drive'), query.get('file_id'))
        payload = {'success': True, 'data': {'url': self.quark_url, 'headers': {'Cookie': 'kps=abc', 'Range': 'x'}}} \
            if ok else {'success': False, 'message': 'denied'}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class QuarkRelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.quark = _serve(FakeQuark)
        FakeMP.quark_url = f'http://127.0.0.1:{cls.quark.server_address[1]}/file.mkv'
        cls.mp = _serve(FakeMP)
        config = relay.RelayConfig({
            'RELAY_SECRET': SECRET, 'MP_URL': f'http://127.0.0.1:{cls.mp.server_address[1]}',
            'MP_APIKEY': APIKEY, 'LISTEN_HOST': '127.0.0.1', 'LISTEN_PORT': '0',
            'PATH_PREFIX': '/q', 'CHUNK_MB': '1',
        })
        config.chunk = 100_000  # 小分段，覆盖多段拼接
        cls.relay = relay.build_server(config)
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.relay.server_address[1]}/q'

    @classmethod
    def tearDownClass(cls):
        for server in (cls.relay, cls.mp, cls.quark):
            server.shutdown()
            server.server_close()

    def url(self, file_id='fid1', sig=None):
        sig = sig or strm.StrmGenerator.relay_signature(SECRET, 'quark', file_id)
        return f'{self.base}/quark/{file_id}/{sig}?/电影.mkv'.replace('电影', '%E7%94%B5%E5%BD%B1')

    def test_signature_matches_plugin(self):
        self.assertEqual(relay.signature(SECRET, 'quark', 'fid1'),
                         strm.StrmGenerator.relay_signature(SECRET, 'quark', 'fid1'))

    def test_strm_template_points_to_relay(self):
        template = strm.StrmGenerator.relay_template('quark', 'file_id')
        generator = strm.StrmGenerator('https://mp.example.com/q', template,
                                       provider_variables={'file_id', 'relay_sig'})
        sig = strm.StrmGenerator.relay_signature(SECRET, 'quark', 'fid1')
        content = generator.render('a.mkv', '/a.mkv', {'file_id': 'fid1', 'relay_sig': sig})
        self.assertEqual(content, f'https://mp.example.com/q/quark/fid1/{sig}?/a.mkv')
        self.assertEqual(relay.parse_target(urlparse(content).path + '?/a.mkv', '/q'), ('quark', 'fid1', sig))

    def test_full_file(self):
        with urlopen(self.url()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Content-Length'], str(len(CONTENT)))
            self.assertEqual(response.read(), CONTENT)

    def test_range_seek(self):
        request = Request(self.url(), headers={'Range': 'bytes=250000-450001'})
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Range'], f'bytes 250000-450001/{len(CONTENT)}')
            self.assertEqual(response.read(), CONTENT[250000:450002])

    def test_suffix_range(self):
        request = Request(self.url(), headers={'Range': 'bytes=-4'})
        with urlopen(request) as response:
            self.assertEqual(response.read(), b'tail')

    def test_head(self):
        with urlopen(Request(self.url(), method='HEAD')) as response:
            self.assertEqual(response.headers['Content-Length'], str(len(CONTENT)))
            self.assertEqual(response.headers['Accept-Ranges'], 'bytes')

    def test_bad_signature_is_rejected_without_calling_mp(self):
        before = len(FakeMP.calls)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url(sig='0' * 32))
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(len(FakeMP.calls), before)

    def test_link_is_cached(self):
        urlopen(self.url('fid-cache')).read()
        urlopen(self.url('fid-cache')).read()
        self.assertEqual(sum(1 for c in FakeMP.calls if c.get('file_id') == 'fid-cache'), 1)


if __name__ == '__main__':
    unittest.main()
