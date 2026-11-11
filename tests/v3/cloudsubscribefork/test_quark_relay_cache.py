"""quark-relay 1.1：内网取链、按过期时间缓存、403/410 续签、预取、DEBUG 逐请求日志（全离线）。"""

import importlib.util
import io
import json
import logging
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location('quark_relay_cache_under_test', ROOT / 'tools' / 'quark-relay' / 'relay.py')
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

SECRET = 'test-secret-0123456789'
APIKEY = 'mp-token-SHOULD-NOT-LEAK'
COOKIE = 'kps=COOKIE-SHOULD-NOT-LEAK'
CONTENT = bytes(range(256)) * 2048  # 512KB


def _serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(*servers):
    threads = [threading.Thread(target=server.shutdown) for server in servers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for server in servers:
        server.server_close()


def make_quark():
    """假夸克：只认当前这一代签名（gen），可模拟播放途中直链过期。"""

    class FakeQuark(BaseHTTPRequestHandler):
        valid_gen = 1
        hits = 0
        expire_after_hits = None  # 第 N 次请求后让当前直链失效
        expired_status = 403
        reject_all = False

        def do_GET(self):
            cls = type(self)
            cls.hits += 1
            gen = int(parse_qs(urlparse(self.path).query).get('gen', ['0'])[0])
            if cls.expire_after_hits is not None and cls.hits > cls.expire_after_hits and gen == cls.valid_gen:
                cls.valid_gen += 1
                cls.expire_after_hits = None
            if self.headers.get('Cookie') != COOKIE or gen != cls.valid_gen or cls.reject_all:
                self.send_response(cls.expired_status)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
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

    return FakeQuark


def make_mp(quark_cls, quark_port, delay=0.0, expires_in=None):
    """假 MP /relay/link：每次返回当前一代的直链（gen），可加延迟模拟公网绕行。"""

    class FakeMP(BaseHTTPRequestHandler):
        calls = []

        def do_GET(self):
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            type(self).calls.append(query)
            if delay:
                time.sleep(delay)
            url = f'http://127.0.0.1:{quark_port}/file.mkv?gen={quark_cls.valid_gen}'
            if expires_in is not None:
                url += f'&Expires={int(time.time() + expires_in)}&Signature=abc'
            ok = query.get('apikey') == APIKEY
            payload = {'success': True, 'data': {'url': url, 'headers': {'Cookie': COOKIE}}} \
                if ok else {'success': False, 'message': 'denied'}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return FakeMP


class RelayEnv:
    """一套假夸克 + 公网 MP + 内网 MP + 中转。"""

    def __init__(self, public_delay=0.0, internal=True, internal_dead=False, expires_in=None, extra=None):
        self.quark_cls = make_quark()
        self.quark = _serve(self.quark_cls)
        port = self.quark.server_address[1]
        self.public_cls = make_mp(self.quark_cls, port, delay=public_delay, expires_in=expires_in)
        self.public = _serve(self.public_cls)
        self.internal_cls = make_mp(self.quark_cls, port, expires_in=expires_in)
        self.internal = _serve(self.internal_cls)
        env = {
            'RELAY_SECRET': SECRET, 'MP_URL': f'http://127.0.0.1:{self.public.server_address[1]}',
            'MP_APIKEY': APIKEY, 'LISTEN_HOST': '127.0.0.1', 'LISTEN_PORT': '0', 'PATH_PREFIX': '/q',
        }
        if internal:
            env['MP_INTERNAL_URL'] = 'http://127.0.0.1:1' if internal_dead else \
                f'http://127.0.0.1:{self.internal.server_address[1]}'
        env.update(extra or {})
        self.config = relay.RelayConfig(env)
        self.config.chunk = 64 * 1024  # 小分段：一个文件要拉很多段，方便模拟途中过期
        self.relay = relay.build_server(self.config)
        self.links = self.relay.RequestHandlerClass.links
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.relay.server_address[1]}/q'

    def url(self, file_id='fid1', sig=None, prefetch=False):
        sig = sig or relay.signature(SECRET, 'quark', file_id)
        head = 'prefetch/' if prefetch else ''
        return f'{self.base}/{head}quark/{file_id}/{sig}?/a.mkv'

    def close(self):
        _stop(self.relay, self.public, self.internal, self.quark)


