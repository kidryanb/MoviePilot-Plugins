"""Cloudflare 拦截页识别与 SeedHub 外部求解服务优先的离线测试。

cloudflare.py / seedhub/security.py 依赖宿主与 curl_cffi，这里用最小替身加载。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
PKG = 'csf_cloudflare_under_test'


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _RequestException(Exception):
    pass


class _Timeout(_RequestException):
    pass


_saved = {name: sys.modules.get(name) for name in (
    'app', 'app.sdk', 'app.sdk.config', 'app.sdk.logging', 'app.helper',
    'app.helper.browser', 'curl_cffi',
)}
_logger = types.SimpleNamespace(**{k: (lambda *a, **kw: None) for k in ('debug', 'info', 'warning', 'error')})
_module('app')
_module('app.sdk')
_module('app.sdk.config', settings=types.SimpleNamespace())
_module('app.sdk.logging', logger=_logger)
_module('app.helper')
_module('app.helper.browser', PlaywrightHelper=object)
_requests = types.SimpleNamespace(
    exceptions=types.SimpleNamespace(RequestException=_RequestException),
    get=None, post=None,
)
_module('curl_cffi', requests=_requests)

for _name in (PKG, f'{PKG}.utils', f'{PKG}.search', f'{PKG}.search.seedhub'):
    _module(_name)
_module(
    f'{PKG}.utils.http_client',
    normalize_proxies=lambda proxy: proxy if isinstance(proxy, dict) else ({'https': proxy} if proxy else {}),
    normalize_service_url=lambda value: str(value or '').rstrip('/'),
    request_error_summary=lambda error: str(error),
    RequestGate=object,
    gated_request=lambda gate, func, *a, **kw: func(*a, **kw),
    requests=_requests,
)
cloudflare = _load(f'{PKG}.utils.cloudflare', 'utils/cloudflare.py')
security = _load(f'{PKG}.search.seedhub.security', 'search/seedhub/security.py')

for _name, _value in _saved.items():
    if _value is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _value

CHALLENGE_PAGE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><form id="challenge-form" action="/?__cf_chl_f_tk=x"></form>
<script>window._cf_chl_opt={cvId:'3'};</script>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>"""

SOLVED_PAGE = """<!DOCTYPE html><html><head><title>SeedHub - 现在就出发</title></head>
<body><div class="movie-list">ok</div>
<script>(function(){var a=document.createElement('script');
a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';document.head.appendChild(a);})();</script>
</body></html>"""

LOGIN_WITH_TURNSTILE = """<html><head><title>登录</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script></head>
<body><form id="login"></form></body></html>"""


class _Response:
    def __init__(self, text='', status=200, headers=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            raise _RequestException(f'HTTP {self.status_code}')


class _Gate:
    def __init__(self):
        self.cooldowns = []

    def run(self, func):
        return func()

    def activate_cooldown(self, seconds, reason=''):
        self.cooldowns.append(seconds)


class ChallengeMarkerTest(unittest.TestCase):
    def test_real_interstitial_is_a_challenge(self):
        self.assertTrue(cloudflare.is_cloudflare_challenge(CHALLENGE_PAGE, 403, {'server': 'cloudflare'}))
        self.assertTrue(cloudflare.is_cloudflare_challenge(CHALLENGE_PAGE))

    def test_jsd_beacon_after_solve_is_not_a_challenge(self):
        self.assertFalse(cloudflare.is_cloudflare_challenge(SOLVED_PAGE, 200, {'server': 'cloudflare'}))

    def test_embedded_turnstile_widget_is_not_a_challenge(self):
        self.assertFalse(cloudflare.is_cloudflare_challenge(LOGIN_WITH_TURNSTILE, 200, {}))

    def test_cf_mitigated_header_still_counts(self):
        self.assertTrue(cloudflare.is_cloudflare_challenge('', 403, {'cf-mitigated': 'challenge'}))


class SeedHubSolverFirstTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.direct = []
        self.solver_result = {
            'html': SOLVED_PAGE,
            'cookies': [{'name': 'cf_clearance', 'value': 'abc', 'domain': '.seedhub.cc'}],
            'user_agent': 'UA-FlareSolverr',
        }
        self.solver_enabled = True
        self._patch(security, 'is_cf_solver_enabled', lambda: self.solver_enabled)
        self._patch(security, 'solve_via_external_solver', self._solver)
        self._patch(security, 'playwright_snapshot', self._browser)
        self.direct_response = lambda headers: _Response(SOLVED_PAGE)
        self._patch(security.requests, 'get', self._direct)
        self.client = security.SeedHubSecurity(
            base_url='https://www.seedhub.cc', proxy=None, request_timeout=20,
            error_type=RuntimeError,
        )
        self.gate = _Gate()
        self.client.bind_gate(self.gate)

    def _patch(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _solver(self, url, proxy=None, timeout=60):
        self.calls.append(('solver', url))
        return self.solver_result

    def _browser(self, url, proxy, timeout, gate):
        self.calls.append(('browser', url))
        return None

    def _direct(self, url, **kwargs):
        self.direct.append(dict(kwargs.get('headers') or {}))
        result = self.direct_response(kwargs.get('headers') or {})
        if isinstance(result, Exception):
            raise result
        return result

    def test_first_request_goes_through_solver_and_reuses_cookie(self):
        self.assertEqual(self.client.get_text('https://www.seedhub.cc/s/x'), SOLVED_PAGE)
        self.assertEqual(self.calls, [('solver', 'https://www.seedhub.cc/s/x')])
        self.assertEqual(self.direct, [])
        self.client.get_text('https://www.seedhub.cc/s/y')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.direct[0].get('Cookie'), 'cf_clearance=abc')
        self.assertEqual(self.direct[0].get('User-Agent'), 'UA-FlareSolverr')

    def test_direct_timeout_falls_back_to_solver(self):
        self.client._cookie_header = 'cf_clearance=old'
        self.direct_response = lambda headers: _Timeout('Timeout')
        self.assertEqual(self.client.get_text('https://www.seedhub.cc/s/x'), SOLVED_PAGE)
        self.assertEqual(len(self.direct), 2)
        self.assertEqual(self.calls, [('solver', 'https://www.seedhub.cc/s/x')])

    def test_challenge_prefers_solver_over_browser(self):
        self.client._cookie_header = 'cf_clearance=old'
        self.direct_response = lambda headers: _Response(CHALLENGE_PAGE, 403, {'server': 'cloudflare'})
        self.assertEqual(self.client.get_text('https://www.seedhub.cc/s/x'), SOLVED_PAGE)
        self.assertEqual([kind for kind, _ in self.calls], ['solver'])

    def test_without_solver_keeps_direct_then_browser(self):
        self.solver_enabled = False
        self.direct_response = lambda headers: _Response(CHALLENGE_PAGE, 403, {'server': 'cloudflare'})
        with self.assertRaises(RuntimeError):
            self.client.get_text('https://www.seedhub.cc/s/x')
        self.assertEqual([kind for kind, _ in self.calls], ['browser'])

    def test_failed_solver_is_not_retried_first_for_a_while(self):
        self.solver_result = None
        self.client.get_text('https://www.seedhub.cc/s/x')
        self.assertEqual([kind for kind, _ in self.calls], ['solver'])
        self.client.get_text('https://www.seedhub.cc/s/y')
        self.assertEqual([kind for kind, _ in self.calls], ['solver'])


if __name__ == '__main__':
    unittest.main()
