"""Manage native MoviePilot STRM monitoring without changing download destinations."""

from copy import deepcopy
from pathlib import Path


JOURNAL = 'strm_directory_takeover'
DIRECTORIES = 'Directories'
IDENTITY = ('storage', 'download_path', 'media_type', 'media_category_id', 'media_category')


def identity(directory):
    """Identify a source/classification while allowing library and display edits."""
    return tuple(directory.get(key) for key in IDENTITY)


def overlaps(left, right):
    """Use resolved components so aliases and similarly named folders are safe."""
    left, right = Path(left).resolve(), Path(right).resolve()
    return left.is_relative_to(right) or right.is_relative_to(left)


def restore(directories, journal):
    """Undo only unchanged owned fields; keep unrelated and user-edited settings."""
    result = deepcopy(directories)
    for entry in journal.get('generated', []):
        if entry in result:
            result.remove(entry)
    for entry in journal.get('paused', []):
        candidates = [row for row in result if identity(row) == identity(entry['before'])]
        if len(candidates) != 1 or candidates[0].get('monitor_type') is not None:
            continue
        row = candidates[0]
        if 'monitor_type' in entry['before']:
            row['monitor_type'] = entry['before']['monitor_type']
        else:
            row.pop('monitor_type', None)
    return result


def plan(directories, rules, strm_path):
    """Keep raw roots first for downloads and clone their organization policies."""
    root = Path(strm_path)
    if not root.is_absolute() or '..' in root.parts or not root.is_dir():
        raise ValueError('STRM 本地目录必须是 MP 可读取的现有绝对路径')
    root = root.resolve()
    sources = [rule['local'] for rule in rules if rule.get('enabled', True)]
    if not sources or any(overlaps(root, source) for source in sources):
        raise ValueError('STRM 目录必须与原下载目录分开，不能互相包含')
    if any(row.get('library_path') and row.get('library_storage', 'local') == 'local'
           and overlaps(root, row['library_path']) for row in directories):
        raise ValueError('STRM 目录必须与媒体库目录分开，不能互相包含')
    templates = []
    for source in sources:
        matched = [row for row in directories if row.get('storage', 'local') == 'local'
                   and row.get('download_path') and overlaps(source, row['download_path'])]
        if not matched:
            raise ValueError('上传规则的 MP 本地目录未匹配到 MP 目录设置，请先设置媒体库目录')
        for row in matched:
            if not row.get('library_path'):
                raise ValueError('原下载目录未设置媒体库目录，无法接管 STRM 自动整理')
            if row not in templates:
                templates.append(row)
    if len({identity(row) for row in templates}) != len(templates):
        raise ValueError('MP 下载目录存在重复分类配置，请先合并重复目录')
    if any(row not in templates and row.get('storage', 'local') == 'local'
           and row.get('download_path') and overlaps(root, row['download_path'])
           for row in directories):
        raise ValueError('STRM 目录已被其他 MP 目录配置使用，请移除冲突配置或选择独立目录')
    result = deepcopy(directories)
    journal = {'paused': [], 'generated': [], 'path': str(root)}
    priority = max((int(row.get('priority') or 0) for row in directories), default=0) + 1
    for index, template in enumerate(templates):
        row = result[directories.index(template)]
        if row.get('monitor_type') is not None:
            journal['paused'].append({'before': deepcopy(template)})
            row['monitor_type'] = None
        generated = deepcopy(template)
        generated.update(name='下载上传 STRM · ' + (template.get('name') or str(index + 1)),
                         storage='local', download_path=str(root), monitor_type='monitor',
                         priority=priority + index)
        journal['generated'].append(generated)
        result.append(generated)
    return result, journal


class Organizer:
    """Persist a recovery journal before atomically changing the host's directories."""

    def __init__(self, store, systemconfig, eventmanager):
        self.store = store
        self.systemconfig = systemconfig
        self.eventmanager = eventmanager

    def configure(self, rules, strm_path=''):
        previous = self.store.meta(JOURNAL, {}) or {}
        recovery = previous.get('pending', previous.get('active', {}))
        if not strm_path and not recovery:
            return ''
        from app.schemas.event import ConfigChangeEventData
        from app.schemas.types import EventType

        if not callable(getattr(self.systemconfig, 'update_atomically', None)):
            raise ValueError('当前 MP 缺少目录原子更新接口，无法安全接管自动整理')

        def mutation(_session, current):
            current = current or []
            if not isinstance(current, list) or any(not isinstance(row, dict) for row in current):
                raise ValueError('MP 目录配置格式错误，未修改目录')
            restored = restore(current, recovery)
            if strm_path:
                updated, active = plan(restored, rules, strm_path)
            else:
                updated, active = restored, {}
            pending = {'paused': recovery.get('paused', []) + active.get('paused', []),
                       'generated': recovery.get('generated', []) + active.get('generated', [])}
            self.store.set_meta(JOURNAL, {'active': previous.get('active', {}), 'pending': pending})
            return (current != updated, active), updated

        changed, active = self.systemconfig.update_atomically(DIRECTORIES, mutation)
        # Keep the write-ahead journal if committing or notifying the host fails.
        if changed or previous.get('pending'):
            self.eventmanager.send_event(EventType.ConfigChanged,
                                         ConfigChangeEventData(key={DIRECTORIES}))
        self.store.set_meta(JOURNAL, {'active': active})
        return active.get('path', '')
