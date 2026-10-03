"""Short-lived 115 QR authorization, with no credentials in API projections."""

import base64
import json
import time
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import UploadError


class NoRedirect(HTTPRedirectHandler):
    """Keep authentication requests on the fixed provider endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request115(path, payload=None, image=False):
    """Use bounded requests and fixed error codes instead of raw responses."""
    data = urlencode(payload).encode() if payload is not None else None
    request = Request('https://qrcodeapi.115.com' + path, data=data,
                      headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError('response too large')
        if image:
            if not content.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('invalid QR image')
            return 'data:image/png;base64,' + base64.b64encode(content).decode('ascii')
        result = json.loads(content)
        if result.get('state') is not True or not isinstance(result.get('data'), dict):
            raise ValueError('provider rejected request')
        return result['data']
    except Exception:
        raise UploadError('115_LOGIN_REQUEST_FAILED') from None


class Login115:
    """One in-memory authorization session, replaced on explicit regeneration."""

    def __init__(self, request=request115, clock=time.time):
        self.request, self.clock = request, clock
        self.clear()

    def clear(self):
        self.token = None
        self.image = ''
        self.cookie = ''
        self.expires = 0
        self.state = 'idle'

    def start(self):
        self.clear()
        token = self.request('/api/1.0/web/1.0/token/')
        if any(not isinstance(token.get(key), (str, int)) or not str(token[key])
               or len(str(token[key])) > 256 for key in ('uid', 'time', 'sign')):
            raise UploadError('115_LOGIN_INVALID_TOKEN', False)
        token = {key: token[key] for key in ('uid', 'time', 'sign')}
        image = self.request('/api/1.0/web/1.0/qrcode?' + urlencode({'uid': token['uid']}), image=True)
        self.token, self.image = token, image
        self.expires = self.clock() + 300
        self.state = 'waiting'

    def poll(self):
        if self.state == 'saved':
            return self.state
        if not self.token or self.clock() >= self.expires:
            self.clear()
            self.state = 'expired'
            return self.state
        if self.cookie:
            return 'confirmed'
        status = self.request('/get/status/?' + urlencode(self.token)).get('status')
        if status in (-1, -2):
            self.clear()
            self.state = 'expired' if status == -1 else 'cancelled'
        elif status in (0, 1):
            self.state = 'waiting' if status == 0 else 'scanned'
        elif status == 2:
            values = self.request('/app/1.0/web/1.0/login/qrcode/', {'account': self.token['uid']}).get('cookie')
            if (not isinstance(values, dict) or any(
                not isinstance(values.get(key), str) or not values[key] or len(values[key]) > 4096
                or any(char in values[key] for char in '\r\n;') for key in ('UID', 'CID', 'SEID'))):
                raise UploadError('115_LOGIN_INVALID_COOKIE', False)
            self.cookie = '; '.join(f'{key}={values[key]}' for key in ('UID', 'CID', 'SEID'))
            self.state = 'confirmed'
        else:
            raise UploadError('115_LOGIN_INVALID_STATUS', False)
        return self.state

    def saved(self):
        self.clear()
        self.state = 'saved'

    def public(self):
        if self.token and self.clock() >= self.expires:
            self.clear()
            self.state = 'expired'
        return {'state': self.state, 'image': self.image}