class ConfigTest(unittest.TestCase):
    def test_internal_url_first_then_public(self):
        config = relay.RelayConfig({'MP_URL': 'https://mp.example.com/', 'MP_INTERNAL_URL': 'http://10.0.0.2:3000'})
        self.assertEqual(config.link_bases, ['http://10.0.0.2:3000', 'https://mp.example.com'])

    def test_without_internal_url_falls_back_to_mp_url(self):
        config = relay.RelayConfig({'MP_URL': 'https://mp.example.com'})
        self.assertEqual(config.link_bases, ['https://mp.example.com'])

    def test_internal_only_is_valid(self):
        relay.RelayConfig({'RELAY_SECRET': 's', 'MP_APIKEY': 'k', 'MP_INTERNAL_URL': 'http://10.0.0.2:3000'}).validate()
        with self.assertRaises(SystemExit):
            relay.RelayConfig({'RELAY_SECRET': 's', 'MP_APIKEY': 'k'}).validate()

    def test_defaults_unchanged(self):
        config = relay.RelayConfig({})
        self.assertEqual((config.link_ttl, config.chunk, config.expiry_margin, config.log_level),
                         (600, 4 * 1024 * 1024, 60, logging.INFO))

    def test_log_level_parse(self):
        self.assertEqual(relay.RelayConfig({'LOG_LEVEL': 'debug'}).log_level, logging.DEBUG)
        self.assertEqual(relay.RelayConfig({'LOG_LEVEL': 'nonsense'}).log_level, logging.INFO)
        self.assertEqual(relay.RelayConfig({'LINK_TTL': 'abc'}).link_ttl, 600)


class ExpiryTest(unittest.TestCase):
    NOW = 1_800_000_000

    def test_oss_expires(self):
        url = f'https://cdn.example.com/v.mkv?Expires={self.NOW + 3600}&OSSAccessKeyId=x&Signature=y'
        self.assertEqual(relay.link_expiry(url, now=self.NOW), self.NOW + 3600)

    def test_x_oss_expires_and_auth_key(self):
        self.assertEqual(relay.link_expiry(f'https://a/b?x-oss-expires={self.NOW + 60}', now=self.NOW), self.NOW + 60)
        self.assertEqual(relay.link_expiry(f'https://a/b?auth_key={self.NOW + 90}-0-0-abc', now=self.NOW),
                         self.NOW + 90)

    def test_amz(self):
        url = 'https://a/b?X-Amz-Date=20270115T080000Z&X-Amz-Expires=600'
        self.assertEqual(relay.link_expiry(url, now=1_800_000_000), 1_800_000_000 + 600)

    def test_mp_fields_win(self):
        self.assertEqual(relay.link_expiry('https://a/b?Expires=1', {'expires_in': 120}, now=self.NOW), self.NOW + 120)

    def test_unknown_or_absurd(self):
        self.assertIsNone(relay.link_expiry('https://a/b?foo=1', now=self.NOW))
        self.assertIsNone(relay.link_expiry('https://a/b?Expires=99999999999', now=self.NOW))

    def test_ttl_uses_expiry_minus_margin_else_link_ttl(self):
        cache = relay.LinkCache(relay.RelayConfig({'LINK_TTL': '600', 'LINK_EXPIRY_MARGIN': '60'}))
        ttl = cache.ttl_for(f'https://a/b?Expires={int(time.time()) + 3600}', {})
        self.assertAlmostEqual(ttl, 3540, delta=3)
        self.assertEqual(cache.ttl_for('https://a/b', {}), 600)
        self.assertEqual(cache.ttl_for(f'https://a/b?Expires={int(time.time()) + 30}', {}), 0)


class InternalLinkTest(unittest.TestCase):
    def test_link_goes_through_internal_url_only(self):
        env = RelayEnv()
        try:
            self.assertEqual(urlopen(env.url()).read(), CONTENT)
            self.assertEqual(len(env.internal_cls.calls), 1)
            self.assertEqual(len(env.public_cls.calls), 0)
        finally:
            env.close()

    def test_without_internal_url_uses_mp_url(self):
        env = RelayEnv(internal=False)
        try:
            self.assertEqual(urlopen(env.url()).read(), CONTENT)
            self.assertEqual(len(env.public_cls.calls), 1)
        finally:
            env.close()

    def test_dead_internal_url_falls_back_and_backs_off(self):
        env = RelayEnv(internal_dead=True)
        try:
            self.assertEqual(urlopen(env.url('a1')).read(), CONTENT)
            self.assertEqual(urlopen(env.url('a2')).read(), CONTENT)
            self.assertEqual(len(env.public_cls.calls), 2)
            # 第二个文件不再先试内网：只有第一次那一下失败的尝试
            self.assertEqual(env.links.mp_calls, 3)
        finally:
            env.close()

    def test_cold_start_first_byte_saves_public_detour(self):
        """模拟公网绕行 +500ms：走内网时首包至少快 300ms（验收线）。"""
        slow = RelayEnv(public_delay=0.5, internal=False)
        fast = RelayEnv(public_delay=0.5)
        try:
            def first_byte(env):
                began = time.monotonic()
                with urlopen(Request(env.url('cold'), headers={'Range': 'bytes=0-'})) as response:
                    response.read(1)
                return time.monotonic() - began

            self.assertGreaterEqual(first_byte(slow) - first_byte(fast), 0.3)
        finally:
            slow.close()
            fast.close()


