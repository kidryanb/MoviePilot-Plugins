"""Compact upload queue built from the plugin's durable file states."""

import time
import json
from pathlib import PurePosixPath


ACTIVE = {'uploading'}
VERIFYING = {'verifying'}
PENDING = {'queued', 'waiting_complete', 'waiting_source', 'retry_wait'}
FINISHED = {'success', 'already_exists'}
ATTENTION = {'failed', 'conflict'}
STATUS = {'uploading': '上传中', 'verifying': '确认上传结果', 'queued': '等待上传',
          'waiting_complete': '等待下载完成', 'waiting_source': '等待本地文件',
          'retry_wait': '等待重试', 'failed': '上传失败', 'conflict': '文件冲突'}


def action_button(plugin_id, file_id, action, title, disabled=False):
    """Submit one visible file and report rejected operations instead of hiding them."""
    confirmation = ''
    if action == 'restart':
        confirmation = "if(!window.confirm('将请求停止当前传输，线程退出后先检查远端，再从零重新上传。校验阶段可能需要等待。是否继续？'))return;"
    callback = ("async function(){" + confirmation + "try{"
                "if(!window.MoviePilotAPI)throw new Error('HOST_API_UNAVAILABLE');"
                f"const response=await window.MoviePilotAPI.post('plugin/{plugin_id}/action',"
                f"{{action:'{action}',ids:[{int(file_id)}]}},{{feedback:'silent'}});"
                "if(!response.success || !response.data?.accepted){"
                "window.alert(response.message || '操作未执行，请刷新后重试');return;}"
                "window.location.reload();"
                "}catch(error){window.alert(error.response?.data?.message || error.data?.message || '操作未完成，请刷新任务状态后重试');}}")
    return {'component': 'VBtn', 'props': {'variant': 'text', 'size': 'small',
            'disabled': disabled, 'onClick': callback}, 'text': title}


def size(value):
    """Format bytes without exposing local or cloud paths."""
    value = max(0, float(value))
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}'
        value /= 1024


