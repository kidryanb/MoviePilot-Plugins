"""Read MP's configured downloader services and use its public storage chain."""

from pathlib import Path
from datetime import datetime

from app.sdk.services import DownloaderHelper

from .core import Torrent, TorrentFile, UploadError, downloader_path
from .cloud115 import Cloud115


def field(value, *names, default=None):
    """Read provider dictionaries or Transmission RPC projections safely."""
    for name in names:
        try:
            found = value.get(name) if isinstance(value, dict) else getattr(value, name)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if found is not None:
            return found
    return default


def epoch(value):
    """Normalize download completion dates without using the host's local zone."""
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return max(0, float(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_task(instance: str, kind: str, raw) -> Torrent:
    """Require an explicit zero-byte remainder and an acceptable provider state."""
    if kind == 'qbittorrent':
        state = str(field(raw, 'state', default='')).lower()
        remaining = field(raw, 'amount_left')
        progress = field(raw, 'progress')
        complete = (remaining is not None and remaining == 0 and progress is not None and progress >= 1
                    and state in {'uploading', 'stalledup', 'forcedup', 'queuedup', 'pausedup', 'stoppedup'})
        return Torrent(instance, str(field(raw, 'hash', default='')), str(field(raw, 'name', default='')),
                       str(field(raw, 'save_path', default='')), complete, epoch(field(raw, 'completion_on')))
    if kind == 'transmission':
        state = str(field(raw, 'status', default='')).lower()
        remaining = field(raw, 'left_until_done', 'leftUntilDone')
        error = field(raw, 'error', default=0)
        complete = remaining is not None and remaining == 0 and error == 0 and state in {'seeding', 'seed_pending', 'stopped', '0', '5', '6'}
        return Torrent(instance, str(field(raw, 'hash_string', 'hashString', default='')),
                       str(field(raw, 'name', default='')), str(field(raw, 'download_dir', 'downloadDir', default='')),
                       complete, epoch(field(raw, 'done_date', 'doneDate')))
    raise UploadError('UNSUPPORTED_DOWNLOADER', False)


def normalize_member(kind: str, raw) -> TorrentFile:
    """Use per-member selection and exact completion data; unknown means wait."""
    name = str(field(raw, 'name', default=''))
    size = field(raw, 'size')
    if kind == 'qbittorrent':
        priority, progress = field(raw, 'priority'), field(raw, 'progress')
        if priority is None:
            raise UploadError('INVALID_FILE_LIST', False)
        selected = priority is not None and priority > 0
        complete = progress is not None and progress >= 1
    else:
        selection = field(raw, 'selected', 'wanted')
        if not isinstance(selection, bool):
            raise UploadError('INVALID_FILE_LIST', False)
        selected = selection is True
        downloaded = field(raw, 'completed', 'bytes_completed')
        complete = size is not None and downloaded is not None and downloaded >= size
    if not name or size is None or size < 0:
        raise UploadError('INVALID_FILE_LIST', False)
    return TorrentFile(name, int(size), selected, complete)


class MPGateway:
    """Translate MP runtime services at the plugin boundary, without credentials."""

    def __init__(self):
        self.cloud = Cloud115()

    def services(self):
        """Enumerate only the two supported configured downloader types."""
        return {name: service for name, service in DownloaderHelper().get_services().items()
                if service.type in {'qbittorrent', 'transmission'}}

    def _service(self, instance):
        service = self.services().get(instance)
        if not service:
            raise UploadError('DOWNLOADER_NOT_AVAILABLE')
        return service

    def tasks(self, instance, hash_string=None):
        """A failed list query cannot initialize a successful empty baseline."""
        service = self._service(instance)
        try:
            raw, error = service.instance.get_torrents(ids=hash_string)
            if error or raw is None:
                raise UploadError('DOWNLOADER_QUERY_FAILED')
            return [normalize_task(instance, service.type, task) for task in raw]
        except UploadError:
            raise
        except Exception:
            raise UploadError('DOWNLOADER_QUERY_FAILED') from None

    def files(self, instance, hash_string):
        """Read raw file counts because the common DTO omits TR completed bytes."""
        service = self._service(instance)
        try:
            raw = service.instance.get_files(hash_string)
            if not raw:
                raise UploadError('FILE_LIST_UNAVAILABLE')
            # Older Transmission clients can return an index-keyed dictionary.
            values = raw.values() if isinstance(raw, dict) else raw
            return [normalize_member(service.type, member) for member in values]
        except UploadError:
            raise
        except Exception:
            raise UploadError('FILE_LIST_UNAVAILABLE') from None

    def ready(self, instance, hash_string, name, size, full_path):
        """Revalidate task selection and location immediately before reading bytes."""
        tasks = self.tasks(instance, hash_string)
        if len(tasks) != 1 or not tasks[0].complete:
            return False
        members = self.files(instance, hash_string)
        member = next((item for item in members if item.name == name), None)
        if not member or not member.selected or not member.complete or member.size != size:
            return False
        actual = str(downloader_path(tasks[0].save_path).joinpath(*Path(name.replace('\\', '/')).parts))
        return downloader_path(actual) == downloader_path(full_path)

    @staticmethod
    def _check_storage(storage):
        if storage not in {'115', '115网盘Plus'}:
            raise UploadError('ONLY_115_SUPPORTED', False)

    def lookup(self, storage, target):
        self._check_storage(storage)
        return self.cloud.lookup(target)

    def folder(self, storage, target, root):
        self._check_storage(storage)
        return self.cloud.folder(target, root)

    def upload(self, folder, path, name, progress=None):
        return self.cloud.upload(folder, path, name, progress=progress)
