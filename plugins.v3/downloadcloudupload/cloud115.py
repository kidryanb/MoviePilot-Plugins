"""Direct 115 uploads using the STRM helper's existing Cookie configuration."""

from http.cookies import SimpleCookie
from pathlib import PurePosixPath
from collections import Counter
import hashlib

from .core import UploadError, UploadStopped


def strm_cookie():
    """Read the documented cookies field without copying or mutating settings."""
    try:
        from app.sdk.plugin import PluginManager
        config = PluginManager().get_plugin_config('P115StrmHelper') or {}
        value = config.get('cookies')
        if not isinstance(value, str) or not value.strip() or '\r' in value or '\n' in value:
            raise ValueError('missing cookie')
        parsed = SimpleCookie(value)
        if any(key not in parsed or not parsed[key].value for key in ('UID', 'CID', 'SEID')):
            raise ValueError('incomplete cookie')
        return value.strip(), parsed['UID'].value.split('_')[0]
    except Exception:
        raise UploadError('115_STRM_COOKIE_REQUIRED', False) from None


def checked(response):
    """A provider error is never interpreted as an empty directory."""
    if not isinstance(response, dict) or response.get('state') not in (True, 1):
        raise UploadError('115_REQUEST_FAILED')
    return response


class Cloud115:
    """Use p115client already installed with the STRM helper; no storage plugin."""

    def client(self):
        cookie, account = strm_cookie()
        try:
            from p115client import P115Client
            return P115Client(cookie), account
        except ImportError:
            raise UploadError('115_STRM_CLIENT_REQUIRED', False) from None
        except Exception:
            raise UploadError('115_CLIENT_FAILED') from None

    @staticmethod
    def entries(client, parent):
        """Require a stable complete listing under the exact requested directory."""
        entries, offset, count = [], 0, None
        while True:
            response = checked(client.fs_files({'cid': parent, 'cur': 1, 'show_dir': 1,
                               'fc_mix': 1, 'limit': 1000, 'offset': offset,
                               'o': 'file_name', 'asc': 1}, timeout=30))
            path, data = response.get('path'), response.get('data')
            if (not isinstance(path, list) or not path or str(path[-1].get('cid')) != str(parent)
                    or not isinstance(data, list) or int(response.get('offset', -1)) != offset):
                raise UploadError('115_LIST_IDENTITY_MISMATCH')
            total = int(response.get('count', -1))
            if total < 0 or (count is not None and count != total):
                raise UploadError('115_LIST_CHANGED')
            count = total
            for raw in data:
                if not isinstance(raw, dict) or not isinstance(raw.get('n'), str):
                    raise UploadError('115_INVALID_FILE_LIST')
                is_file = bool(raw.get('fid'))
                identifier = raw.get('fid') if is_file else raw.get('cid')
                if identifier is None or str(raw.get('cid' if is_file else 'pid', '')) != str(parent):
                    raise UploadError('115_INVALID_FILE_LIST')
                entries.append({'name': raw['n'], 'type': 'file' if is_file else 'dir',
                                'id': str(identifier), 'size': int(raw.get('s', 0)),
                                'pickcode': str(raw.get('pc') or ''),
                                'sha1': str(raw.get('sha') or '').upper() if is_file else ''})
            offset += len(data)
            if offset == count:
                return entries
            if not data or offset > count:
                raise UploadError('115_INCOMPLETE_FILE_LIST')

    def child(self, client, parent, name):
        matches = [item for item in self.entries(client, parent) if item['name'] == name]
        if len(matches) > 1:
            raise UploadError('115_AMBIGUOUS_PATH', False)
        return matches[0] if matches else None

    def resolve(self, client, path):
        path = PurePosixPath(path)
        if not path.is_absolute() or '..' in path.parts:
            raise UploadError('115_INVALID_TARGET', False)
        current = {'id': '0', 'type': 'dir'}
        for name in path.parts[1:]:
            if current['type'] != 'dir':
                raise UploadError('REMOTE_PATH_IS_FILE', False)
            current = self.child(client, current['id'], name)
            if current is None:
                return None
        return current

    def lookup(self, target):
        client, account = self.client()
        try:
            item = self.resolve(client, target)
            if item is None:
                return None
            if item['type'] != 'file':
                raise UploadError('REMOTE_PATH_IS_DIRECTORY', False)
            ids = [account + ':' + item['id']]
            if item['pickcode']:
                ids.append(account + ':' + item['pickcode'])
            result = {'id': ids[0], 'ids': ids, 'size': item['size'], 'confirmed': True}
            if len(item.get('sha1') or '') == 40:
                result['sha1'] = item['sha1']
            return result
        except UploadError:
            raise
        except Exception:
            raise UploadError('REMOTE_QUERY_FAILED') from None

    def browse(self, path):
        """List selectable directories without creating or changing anything."""
        client, _ = self.client()
        current = self.resolve(client, path)
        if current is None or current['type'] != 'dir':
            raise UploadError('115_FOLDER_MISSING', False)
        path = PurePosixPath(path)
        children = self.entries(client, current['id'])
        names = Counter(item['name'] for item in children)
        folders = []
        for item in children:
            name = item['name']
            if item['type'] != 'dir':
                continue
            if names[name] != 1 or name in ('', '.', '..') or '/' in name:
                raise UploadError('115_AMBIGUOUS_PATH', False)
            folders.append({'name': name, 'path': str(path / name)})
        return {'path': str(path), 'parent': str(path.parent),
                'folders': sorted(folders, key=lambda item: item['name'].casefold())}

    def folder(self, target, root):
        client, account = self.client()
        try:
            root_path, target_path = PurePosixPath(root), PurePosixPath(target)
            if not target_path.is_relative_to(root_path):
                raise UploadError('115_INVALID_TARGET', False)
            current = self.resolve(client, root)
            if current is None or current['type'] != 'dir':
                raise UploadError('CONFIGURED_ROOT_MISSING', False)
            for name in target_path.relative_to(root_path).parts:
                child = self.child(client, current['id'], name)
                if child is None:
                    checked(client.fs_mkdir({'cname': name, 'pid': current['id']}, timeout=30))
                    child = self.child(client, current['id'], name)
                if child is None or child['type'] != 'dir':
                    raise UploadError('TARGET_FOLDER_UNAVAILABLE')
                current = child
            return {'id': current['id'], 'client': client, 'account': account}
        except UploadError:
            raise
        except Exception:
            raise UploadError('TARGET_FOLDER_UNAVAILABLE') from None

    @staticmethod
    def upload(folder, path, name, progress=None):
        """Only a provider receipt authorizes successful queue reconciliation."""
        try:
            from inspect import signature
            options = {}
            # Only named SDK parameters are safe; **kwargs may reach HTTP requests.
            try:
                parameters = signature(folder['client'].upload_file).parameters
            except (TypeError, ValueError):
                parameters = {}
            if any(parameter.kind == parameter.VAR_KEYWORD for parameter in parameters.values()):
                method = folder['client'].upload_file
                function = getattr(method, '__func__', method)
                delegate = getattr(function, '__globals__', {}).get('upload_file')
                if getattr(delegate, '__module__', '').startswith('p115oss'):
                    try:
                        parameters = signature(delegate).parameters
                    except (TypeError, ValueError):
                        pass
            if progress is not None:
                if 'make_reporthook' in parameters:
                    options['make_reporthook'] = lambda total: progress
                elif 'reporthook' in parameters:
                    options['reporthook'] = progress
                notify = getattr(progress, 'set_supported', None)
                if callable(notify):
                    notify(bool(options))
            prepare = getattr(progress, 'preparing', None)
            # Give the SDK a complete SHA1 so its hidden local scan has visible progress.
            known = getattr(progress, 'filesha1', None)
            if isinstance(known, str) and len(known) == 40:
                # The engine hashed this exact, signature-checked file moments ago.
                if callable(prepare):
                    prepare(0)
                    prepare(path.stat().st_size)
                options['filesha1'] = known.upper()
            elif callable(prepare):
                digest, read = hashlib.sha1(), 0
                prepare(0)
                with path.open('rb') as source:
                    while chunk := source.read(4 * 1024 * 1024):
                        digest.update(chunk)
                        read += len(chunk)
                        prepare(read)
                options['filesha1'] = digest.hexdigest().upper()
            result = checked(folder['client'].upload_file(file=str(path), pid=folder['id'],
                             filename=name, filesize=path.stat().st_size, partsize=-1, timeout=300, **options))
            data = result.get('data') or result
            identifier = (data.get('file_id') or data.get('fid') or data.get('pick_code')
                          or data.get('pickcode') or result.get('pickcode'))
            if not identifier:
                raise UploadError('UPLOAD_RESULT_UNKNOWN')
            return {'id': folder['account'] + ':' + str(identifier), 'size': path.stat().st_size,
                    'instant': bool(result.get('reuse'))}
        except UploadStopped:
            raise
        except Exception:
            raise UploadError('UPLOAD_RESULT_UNKNOWN') from None