def queue_page(store, plugin_id='DownloadCloudUpload', worker_busy=False):
    """Show active and pending tasks; old baselines and completed tasks stay hidden."""
    groups = {'active': [], 'verifying': [], 'pending': [], 'attention': []}
    tasks = store.rows('''SELECT t.* FROM torrents t WHERE EXISTS (
        SELECT 1 FROM files f WHERE f.task_key=t.key AND f.state IN
        ('uploading','verifying','queued','waiting_complete','waiting_source','retry_wait','failed','conflict'))
        OR (t.state='waiting_complete' AND NOT EXISTS (
        SELECT 1 FROM files f WHERE f.task_key=t.key)) ORDER BY t.updated DESC''')
    for task in tasks:
        files = store.rows('SELECT * FROM files WHERE task_key=? ORDER BY id', (task['key'],))
        visible = [row for row in files if row['state'] in ACTIVE | VERIFYING | PENDING | ATTENTION]
        group = ('active' if any(row['state'] in ACTIVE for row in visible) else
                 'verifying' if any(row['state'] in VERIFYING for row in visible) else
                 'pending' if not files or any(row['state'] in PENDING for row in visible) else 'attention')
        done = sum(row['state'] in FINISHED for row in files)
        content = [{'component': 'VCardTitle', 'props': {'class': 'text-body-1 font-weight-bold',
                    'title': task['title'], 'style': {'overflow': 'hidden', 'textOverflow': 'ellipsis'}},
                    'text': task['title']}]
        details = []
        if len(files) > 1:
            details.append({'component': 'div', 'props': {'class': 'text-caption mb-2'},
                            'text': f'{done} / {len(files)} 个文件已确认'})
        if not files:
            details.append({'component': 'div', 'text': '等待下载完成'})
        for row in visible:
            if len(files) > 1:
                details.append({'component': 'div', 'props': {'class': 'text-caption text-truncate'},
                                'text': PurePosixPath(row['name'].replace('\\', '/')).name})
            state = row['state']
            text = STATUS[state]
            progress = store.meta('upload_progress:' + str(row['id']), {}) or {}
            request = store.meta('restart_request:' + str(row['id']), {}) or {}
            stopping = bool(request and request.get('attempt') == progress.get('attempt'))
            legacy_busy = state == 'uploading' and worker_busy and not progress.get('can_stop')
            if state == 'uploading':
                preparing = progress.get('phase') == 'preparing'
                known = progress.get('total') == row['size'] and row['size'] > 0 and 'sent' in progress
                sent = max(0, min(row['size'], progress.get('sent', 0))) if known else 0
                percent = min(100, sent * 100 / row['size']) if known else 0
                if preparing:
                    text = '准备上传（文件校验）'
                elif not known:
                    text = '上传中（当前线程未提供进度）'
                if stopping:
                    text = '正在停止，随后重新上传'
                if known:
                    text += f' · {percent:.1f}% · {size(sent)} / {size(row["size"])}'
                    if time.time() - progress.get('updated', 0) < 5 and progress.get('speed', 0) > 0:
                        text += f' · {size(progress["speed"])}/s'
                else:
                    text += ' · ' + size(row['size'])
                details.append({'component': 'div', 'props': {'class': 'text-caption mb-2'}, 'text': text})
                details.append({'component': 'VProgressLinear', 'props': {'model-value': percent,
                                'indeterminate': not known or preparing, 'height': 6, 'rounded': True, 'color': 'primary'}})
            else:
                percent = None
                if state == 'verifying':
                    try:
                        receipt = json.loads(row['receipt']) if row['receipt'] else {}
                    except (ValueError, TypeError):
                        receipt = {}
                    submitted = (isinstance(receipt, dict) and receipt.get('id')
                                 and receipt.get('size') == row['size'] and bool(row['attempted']))
                    known = progress.get('total') == row['size'] and row['size'] > 0 and 'sent' in progress
                    if submitted:
                        percent = 100
                        text = '上传提交完成 · 100% · 等待115核对'
                    elif known:
                        sent = max(0, min(row['size'], progress['sent']))
                        percent = sent * 100 / row['size']
                        text = f'结果待核对 · 已发送 {percent:.1f}% · {size(sent)} / {size(row["size"])}'
                    else:
                        text = '上传结果待核对（尚无可靠进度）'
                if stopping:
                    text = '正在核对远端，等待重新上传'
                details.append({'component': 'div', 'props': {'class': 'text-caption'}, 'text': text})
                if percent is not None:
                    details.append({'component': 'VProgressLinear', 'props': {'indeterminate': False,
                                    'model-value': percent, 'height': 6, 'rounded': True, 'color': 'primary'}})
            details.append({'component': 'div', 'props': {'class': 'd-flex flex-wrap mt-2 mb-2'},
                            'content': [action_button(plugin_id, row['id'], action,
                                        ('等待旧上传结束' if legacy_busy else '停止并重新上传')
                                        if action == 'restart' and state == 'uploading' else title,
                                        disabled=stopping or (legacy_busy and action == 'restart')
                                        or (state == 'uploading' and action != 'restart')
                                        or (action == 'remap' and bool(row['attempted'])))
                                        for action, title in [('retry', '重试'), ('verify', '核对'),
                                        ('remap', '重新匹配'), ('restart', '重新上传'), ('ignore', '忽略')]]})
        content.append({'component': 'VCardText', 'props': {'class': 'pt-0'}, 'content': details})
        groups[group].append({'component': 'VCard', 'props': {'class': 'mb-3', 'variant': 'tonal'},
                              'content': content})
    page = [{'component': 'div', 'props': {'class': 'd-flex align-center justify-space-between mb-3'},
             'content': [{'component': 'div', 'text': f'上传中 {len(groups["active"])} · 待上传 {len(groups["pending"])}'
                          + (f' · 待核对 {len(groups["verifying"])}' if groups['verifying'] else '')},
                         {'component': 'VBtn', 'props': {'variant': 'text', 'size': 'small',
                          'prepend-icon': 'mdi-refresh', 'onClick': 'function(){window.location.reload();}'},
                          'text': '刷新'}]}]
    for key, title in [('active', '上传中'), ('verifying', '等待核对'), ('pending', '待上传'), ('attention', '需处理')]:
        if groups[key]:
            page.append({'component': 'div', 'props': {'class': 'text-subtitle-2 mb-2'}, 'text': title})
            page.extend(groups[key])
    if not tasks:
        page.append({'component': 'div', 'props': {'class': 'text-center pa-6'}, 'text': '暂无待上传任务'})
    return page