class CacheAndRenewTest(unittest.TestCase):
    def setUp(self):
        self.env = RelayEnv(expires_in=3600)

    def tearDown(self):
        self.env.close()

    def mp_calls(self):
        return len(self.env.internal_cls.calls)

    def test_second_open_hits_cache(self):
        urlopen(self.env.url()).read()
        urlopen(Request(self.env.url(), headers={'Range': 'bytes=1000-2000'})).read()
        self.assertEqual(self.mp_calls(), 1)

    def test_link_close_to_expiry_is_not_cached(self):
        env = RelayEnv(expires_in=30)  # 余量 60 秒 > 剩余 30 秒
        try:
            urlopen(env.url()).read()
            urlopen(env.url()).read()
            self.assertEqual(len(env.internal_cls.calls), 2)
        finally:
            env.close()

    def test_expired_cached_link_renews_once_on_open(self):
        urlopen(self.env.url()).read()
        self.env.quark_cls.valid_gen += 1  # 缓存里的直链失效
        self.assertEqual(urlopen(self.env.url()).read(), CONTENT)
        self.assertEqual(self.mp_calls(), 2)

    def test_410_also_renews(self):
        urlopen(self.env.url()).read()
        self.env.quark_cls.expired_status = 410
        self.env.quark_cls.valid_gen += 1
        self.assertEqual(urlopen(self.env.url()).read(), CONTENT)
        self.assertEqual(self.mp_calls(), 2)

    def test_link_expires_mid_stream_and_playback_continues(self):
        self.env.quark_cls.expire_after_hits = 3  # 拉到第 4 段时签名过期
        with urlopen(self.env.url()) as response:
            self.assertEqual(response.read(), CONTENT)
        self.assertEqual(self.mp_calls(), 2)

    def test_retry_only_once(self):
        urlopen(self.env.url()).read()
        before = self.mp_calls()
        # 重新取到的链接也被拒：只重取一次就放弃，返回 502
        self.env.quark_cls.reject_all = True
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.env.url())
        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(self.mp_calls() - before, 1)

    def test_concurrent_cold_requests_fetch_link_once(self):
        errors = []

        def play(offset):
            try:
                urlopen(Request(self.env.url('burst'), headers={'Range': f'bytes={offset}-{offset + 999}'})).read()
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=play, args=(i * 1000,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for c in self.env.internal_cls.calls if c.get('file_id') == 'burst'), 1)

    def test_prefetch_warms_cache(self):
        quark_hits = self.env.quark_cls.hits
        with urlopen(Request(self.env.url('pre', prefetch=True), method='POST', data=b'')) as response:
            self.assertEqual(response.status, 204)
        self.assertEqual(self.env.quark_cls.hits, quark_hits)  # 预取不拉流
        urlopen(self.env.url('pre')).read()
        self.assertEqual(sum(1 for c in self.env.internal_cls.calls if c.get('file_id') == 'pre'), 1)

    def test_prefetch_checks_signature(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.env.url('pre', sig='0' * 32, prefetch=True))
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.mp_calls(), 0)


class DebugLogTest(unittest.TestCase):
    def test_debug_line_per_request_without_secrets(self):
        env = RelayEnv(expires_in=3600)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger('quark-relay')
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        file_id = 'fid-0123456789abcdef'
        try:
            urlopen(env.url(file_id)).read()
            urlopen(Request(env.url(file_id), headers={'Range': 'bytes=10-20'})).read()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
            env.close()
        lines = [line for line in stream.getvalue().splitlines() if line.startswith('请求 ')]
        self.assertEqual(len(lines), 2)
        self.assertIn('cache=miss', lines[0])
        self.assertIn('cache=hit', lines[1])
        self.assertIn(f'bytes={len(CONTENT)}', lines[0])
        self.assertIn('bytes=11', lines[1])
        for word in ('link=', 'ttfb=', 'total='):
            self.assertIn(word, lines[0])
        text = stream.getvalue()
        sig = relay.signature(SECRET, 'quark', file_id)
        for secret in (APIKEY, 'COOKIE-SHOULD-NOT-LEAK', sig, 'gen=', 'Expires=', 'Signature', file_id, SECRET):
            self.assertNotIn(secret, text)

    def test_redaction_helpers(self):
        self.assertEqual(relay.safe_origin('https://user:pw@mp.example.com:8443/a?apikey=1'),
                         'https://mp.example.com:8443')
        sig = 'a' * 32
        self.assertNotIn(sig, relay.redact_path(f'/q/quark/fid/{sig}?/x.mkv'))
        self.assertNotIn('apikey', relay.redact_text('GET /x?apikey=1 HTTP/1.1'))

    def test_errors_do_not_echo_mp_address_or_token(self):
        config = relay.RelayConfig({'MP_URL': 'http://127.0.0.1:1', 'MP_APIKEY': APIKEY, 'RELAY_SECRET': 's'})
        with self.assertRaises(relay.LinkFetchError) as caught:
            relay.LinkCache(config).get('quark', 'fid', 'sig')
        self.assertNotIn(APIKEY, str(caught.exception))
        self.assertNotIn('relay/link', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
