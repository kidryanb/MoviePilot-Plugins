"""0.5.16：PanSou 偶发返回 nginx「plain HTTP request was sent to HTTPS port」400 时重试一次。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
PKG = 'csf_pansou_gateway_under_test'

GATEWAY = (b"<html><head><title>400 The plain HTTP request was sent to HTTPS port</title></head>"
           b"<body><center><h1>400 Bad Request</h1></center>"
           b"<center>The plain HTTP request was sent to HTTPS port</center>"
           b"<hr><center>nginx</center></body></html>")


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.content = body

    def json(self):
        import json
        return json.loads(self.content)


calls = []
replies = []


def gated_request(gate, func, url, **kwargs):
    calls.append(kwargs['json'])
    return replies.pop(0)


_module('app')
_module('app.sdk')
_module('app.sdk.logging', logger=SimpleNamespace(
    debug=lambda *a, **k: None, info=lambda *a, **k: None,
    warning=lambda *a, **k: None, error=lambda *a, **k: None))
for _name in (PKG, f'{PKG}.search', f'{PKG}.search.pansou', f'{PKG}.utils'):
    _module(_name)
_module(f'{PKG}.search.types', PANSOU_RESOURCE_TYPES=('115', 'quark', 'magnet'),
        normalize_resource_type=lambda value: str(value or '').lower())
_module(f'{PKG}.utils.http_client', RequestGate=SimpleNamespace(shared=lambda *a, **k: object()),
        gated_request=gated_request, normalize_proxies=lambda proxy: None,
        requests=SimpleNamespace(post=None))
spec = importlib.util.spec_from_file_location(
    f'{PKG}.search.pansou.client', PLUGIN_DIR / 'search/pansou/client.py')
client_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = client_module
spec.loader.exec_module(client_module)
# 只替换这个模块看到的 time，不动全局 time.sleep（其它测试要用）
import time as _real_time
client_module.time = SimpleNamespace(sleep=lambda _: None, monotonic=_real_time.monotonic,
                                     time=_real_time.time)


class GatewayRetryTest(unittest.TestCase):
    def setUp(self):
        calls.clear()
        replies.clear()
        self.client = client_module.PanSouClient('https://example.invalid', auth_enabled=False)

    def test_gateway_400_retries_once_with_same_payload(self):
        replies.extend([Response(400, GATEWAY), Response(200, b'{"code":0,"data":{"results":[]}}')])
        result = self.client.request_search('测试', cloud_types=['115', 'magnet'])
        self.assertNotIn('error', result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_gateway_400_twice_reports_error(self):
        replies.extend([Response(400, GATEWAY), Response(400, GATEWAY)])
        result = self.client.request_search('测试', cloud_types=['115'])
        self.assertEqual(len(calls), 2)
        self.assertIn('HTTP 400', result['error'])

    def test_business_400_is_not_retried(self):
        replies.append(Response(400, b'{"code":400,"message":"bad"}'))
        result = self.client.request_search('测试', cloud_types=['115'])
        self.assertEqual(len(calls), 1)
        self.assertIn('HTTP 400', result['error'])


if __name__ == '__main__':
    unittest.main()
