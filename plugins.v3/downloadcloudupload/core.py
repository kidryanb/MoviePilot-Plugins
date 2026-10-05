"""Downloader-independent mapping, durable queue and upload state machine."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.mov', '.wmv', '.webm'}
SIDECAR_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.mka', '.ac3', '.aac', '.flac'}
LABELS = {
    'baseline': '启用前已完成', 'waiting_complete': '等待下载完成',
    'needs_mapping': '未配置目标文件夹', 'waiting_source': '等待本地文件',
    'queued': '待上传', 'uploading': '上传中', 'verifying': '待核对',
    'retry_wait': '等待重试', 'success': '上传成功', 'already_exists': '远端已存在',
    'conflict': '文件冲突', 'failed': '上传失败', 'ignored': '已忽略',
}


class UploadError(Exception):
    """Fixed, credential-free error classification for service boundaries."""

    def __init__(self, code: str, retryable: bool = True):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class UploadStopped(UploadError):
    """The SDK byte iterator acknowledged a cooperative stop before sending more data."""

    def __init__(self):
        super().__init__('UPLOAD_STOP_REQUESTED')


@dataclass(frozen=True)
class Torrent:
    """A complete snapshot of a downloader task without account credentials."""

    instance: str
    hash: str
    title: str
    save_path: str
    complete: bool
    completed_at: float = 0

    @property
    def key(self):
        return json.dumps([self.instance, self.hash], ensure_ascii=False)


@dataclass(frozen=True)
class TorrentFile:
    """Selected status and exact downloaded bytes for one torrent member."""

    name: str
    size: int
    selected: bool
    complete: bool


def downloader_path(value: str):
    """Preserve Windows path semantics even when MP itself runs on Linux."""
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('路径不能为空')
    path = PureWindowsPath(value) if ('\\' in value or (len(value) > 1 and value[1] == ':')) else PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('来源必须是没有上级引用的绝对路径')
    return path


def validate_rules(rules: list[dict]) -> list[dict]:
    """Validate folder rules and reject equally-specific ambiguous mappings."""
    if not isinstance(rules, list):
        raise ValueError('文件夹规则必须为列表')
    result = []
    for index, raw in enumerate(rules):
        if not isinstance(raw, dict):
            raise ValueError('文件夹规则格式错误')
        rule = {key: str(raw.get(key) or '').strip() for key in ('name', 'instance', 'source', 'local', 'storage', 'target')}
        rule['storage'] = rule['storage'] or '115'
        rule['enabled'] = bool(raw.get('enabled', True))
        if not rule['source'] and not rule['local'] and not rule['target']:
            continue
        downloader_path(rule['source'])
        local = Path(rule['local'])
        target = PurePosixPath(rule['target'])
        if not local.is_absolute() or '..' in local.parts or not target.is_absolute() or '..' in target.parts:
            raise ValueError('本地和网盘文件夹必须是没有上级引用的绝对路径')
        if not rule['storage'] or rule['storage'].lower() in {'local', 'smb'}:
            raise ValueError('请选择网盘储存')
        rule['name'] = rule['name'] or f'规则 {index + 1}'
        for other in result:
            if (rule['enabled'] and other['enabled'] and downloader_path(rule['source']) == downloader_path(other['source'])
                    and (not rule['instance'] or not other['instance'] or rule['instance'] == other['instance'])):
                raise ValueError('同一来源文件夹存在重复规则')
        result.append(rule)
    return result


def map_file(instance: str, full_path: str, rules: list[dict]):
    """Choose the deepest component-matching source root and preserve structure."""
    path = downloader_path(full_path)
    matches = []
    for rule in rules:
        if not rule['enabled'] or rule['instance'] not in ('', instance):
            continue
        root = downloader_path(rule['source'])
        if type(root) is type(path) and path.is_relative_to(root):
            matches.append((len(root.parts), rule, path.relative_to(root)))
    if not matches:
        return None
    _, rule, relative = max(matches, key=lambda entry: entry[0])
    if not relative.parts:
        raise ValueError('来源路径不是文件')
    local_root = Path(rule['local']).resolve()
    local = local_root.joinpath(*relative.parts)
    # Resolve symlinks before accepting a read, and again immediately before upload.
    if not local.resolve().is_relative_to(local_root):
        raise ValueError('文件位于来源文件夹之外')
    return {
        'rule': rule['name'], 'root': str(local_root), 'local': str(local),
        'storage': rule['storage'], 'target_root': rule['target'],
        'target': str(PurePosixPath(rule['target']).joinpath(*relative.parts)),
    }


def signature(path: Path):
    """Record source identity and reject a symlink or non-regular member."""
    if path.is_symlink() or not path.is_file():
        raise UploadError('SOURCE_MISSING')
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def digest_file(path: Path, stopped=lambda: False):
    """Hash file contents in bounded chunks, allowing stop between reads."""
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(4 * 1024 * 1024):
            if stopped():
                raise UploadError('STOPPED')
            digest.update(chunk)
    return digest.hexdigest()


class Store:
    """Transactional, plugin-owned SQLite state with no host database writes."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS torrents (
                    key TEXT PRIMARY KEY, instance TEXT NOT NULL, hash TEXT NOT NULL,
                    title TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY, task_key TEXT NOT NULL, name TEXT NOT NULL,
                    size INTEGER NOT NULL, full_path TEXT NOT NULL, mapping TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', message TEXT NOT NULL DEFAULT '',
                    signature TEXT, stable_at REAL NOT NULL DEFAULT 0, digest TEXT,
                    receipt TEXT, attempted INTEGER NOT NULL DEFAULT 0,
                    retries INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                    updated REAL NOT NULL, UNIQUE(task_key, name));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, file_id INTEGER, state TEXT NOT NULL,
                    message TEXT NOT NULL, created REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        """Open a short-lived connection, isolated from worker and UI threads."""
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, parameters=()):
        """Return detached dictionaries from a read transaction."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, parameters)]

    def meta(self, key, default=None):
        """Read a baseline or timestamp value."""
        rows = self.rows('SELECT value FROM meta WHERE key=?', (key,))
        return json.loads(rows[0]['value']) if rows else default

    def set_meta(self, key, value):
        """Persist a baseline atomically."""
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def torrent(self, task: Torrent, state: str, message='', overwrite=False):
        """Remember discovery without accidentally resetting ignored baselines."""
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO torrents VALUES (?,?,?,?,?,?,?)',
                       (task.key, task.instance, task.hash, task.title, state, message, time.time()))
            if overwrite:
                db.execute('UPDATE torrents SET state=?,message=?,updated=? WHERE key=?',
                           (state, message, time.time(), task.key))

    def enqueue(self, task: Torrent, member: TorrentFile, full_path: str, mapping: dict):
        """Queue each selected member once; existing targets remain immutable."""
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO files
                (task_key,name,size,full_path,mapping,updated) VALUES (?,?,?,?,?,?)''',
                       (task.key, member.name, member.size, full_path, json.dumps(mapping), time.time()))

    def update(self, file_id: int, **values):
        """Atomically persist a transition and append its credential-free event."""
        allowed = {'state', 'message', 'signature', 'stable_at', 'digest', 'receipt', 'attempted', 'retries', 'next_at'}
        if not values or not set(values) <= allowed:
            raise ValueError('Invalid state update')
        values['updated'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE files SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE id=?',
                       (*values.values(), file_id))
            if 'state' in values:
                db.execute('INSERT INTO events(file_id,state,message,created) VALUES (?,?,?,?)',
                           (file_id, values['state'], values.get('message', ''), time.time()))


class Engine:
    """One serial worker for polling, reconciliation and source-preserving upload."""

    def __init__(self, store: Store, gateway, rules: list[dict], instances: list[str],
                 stable_seconds=10, retry_limit=5, sidecars=False, stopped=lambda: False,
                 extensions=None, excluded=None):
        self.store, self.gateway = store, gateway
        self.rules = validate_rules(rules)
        self.instances = instances
        self.stable_seconds, self.retry_limit = stable_seconds, retry_limit
        self.extensions = (set(extensions) if extensions is not None else VIDEO_EXTENSIONS) | (SIDECAR_EXTENSIONS if sidecars else set())
        self.excluded = [str(value).casefold() for value in (excluded or []) if value]
        self.stopped = stopped
        self.lock = threading.Lock()

    def scan(self, backfill=None, recent_days=0):
        """Initialize successful baselines and queue only complete selected files."""
        preview = []
        for instance in self.instances:
            if self.stopped():
                break
            try:
                tasks = self.gateway.tasks(instance)
                first = self.store.meta('baseline:' + instance) is None
                for task in tasks:
                    if not task.hash:
                        raise UploadError('TASK_ID_MISSING', False)
                    existing = self.store.rows('SELECT state FROM torrents WHERE key=?', (task.key,))
                    if backfill == 'preview':
                        if task.complete:
                            preview.append({**asdict(task), 'key': task.key,
                                            'time_unknown': not bool(task.completed_at)})
                        continue
                    selected = isinstance(backfill, list) and task.key in backfill
                    if first and task.complete and not selected and not existing:
                        self.store.torrent(task, 'baseline')
                        continue
                    if existing and existing[0]['state'] in {'baseline', 'ignored'} and not selected:
                        continue
                    self.store.torrent(task, 'waiting_complete' if not task.complete else 'queued', overwrite=True)
                    if not task.complete:
                        continue
                    if selected and recent_days and (not task.completed_at or task.completed_at < time.time() - recent_days * 86400):
                        self.store.torrent(task, 'baseline', '完成时间未知或不在补传范围', overwrite=True)
                        continue
                    members = self.gateway.files(instance, task.hash)
                    if not members or any(f.selected and not f.complete for f in members):
                        self.store.torrent(task, 'waiting_complete', '等待文件清单完成', overwrite=True)
                        continue
                    missing_mapping = False
                    for member in members:
                        if not member.selected or not member.size or Path(member.name).suffix.lower() not in self.extensions:
                            continue
                        if any(keyword in member.name.casefold() for keyword in self.excluded):
                            continue
                        relative = PurePosixPath(member.name.replace('\\', '/'))
                        if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
                            raise UploadError('UNSAFE_MEMBER_PATH', False)
                        full = str(downloader_path(task.save_path).joinpath(*relative.parts))
                        mapping = map_file(instance, full, self.rules)
                        if mapping:
                            self.store.enqueue(task, member, full, mapping)
                        else:
                            missing_mapping = True
                    if missing_mapping:
                        self.store.torrent(task, 'needs_mapping', overwrite=True)
                if backfill != 'preview':
                    self.store.set_meta('baseline:' + instance, time.time())
                self.store.set_meta('connection:' + instance, {'ok': True, 'checked': time.time()})
            except Exception as error:
                code = error.code if isinstance(error, UploadError) else 'DOWNLOADER_QUERY_FAILED'
                self.store.set_meta('connection:' + instance, {'ok': False, 'code': code, 'checked': time.time()})
        return preview

    def run(self):
        """Poll without overlapping scans; upload one eligible item per invocation."""
        if self.stopped() or not self.lock.acquire(blocking=False):
            return
        try:
            self.scan()
            self.process()
        finally:
            self.lock.release()

    def process(self, budget=1):
        """Advance due files while leaving unavailable tasks out of the hot path."""
        self.resolve_restarts()
        rows = self.store.rows('''SELECT f.*,t.instance,t.hash,t.title FROM files f
            JOIN torrents t ON t.key=f.task_key WHERE f.state IN
            ('queued','waiting_source','waiting_complete','retry_wait','verifying')
            AND f.next_at<=? ORDER BY f.id LIMIT 100''', (time.time(),))
        uploads = 0
        for row in rows:
            if self.stopped() or uploads >= budget:
                break
            if self.store.meta('restart_request:' + str(row['id'])):
                continue
            try:
                uploads += int(self._process(row))
            except Exception as error:
                self._fail(row, error)
        self.resolve_restarts()

    def request_restart(self, file_id):
        """Publish a stop request for the exact capable upload attempt, without its lock."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM files WHERE id=?', (file_id,)).fetchone()
            raw = db.execute('SELECT value FROM meta WHERE key=?', ('upload_progress:' + str(file_id),)).fetchone()
            progress = json.loads(raw['value']) if raw else {}
            if not row or row['state'] != 'uploading' or not progress.get('can_stop') or not progress.get('attempt'):
                return False
            request = {'attempt': progress['attempt'], 'requested_at': time.time()}
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                       ('restart_request:' + str(file_id), json.dumps(request)))
        return True

    def resolve_restarts(self):
        """Only a worker owning the upload lease can requeue acknowledged stops."""
        requests = self.store.rows("SELECT key,value FROM meta WHERE key LIKE 'restart_request:%'")
        for entry in requests:
            request = json.loads(entry['value'])
            if not request:
                continue
            file_id = int(entry['key'].split(':')[-1])
            rows = self.store.rows('SELECT * FROM files WHERE id=?', (file_id,))
            progress = self.store.meta('upload_progress:' + str(file_id), {}) or {}
            if not rows or progress.get('attempt') != request.get('attempt'):
                self.store.set_meta(entry['key'], None)
                continue
            row = rows[0]
            if row['state'] == 'uploading':
                continue
            if row['message'] == 'USER_REQUESTED_RESTART':
                try:
                    changed = self.action([file_id], 'restart')
                    if not changed:
                        self.store.update(file_id, state='verifying',
                                          message='AWAITING_REMOTE_CONFIRMATION', next_at=0)
                except Exception:
                    # A failed remote lookup does not authorize sending the file again.
                    continue
            # A normal completion or uncertain failure is verified, never blindly restarted.
            self.store.set_meta(entry['key'], None)

    def _process(self, row):
        """Recheck completion, stable identity, remote collisions and upload receipt."""
        mapping = json.loads(row['mapping'])
        path = Path(mapping['local'])
        if not path.resolve().is_relative_to(Path(mapping['root']).resolve()):
            raise UploadError('SOURCE_OUTSIDE_ROOT', False)
        sig = signature(path)
        if sig[2] != row['size']:
            self.store.update(row['id'], state='waiting_source', message='SOURCE_SIZE_CHANGED', next_at=time.time() + 60)
            return False
        if row['signature'] != json.dumps(sig):
            if row['attempted']:
                raise UploadError('SOURCE_CHANGED_AFTER_UPLOAD', False)
            self.store.update(row['id'], signature=json.dumps(sig), stable_at=time.time(),
                              state='queued', next_at=time.time() + self.stable_seconds, digest=None)
            return False
        if time.time() - row['stable_at'] < self.stable_seconds:
            return False
        if not self.gateway.ready(row['instance'], row['hash'], row['name'], row['size'], row['full_path']):
            self.store.update(row['id'], state='waiting_complete', message='TASK_NOT_READY', next_at=time.time() + 60)
            return False
        digest = row['digest'] or digest_file(path, self.stopped)
        if signature(path) != sig:
            raise UploadError('SOURCE_CHANGED', False)
        self.store.update(row['id'], digest=digest)
        remote = self.gateway.lookup(mapping['storage'], mapping['target'])
        if remote is not None:
            if remote['size'] != row['size']:
                self.store.update(row['id'], state='conflict', message='REMOTE_SIZE_CONFLICT')
                return False
            proof = self._proof(row, remote, digest)
            if proof:
                self.store.update(row['id'], state='success' if row['attempted'] else 'already_exists',
                                  message='VERIFIED_RECEIPT' if row['attempted'] else 'VERIFIED_CONTENT', next_at=0)
                return False
            self.store.update(row['id'], state='conflict', message='REMOTE_CONTENT_UNCONFIRMED')
            return False
        if row['attempted']:
            # An SDK upload may still be in flight after a timeout; never blindly repeat it.
            self.store.update(row['id'], state='verifying', message='UPLOAD_RESULT_UNKNOWN', next_at=time.time() + 60)
            return False
        folder = self.gateway.folder(mapping['storage'], str(PurePosixPath(mapping['target']).parent), mapping['target_root'])
        # Persist intent before crossing the external-write boundary.
        self.store.update(row['id'], state='uploading', attempted=1, message='')
        progress_key = 'upload_progress:' + str(row['id'])
        attempt = uuid.uuid4().hex
        can_stop = False
        self.store.set_meta(progress_key, {'sent': 0, 'total': row['size'], 'speed': 0,
                            'phase': 'preparing', 'updated': time.time(), 'attempt': attempt, 'can_stop': False})
        sent, previous_sent, previous_at = 0, 0, time.monotonic()
        reported = False
        stop_requested = False
        last_stop_check = 0

        def progress(increment):
            nonlocal sent, previous_sent, previous_at, reported, stop_requested, last_stop_check
            now = time.monotonic()
            increment = int(increment)
            if now - last_stop_check >= 0.25 or sent + increment >= row['size']:
                try:
                    request = self.store.meta('restart_request:' + str(row['id']), {}) or {}
                except Exception:
                    request = {}
                last_stop_check = now
                if request.get('attempt') == attempt:
                    stop_requested = True
                    raise UploadStopped()
            sent = max(0, min(row['size'], sent + increment))
            elapsed = now - previous_at
            if reported and elapsed < 1 and sent != row['size']:
                return
            try:
                self.store.set_meta(progress_key, {'sent': sent, 'total': row['size'],
                                    'speed': max(0, sent - previous_sent) / max(elapsed, 0.001),
                                    'phase': 'uploading', 'updated': time.time(),
                                    'attempt': attempt, 'can_stop': can_stop})
            except Exception:
                # Display telemetry must not interrupt an upload already in flight.
                return
            reported = True
            previous_sent, previous_at = sent, now

        def set_supported(supported):
            nonlocal can_stop
            can_stop = bool(supported)
            current = self.store.meta(progress_key, {}) or {}
            current['can_stop'] = can_stop
            self.store.set_meta(progress_key, current)

        progress.set_supported = set_supported

        try:
            receipt = self.gateway.upload(folder, path, PurePosixPath(mapping['target']).name, progress=progress)
        except Exception:
            if stop_requested:
                # HTTP adapters may wrap the iterator's cancellation exception.
                raise UploadStopped() from None
            raise
        if signature(path) != sig:
            raise UploadError('SOURCE_CHANGED_AFTER_UPLOAD', False)
        self.store.update(row['id'], state='verifying', receipt=json.dumps(receipt) if receipt else None,
                          message='AWAITING_REMOTE_CONFIRMATION', next_at=0)
        if receipt and receipt.get('id') and receipt.get('size') == row['size']:
            try:
                self.store.set_meta(progress_key, {'sent': row['size'], 'total': row['size'], 'speed': 0,
                                    'phase': 'submitted', 'updated': time.time(),
                                    'attempt': attempt, 'can_stop': False})
            except Exception:
                # The durable receipt above still proves submission if telemetry fails.
                pass
        return True

    def _proof(self, row, remote, digest):
        """Require content evidence or this plugin's own matching upload receipt."""
        if remote.get('sha256') and remote['sha256'] == digest:
            return True
        receipt = json.loads(row['receipt']) if row['receipt'] else None
        if (row['attempted'] and receipt and receipt.get('id') and receipt['id'] in remote.get('ids', [remote.get('id')])
                and receipt.get('size') == row['size'] and remote.get('confirmed', False)):
            return True
        candidates = self.store.rows("SELECT mapping,receipt,digest FROM files WHERE state IN ('success','already_exists') AND digest=?", (digest,))
        for candidate in candidates:
            old = json.loads(candidate['mapping'])
            proof = json.loads(candidate['receipt']) if candidate['receipt'] else None
            if (old['storage'] == json.loads(row['mapping'])['storage'] and old['target'] == json.loads(row['mapping'])['target']
                    and proof and proof.get('id') and proof['id'] in remote.get('ids', [remote.get('id')]) and remote.get('confirmed', False)):
                return True
        return False

    def _fail(self, row, error):
        """Persist bounded retries using safe codes; never log raw SDK errors."""
        current = self.store.rows('SELECT * FROM files WHERE id=?', (row['id'],))[0]
        if isinstance(error, UploadStopped):
            self.store.update(row['id'], state='verifying', message='USER_REQUESTED_RESTART', next_at=0)
            return
        code = error.code if isinstance(error, UploadError) else 'SERVICE_ERROR'
        retryable = not isinstance(error, UploadError) or error.retryable
        retries = current['retries'] + 1
        if current['attempted'] and retryable:
            self.store.update(row['id'], state='verifying' if retries <= self.retry_limit else 'failed',
                              message=code, retries=retries, next_at=time.time() + 60)
        elif retryable and retries <= self.retry_limit:
            delay = [60, 300, 900, 1800, 3600][min(retries - 1, 4)]
            self.store.update(row['id'], state='retry_wait', message=code, retries=retries, next_at=time.time() + delay)
        else:
            self.store.update(row['id'], state='failed', message=code, retries=retries)

    def action(self, ids: list[int], action: str, allow_stale_upload=False):
        """Perform explicit file actions without discarding successful deduplication."""
        if action not in {'retry', 'ignore', 'remap', 'verify', 'restart'}:
            raise ValueError('未知操作')
        changed = 0
        for file_id in dict.fromkeys(ids):
            rows = self.store.rows('''SELECT f.*,t.instance FROM files f JOIN torrents t
                ON t.key=f.task_key WHERE f.id=?''', (file_id,))
            if not rows or rows[0]['state'] in {'success', 'already_exists'}:
                continue
            row = rows[0]
            if row['state'] == 'uploading' and not (action == 'restart' and allow_stale_upload):
                continue
            if action == 'restart':
                mapping = json.loads(row['mapping'])
                if self.gateway.lookup(mapping['storage'], mapping['target']) is not None:
                    continue
                self.store.update(file_id, attempted=0, receipt=None, state='queued',
                                  message='USER_CONFIRMED_RESTART', next_at=0, retries=0)
                self.store.set_meta('upload_progress:' + str(file_id), {})
                changed += 1
                continue
            if action == 'remap':
                if row['attempted']:
                    continue
                mapping = map_file(row['instance'], row['full_path'], self.rules)
                if not mapping:
                    continue
                with self.store.connect() as db:
                    db.execute('UPDATE files SET mapping=? WHERE id=?', (json.dumps(mapping), file_id))
            state = 'ignored' if action == 'ignore' else ('verifying' if row['attempted'] else 'queued')
            self.store.update(file_id, state=state, message='', next_at=0, retries=0)
            changed += 1
        return changed


@contextmanager
def worker_lease(path: Path):
    """Prevent overlapping cloud writes across hot-reloaded plugin instances."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
