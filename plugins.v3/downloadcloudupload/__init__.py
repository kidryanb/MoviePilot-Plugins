"""MoviePilot V3 plugin: copy completed QB/TR downloads into configured clouds."""

from __future__ import annotations

import json
import threading
import time
import sqlite3
from typing import Literal

from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, Field
from fastapi import Depends

from app.sdk.plugin import _PluginBase
from app.sdk.logging import logger
from app.sdk import scheduler as scheduler_sdk
from app.schemas.response import Response
from app.api.endpoints.plugin import get_current_active_superuser

from .core import Engine, Store, LABELS, VIDEO_EXTENSIONS, map_file, worker_lease
from .gateway import MPGateway
from .cloud115 import Cloud115, strm_cookie
from .picker import local_folders, folder_picker
from .organizer import Organizer
from .dashboard import queue_page


class FolderRequest(BaseModel):
    """Authenticated read-only folder browsing."""

    kind: Literal['local', 'cloud']
    path: str = Field(default='', max_length=4096)


class FolderEntry(BaseModel):
    name: str
    path: str


class FolderResult(BaseModel):
    path: str
    parent: str
    folders: list[FolderEntry]


class ActionRequest(BaseModel):
    """Explicit selection for a non-destructive queue operation."""

    action: Literal['retry', 'ignore', 'remap', 'verify', 'restart']
    ids: list[int] = Field(default_factory=list, max_length=100)


class BackfillRequest(BaseModel):
    """Only task identities shown in the last backfill preview may be selected."""

    keys: list[str] = Field(default_factory=list, max_length=100)


class MappingRequest(BaseModel):
    """Read-only mapping preview input."""

    instance: str
    path: str


class ActionResult(BaseModel):
    """Submission or update result."""

    accepted: bool = False
    count: int = 0


class PreviewEntry(BaseModel):
    """Backfill selection summary, with no downloader credentials."""

    key: str
    instance: str
    hash: str
    title: str
    save_path: str
    complete: bool
    completed_at: float
    time_unknown: bool


class MappingResult(BaseModel):
    """Resolved mapping paths."""

    matched: bool
    local: str = ''
    storage: str = ''
    target: str = ''


