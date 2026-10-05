"""Compact upload queue built from the plugin's durable file states."""

import time
from pathlib import PurePosixPath


ACTIVE = {'uploading', 'verifying'}
PENDING = {'queued', 'waiting_complete', 'waiting_source', 'retry_wait'}
FINISHED = {'success', 'already_exists'}
ATTENTION = {'failed', 'conflict'}
STATUS = {'uploading': '上传中', 'verifying': '确认上传结果', 'queued': '等待上传',
          'waiting_complete': '等待下载完成', 'waiting_source': '等待本地文件',
          'retry_wait': '等待重试', 'failed': '上传失败', 'conflict': '文件冲突'}


def size(value):
    """Format bytes without exposing local or cloud paths."""
    value = max(0, float(value))
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}'
        value /= 1024


def queue_page(store):
    """Show active and pending tasks; old baselines and completed tasks stay hidden."""
    groups = {'active': [], 'pending': [], 'attention': []}
    tasks = store.rows('''SELECT t.* FROM torrents t WHERE EXISTS (
        SELECT 1 FROM files f WHERE f.task_key=t.key AND f.state IN
        ('uploading','verifying','queued','waiting_complete','waiting_source','retry_wait','failed','conflict'))
        OR (t.state='waiting_complete' AND NOT EXISTS (
        SELECT 1 FROM files f WHERE f.task_key=t.key)) ORDER BY t.updated DESC''')
    for task in tasks:
        files = store.rows('SELECT * FROM files WHERE task_key=? ORDER BY id', (task['key'],))
        visible = [row for row in files if row['state'] in ACTIVE | PENDING | ATTENTION]
        group = ('active' if any(row['state'] in ACTIVE for row in visible) else
                 'pending' if not files or any(row['state'] in PENDING for row in visible) else 'attention')
        done = sum(row['state'] in FINISHED for row in files)
        content = [{'component': 'VCardTitle', 'props': {'class': 'text-body-1 font-weight-bold',
                    'title': task['title'], 'style': {'overflow': 'hidden', 'textOverflow': 'ellipsis'}},
                    'text': task['title']}]
        details = []
        if len(files) > 1:
            details.append({'component': 'div', 'props': {'class': 'text-caption mb-2'},
                            'text': f'{done} / {len(files)} 个文件已上传'})
        if not files:
            details.append({'component': 'div', 'text': '等待下载完成'})
        for row in visible:
            if len(files) > 1:
                details.append({'component': 'div', 'props': {'class': 'text-caption text-truncate'},
                                'text': PurePosixPath(row['name'].replace('\\', '/')).name})
            state = row['state']
            text = STATUS[state]
            if state == 'uploading':
                progress = store.meta('upload_progress:' + str(row['id']), {}) or {}
                known = progress.get('total') == row['size'] and row['size'] > 0 and 'sent' in progress
                sent = max(0, min(row['size'], progress.get('sent', 0))) if known else 0
                percent = min(100, sent * 100 / row['size']) if known else 0
                if known:
                    text += f' · {percent:.1f}% · {size(sent)} / {size(row["size"])}'
                    if time.time() - progress.get('updated', 0) < 5 and progress.get('speed', 0) > 0:
                        text += f' · {size(progress["speed"])}/s'
                else:
                    text += ' · ' + size(row['size'])
                details.append({'component': 'div', 'props': {'class': 'text-caption mb-2'}, 'text': text})
                details.append({'component': 'VProgressLinear', 'props': {'model-value': percent,
                                'indeterminate': not known, 'height': 6, 'rounded': True, 'color': 'primary'}})
            else:
                details.append({'component': 'div', 'props': {'class': 'text-caption'}, 'text': text})
                if state == 'verifying':
                    details.append({'component': 'VProgressLinear', 'props': {'indeterminate': True,
                                    'height': 6, 'rounded': True, 'color': 'primary'}})
        content.append({'component': 'VCardText', 'props': {'class': 'pt-0'}, 'content': details})
        groups[group].append({'component': 'VCard', 'props': {'class': 'mb-3', 'variant': 'tonal'},
                              'content': content})
    page = [{'component': 'div', 'props': {'class': 'd-flex align-center justify-space-between mb-3'},
             'content': [{'component': 'div', 'text': f'上传中 {len(groups["active"])} · 待上传 {len(groups["pending"])}'},
                         {'component': 'VBtn', 'props': {'variant': 'text', 'size': 'small',
                          'prepend-icon': 'mdi-refresh', 'onClick': 'function(){window.location.reload();}'},
                          'text': '刷新'}]}]
    for key, title in [('active', '上传中'), ('pending', '待上传'), ('attention', '需处理')]:
        if groups[key]:
            page.append({'component': 'div', 'props': {'class': 'text-subtitle-2 mb-2'}, 'text': title})
            page.extend(groups[key])
    if not tasks:
        page.append({'component': 'div', 'props': {'class': 'text-center pa-6'}, 'text': '暂无待上传任务'})
    return page