class DownloadCloudUpload(_PluginBase):
    """Discover completed downloads and upload a durable, source-preserving queue."""

    plugin_name = '下载完成自动上传'
    plugin_desc = '下载完成上传115，可接管 MP 自动整理并监控 STRM 本地目录。'
    plugin_icon = 'cloud.png'
    plugin_version = '0.3.6'
    plugin_author = 'kidryanb'
    author_url = 'https://github.com/kidryanb'
    plugin_config_prefix = 'downloadcloudupload_'
    plugin_order = 50
    auth_level = 1

    def __init__(self):
        """Create per-instance lifecycle locks without accessing external services."""
        super().__init__()
        self._enabled = False
        self._stop = threading.Event()
        self._monitor_lock = threading.Lock()
        self._worker = None
        self._engine = None
        self._error = ''
        self._config = {}
        self._strm_path = ''
        self._page_cache = []

    def init_plugin(self, config=None):
        """Validate configuration and schedule work through the host scheduler."""
        self.stop_service()
        if (self._worker and self._worker.is_alive()) or self._monitor_lock.locked():
            self._error = '上一轮上传尚未结束，请结束后重新保存设置。'
            return
        self._stop = threading.Event()
        self._config = dict(config or {})
        self._config = {key: value for key, value in self._config.items() if not key.startswith('picker_')}
        self._error = ''
        try:
            store = Store(self.get_data_path() / 'queue.sqlite')
            organizer = Organizer(store, getattr(self, 'systemconfig', None),
                                  getattr(self, 'eventmanager', None))
            if not self._config.get('enabled'):
                self._strm_path = organizer.configure([])
            interval = int(self._config.get('interval', 60))
            stable = int(self._config.get('stable_seconds', 10))
            retries = int(self._config.get('retry_limit', 5))
            if not 30 <= interval <= 3600 or not 1 <= stable <= 3600 or not 0 <= retries <= 10:
                raise ValueError('检查周期、稳定等待或重试次数超出范围')
            gateway = MPGateway()
            names = list(self._config.get('downloaders') or [])
            if any(not isinstance(name, str) for name in names):
                raise ValueError('下载器选择格式错误')
            extensions = self._config.get('extensions', sorted(VIDEO_EXTENSIONS))
            excluded = self._config.get('excluded', [])
            if not isinstance(extensions, list) or not extensions or any(not isinstance(ext, str) or not ext.startswith('.') or '/' in ext or '\\' in ext for ext in extensions):
                raise ValueError('视频扩展名必须以点开头')
            if not isinstance(excluded, list) or any(not isinstance(word, str) for word in excluded):
                raise ValueError('排除关键词格式错误')
            self._engine = Engine(store, gateway,
                                  self._config.get('rules') or [], names,
                                  stable_seconds=stable, retry_limit=retries,
                                  sidecars=bool(self._config.get('sidecars')), stopped=self._stop.is_set,
                                  extensions=[ext.lower() for ext in extensions], excluded=excluded)
            self._enabled = bool(self._config.get('enabled'))
            if self._enabled and (not names or not self._engine.rules):
                raise ValueError('请选择下载器并添加文件夹规则')
            if self._enabled:
                path = self._config.get('strm_path', '')
                if not isinstance(path, str):
                    raise ValueError('STRM 本地目录格式错误')
                monitored_rules = [rule for rule in self._engine.rules
                                   if not rule['instance'] or rule['instance'] in names]
                self._strm_path = organizer.configure(monitored_rules, path.strip())
                self._schedule('initial', self.check, delay=3)
            for flag, method in [('check_once', self.check), ('preview_once', self.preview_backfill),
                                 ('backfill_once', self._backfill_config), ('retry_once', self._retry_config)]:
                if self._config.get(flag):
                    self._config[flag] = False
                    self._schedule(flag, method, delay=3)
            self.update_config(self._config)
        except Exception as error:
            self._enabled = False
            self._error = str(error) if isinstance(error, ValueError) else '初始化失败，请核对 MP V3 接口与文件权限。'
            logger.warning('下载完成自动上传：配置不可用')

    def _schedule(self, job_id, function, delay=0):
        """Avoid unmanaged schedulers and keep one-shot jobs instance scoped."""
        return scheduler_sdk.add_plugin_once_job(self.__class__.__name__, job_id, function,
                                                 self.plugin_name, delay_seconds=delay)

    def get_state(self):
        """Expose effective runtime availability."""
        return self._enabled

    @staticmethod
    def get_command():
        """No messaging commands or external callbacks in the first version."""
        return []

    def get_service(self):
        """Declare non-overlapping downloader polling through MP's scheduler."""
        if not self._enabled:
            return []
        return [{'id': f'{self.__class__.__name__}.Check', 'name': self.plugin_name,
                 'trigger': IntervalTrigger(seconds=int(self._config.get('interval', 60))),
                 'func': self.check, 'kwargs': {}}]

    def check(self):
        """Poll even during an upload; one worker serializes cloud writes."""
        if not self._enabled or self._stop.is_set() or not self._engine:
            return
        if not self._monitor_lock.acquire(blocking=False):
            return
        try:
            self._engine.scan()
            if not self._worker or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._upload_loop, name=f'{self.__class__.__name__}-Upload', daemon=True)
                self._worker.start()
        finally:
            self._monitor_lock.release()

    def _upload_loop(self):
        """Drain ready files, then leave delayed work to the next polling cycle."""
        if not self._engine.lock.acquire(blocking=False):
            return
        try:
            with worker_lease(self.get_data_path() / 'worker.lock') as acquired:
                if not acquired:
                    return
                with self._engine.store.connect() as db:
                    db.execute("UPDATE files SET state='verifying',message='RESTART_RECONCILE' WHERE state='uploading'")
                self._engine.process(budget=100)
        except Exception:
            logger.warning('下载完成自动上传：上传队列运行失败')
        finally:
            self._engine.lock.release()

    def stop_service(self):
        """Stop new work; an in-flight SDK upload is allowed to return normally."""
        self._enabled = False
        self._stop.set()
        for job_id in ['initial', 'check_once', 'preview_once', 'backfill_once', 'retry_once', 'manual']:
            scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, job_id)
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)

    def preview_backfill(self) -> Response[list[PreviewEntry]]:
        """List completed tasks without initializing baselines or writing files."""
        if not self._engine or not self._monitor_lock.acquire(blocking=False):
            return Response(success=False, message='监控正在运行，请稍后再试')
        try:
            entries = self._engine.scan(backfill='preview')
            days = int(self._config.get('backfill_days', 0))
            if days:
                entries = [entry for entry in entries if not entry['completed_at'] or entry['completed_at'] >= time.time() - days * 86400]
            self._engine.store.set_meta('backfill_preview', entries)
            return Response(success=True, data=[PreviewEntry(**entry) for entry in entries])
        finally:
            self._monitor_lock.release()

    def api_check(self) -> Response[ActionResult]:
        """Queue a check rather than blocking the API on a cloud transfer."""
        if not self._enabled:
            return Response(success=False, message='请先启用插件')
        accepted = self._schedule('manual', self.check)
        return Response(success=bool(accepted), data=ActionResult(accepted=bool(accepted)))

    def api_backfill(self, payload: BackfillRequest) -> Response[ActionResult]:
        """Persist an explicit selection and submit it to the host scheduler."""
        if not self._enabled or not self._engine:
            return Response(success=False, message='请先启用插件')
        allowed = {entry['key'] for entry in self._engine.store.meta('backfill_preview', [])}
        if not payload.keys or not set(payload.keys) <= allowed:
            return Response(success=False, message='请先预览并选择补传任务')
        self._engine.store.set_meta('backfill_selection', payload.keys)
        accepted = self._schedule('backfill_once', self._backfill_selected)
        return Response(success=bool(accepted), data=ActionResult(accepted=bool(accepted), count=len(payload.keys)))

    def _backfill_config(self):
        """Apply the task selection made in the native configuration page."""
        self.api_backfill(BackfillRequest(keys=self._config.get('backfill_keys') or []))

    def _backfill_selected(self):
        """Re-query selected tasks; completion is checked again by the upload worker."""
        if not self._enabled or not self._monitor_lock.acquire(blocking=False):
            return
        try:
            keys = self._engine.store.meta('backfill_selection', [])
            self._engine.scan(backfill=keys)
            self._engine.store.set_meta('backfill_selection', [])
        finally:
            self._monitor_lock.release()
        self.check()

    def api_action(self, payload: ActionRequest) -> Response[ActionResult]:
        """Serialize user changes with the uploading worker."""
        if not self._engine:
            return Response(success=False, message='请先配置并启用插件')
        if not self._engine.lock.acquire(blocking=False):
            if payload.action == 'restart':
                return self._request_active_restart(payload)
            return Response(success=False, message='上传正在执行，请稍后再试')
        try:
            with worker_lease(self.get_data_path() / 'worker.lock') as acquired:
                if not acquired:
                    if payload.action == 'restart':
                        return self._request_active_restart(payload)
                    return Response(success=False, message='旧上传线程仍在运行，本次操作未执行。重置插件不会中止上传，请等当前上传结束。')
                count = self._engine.action(payload.ids, payload.action, allow_stale_upload=True)
                if not count:
                    message = ('未重新上传：任务已完成、远端已有文件或任务已失效，请先核对并刷新。'
                               if payload.action == 'restart' else '任务状态不允许此操作，或没有可处理的文件，请刷新。')
                    return Response(success=False, message=message, data=ActionResult(accepted=False, count=0))
                self._schedule('manual', self.check, delay=1)
                return Response(success=True, message=f'已处理 {count} 个文件',
                                data=ActionResult(accepted=True, count=count))
        except Exception:
            return Response(success=False, message='操作未完成，请检查115连接后刷新任务状态。')
        finally:
            self._engine.lock.release()

    def _request_active_restart(self, payload):
        """Request cooperative cancellation even when another plugin instance owns the lease."""
        if len(set(payload.ids)) != 1:
            return Response(success=False, message='上传中的任务请逐个停止并重新上传')
        try:
            accepted = self._engine.request_restart(payload.ids[0])
        except Exception:
            return Response(success=False, message='停止请求未保存，请刷新任务状态后重试')
        if not accepted:
            return Response(success=False, message='当前上传线程不支持安全停止或已结束，未执行重新上传。请刷新；旧上传线程需等待上传完成。')
        return Response(success=True, message='已请求停止，线程退出并核对远端后会重新排队。',
                        data=ActionResult(accepted=True, count=1))

    def _retry_config(self):
        """Run a selected operation from the native form."""
        self.api_action(ActionRequest(ids=self._config.get('action_ids') or [],
                                      action=self._config.get('file_action') or 'retry'))

    def api_mapping(self, payload: MappingRequest) -> Response[MappingResult]:
        """Preview an input path without opening or uploading its contents."""
        try:
            mapping = map_file(payload.instance, payload.path, self._engine.rules if self._engine else [])
            return Response(success=True, data=MappingResult(matched=bool(mapping), **{
                key: mapping[key] for key in ('local', 'storage', 'target') if mapping}))
        except ValueError:
            return Response(success=False, message='路径无效或位于来源文件夹之外')

    def get_api(self):
        """Declare authenticated, schema-checked native-page operations."""
        return [{'path': path, 'endpoint': endpoint, 'methods': [method], 'auth': 'bear',
                 'dependencies': [Depends(get_current_active_superuser)],
                 'summary': summary, 'response_model': model}
                for path, endpoint, method, summary, model in [
                    ('/check', self.api_check, 'POST', '立即检查下载器', Response[ActionResult]),
                    ('/preview', self.preview_backfill, 'POST', '预览已完成任务', Response[list[PreviewEntry]]),
                    ('/backfill', self.api_backfill, 'POST', '补传所选任务', Response[ActionResult]),
                    ('/action', self.api_action, 'POST', '处理所选文件', Response[ActionResult]),
                    ('/mapping', self.api_mapping, 'POST', '测试文件夹规则', Response[MappingResult]),
                    ('/status', self.api_status, 'GET', '刷新上传任务状态', Response[ActionResult]),
                    ('/confirmation', self.api_confirmation, 'POST', '确认重新上传', Response[ActionResult]),
                    ('/folders', self.api_folders, 'POST', '浏览本地或115目录', Response[FolderResult]),
                ]]

    def api_folders(self, payload: FolderRequest) -> Response[FolderResult]:
        """Never return file contents, account IDs or credentials."""
        try:
            if '\x00' in payload.path:
                raise ValueError('INVALID_PATH')
            result = (Cloud115().browse(payload.path or '/') if payload.kind == 'cloud'
                      else local_folders(payload.path))
            return Response(success=True, data=FolderResult(**result))
        except Exception:
            return Response(success=False, message='无法读取目录，请检查路径、目录权限和 STRM 助手授权。')

    def get_form(self):
        """Native folder editor supports adding and deleting arbitrary mappings."""
        gateway = MPGateway()
        try:
            services = list(gateway.services())
        except Exception:
            services = []
        try:
            strm_cookie()
            auth_status = '已读取115 STRM助手的 Cookie；上传时会读取最新配置。'
            auth_ready = True
        except Exception:
            auth_status = '未找到115 STRM助手的完整 Cookie，请先在 STRM助手中登录并保存设置。'
            auth_ready = False
        preview = self._engine.store.meta('backfill_preview', []) if self._engine else []
        def control(component, model, label, **props):
            if component == 'VSwitch':
                return {'component': component, 'props': {'model': model, 'label': label,
                        'hide-details': True, 'inset': True, **props}}
            return {'component': 'div', 'content': [
                {'component': 'div', 'props': {'class': 'text-body-2 mb-2',
                 'style': {'whiteSpace': 'normal', 'lineHeight': '1.5', 'overflowWrap': 'anywhere'}}, 'text': label},
                {'component': component, 'props': {'model': model, 'aria-label': label,
                 'variant': 'outlined', 'density': 'comfortable', 'hide-details': 'auto', **props}},
            ]}
        select_rule = '''function(index) {
            rule_index=index;
            const r = rules[index]; if (!r) return;
            rule_name=r.name; rule_instance=r.instance; rule_source=r.source;
            rule_local=r.local; rule_target=r.target; rule_enabled=r.enabled;
        }'''
        apply_rule = '''function() {
            const r={name:rule_name,instance:rule_instance || '',source:rule_source,
              local:rule_local,storage:'115',target:rule_target,enabled:rule_enabled};
            if (!r.source || !r.local || !r.target) return;
            const copy=[...rules];
            if (rule_index === null || rule_index === undefined) { copy.push(r); rule_index=copy.length-1; }
            else { copy[rule_index]=r; }
            rules=copy;
        }'''
        reset_rule = '''function() {rule_index=null;rule_name='';rule_instance='';rule_source='';
            rule_local='';rule_target='';rule_enabled=true;}'''
        defaults = {'enabled': False, 'strm_path': '', 'downloaders': [], 'interval': 60, 'stable_seconds': 10,
                    'retry_limit': 5, 'sidecars': False, 'extensions': sorted(VIDEO_EXTENSIONS), 'excluded': [],
                    'backfill_days': 0, 'rules': [], 'rule_index': None,
                    'rule_name': '', 'rule_instance': '', 'rule_source': '', 'rule_local': '',
                    'rule_target': '', 'rule_enabled': True,
                    'check_once': False, 'preview_once': False, 'backfill_once': False,
                    'backfill_keys': [], 'action_ids': [], 'file_action': 'retry', 'retry_once': False}
        local_button, cloud_button, strm_button, picker_dialog, picker_defaults = folder_picker(self.__class__.__name__)
        defaults.update(picker_defaults)
        content = [
            {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
             'text': '复制上传并保留做种文件。首次启用跳过已有完成任务；旧任务需先预览再选择补传。'}},
            {'component': 'VAlert', 'props': {'type': 'info' if auth_ready else 'warning',
             'variant': 'tonal', 'title': '复用115 STRM助手授权', 'text': auth_status}},
            {'component': 'VBtn', 'props': {'href': '/plugins', 'target': '_blank', 'rel': 'noopener',
             'variant': 'tonal'}, 'text': '打开115 STRM助手配置（我的插件）'},
            control('VSwitch', 'enabled', '启用插件'),
            {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
             'text': '填写 STRM 本地目录后，启用即接管自动整理：原目录继续用于下载及上传，MP 改为监控 STRM；关闭插件或清空此项恢复原设置。分类、重命名和刮削沿用 MP 目录设置。留空仅上传。'}},
            control('VTextField', 'strm_path', 'STRM 本地目录（115 STRM 助手输出目录）', placeholder='/media/strm'),
            strm_button,
            control('VSelect', 'downloaders', '监控下载器', items=services, multiple=True, chips=True),
            control('VTextField', 'interval', '检查周期（秒）', type='number', min=30, max=3600),
            control('VTextField', 'stable_seconds', '文件稳定等待（秒）', type='number', min=1, max=3600),
            control('VTextField', 'retry_limit', '自动重试次数', type='number', min=0, max=10),
            control('VSwitch', 'sidecars', '附带上传任务内的字幕及音轨'),
            control('VCombobox', 'extensions', '视频扩展名', items=sorted(VIDEO_EXTENSIONS), multiple=True, chips=True),
            control('VCombobox', 'excluded', '排除路径关键词（输入后回车）', multiple=True, chips=True),
            {'component': 'VDivider', 'props': {'class': 'my-4'}},
            {'component': 'VAlert', 'props': {'type': 'info', 'text': '填写文件夹后点击“加入或更新规则”，最后保存插件设置。网盘目标根文件夹须已存在。'}},
            control('VSelect', 'rule_index', '选择已有规则', clearable=True,
                    items="{{ rules.map((r,i) => ({title:r.name || r.source,value:i})) }}",
                    **{'onUpdate:modelValue': select_rule}),
            control('VTextField', 'rule_name', '规则名称'),
            control('VSelect', 'rule_instance', '适用下载器', items=[{'title': '全部所选下载器', 'value': ''}] +
                    [{'title': name, 'value': name} for name in services]),
            control('VTextField', 'rule_source', '下载器保存文件夹', placeholder='/data/tv'),
            control('VTextField', 'rule_local', 'MP 可读取文件夹', placeholder='/downloads/tv'),
            local_button,
            control('VTextField', 'rule_target', '115目标文件夹', placeholder='/影视/电视剧'),
            cloud_button,
            picker_dialog,
            control('VSwitch', 'rule_enabled', '启用该规则'),
            {'component': 'VBtn', 'props': {'onClick': apply_rule, 'class': 'ma-1'}, 'text': '加入或更新规则'},
            {'component': 'VBtn', 'props': {'onClick': reset_rule, 'class': 'ma-1', 'variant': 'outlined'}, 'text': '新增规则'},
            {'component': 'VBtn', 'props': {'onClick': "function() {if(rule_index !== null){rules=rules.filter((r,i)=>i!==rule_index);rule_index=null;}}",
                                          'class': 'ma-1', 'variant': 'outlined'}, 'text': '删除所选规则'},
            control('VSwitch', 'check_once', '保存后立即检查一次'),
            control('VSelect', 'backfill_days', '补传预览时间范围', items=[
                {'title': '全部已完成任务', 'value': 0}, {'title': '最近 7 天', 'value': 7}, {'title': '最近 30 天', 'value': 30}]),
            control('VSwitch', 'preview_once', '保存后预览已有完成任务（不上传）'),
            control('VSelect', 'backfill_keys', '选择补传任务（预览后重新打开设置）', multiple=True, chips=True,
                    items=[{'title': f"{entry['instance']}：{entry['title']}" + ('（完成时间未知）' if entry['time_unknown'] else ''),
                            'value': entry['key']} for entry in preview]),
            control('VSwitch', 'backfill_once', '保存后补传所选任务'),
        ]
        return [{'component': 'VForm', 'content': [
            {'component': 'VRow', 'props': {'class': 'ma-0'}, 'content': [
                {'component': 'VCol', 'props': {'cols': 12, 'class': 'px-0 py-3'}, 'content': [item]}
                for item in content
            ]}
        ]}], defaults

    def get_page(self):
        """Show upload progress and pending work, with actionable failures kept visible."""
        page = []
        if self._error:
            page.append({'component': 'VAlert', 'props': {'type': 'error', 'text': self._error}})
        if not self._engine:
            if not self._error:
                page.append({'component': 'div', 'props': {'class': 'text-center pa-6'}, 'text': '请先配置并启用插件'})
            return page
        if self._engine.lock.locked():
            worker_busy = True
        else:
            with worker_lease(self.get_data_path() / 'worker.lock') as acquired:
                worker_busy = not acquired
        try:
            snapshot = self._engine.store.page_snapshot()
            self._page_cache = queue_page(snapshot, self.__class__.__name__, worker_busy=worker_busy)
        except sqlite3.OperationalError:
            page.append({'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'},
                         'text': '任务状态正在更新，请稍后刷新。'})
        page.extend(self._page_cache)
        return page

    def api_status(self) -> Response[ActionResult]:
        """Let the native page emit its refresh event without starting cloud work."""
        return Response(success=True, data=ActionResult(accepted=True))

    def api_confirmation(self, payload: ActionRequest) -> Response[ActionResult]:
        """Display a second explicit restart button; this request never starts work."""
        if not self._engine or payload.action != 'restart':
            return Response(success=False, message='请先配置插件')
        count = 0
        for file_id in set(payload.ids):
            if self._engine.store.rows('SELECT id FROM files WHERE id=?', (file_id,)):
                self._engine.store.set_meta('restart_confirmation:' + str(file_id), time.time() + 60)
                count += 1
        return Response(success=bool(count), data=ActionResult(accepted=bool(count), count=count))
