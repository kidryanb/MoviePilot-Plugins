"""Read-only host directory browsing and native MoviePilot folder dialog."""

import os
from pathlib import Path


def local_folders(value):
    """Browse the MP host, never the user's browser computer."""
    path = Path(value or Path.cwd().anchor)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('INVALID_LOCAL_PATH')
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError('LOCAL_FOLDER_MISSING')
    with os.scandir(path) as entries:
        folders = [{'name': entry.name, 'path': str(path / entry.name)}
                   for entry in entries if entry.is_dir(follow_symlinks=False)]
    return {'path': str(path), 'parent': str(path.parent),
            'folders': sorted(folders, key=lambda item: item['name'].casefold())}


def folder_picker(plugin_id):
    """Share one dialog across local and cloud fields; use the host's API client."""
    load = '''async function(path) {
        if (picker_loading) return;
        picker_loading=true; picker_error=''; picker_valid=false; picker_items=[];
        const serial=++picker_serial;
        try {
            if (!window.MoviePilotAPI) throw new Error('HOST_API_UNAVAILABLE');
            const response=await window.MoviePilotAPI.post('plugin/PLUGIN_ID/folders',
                {kind:picker_kind,path:path || ''},{timeout:120000,feedback:'silent'});
            if (serial !== picker_serial || !picker_open) return;
            if (!response.success || !response.data) {
                picker_error=response.message || '读取目录失败'; return;
            }
            picker_path=response.data.path; picker_parent=response.data.parent;
            picker_items=response.data.folders; picker_page=0; picker_valid=true;
        } catch(error) {
            if (serial === picker_serial && picker_open)
                picker_error='读取目录失败，请检查登录状态、STRM 授权或目录权限。';
        } finally {if (serial === picker_serial) picker_loading=false;}
    }'''.replace('PLUGIN_ID', plugin_id)
    cancel = "function(){picker_open=false;picker_serial++;picker_loading=false;picker_valid=false;}"

    def button(kind, target, title):
        start = '/' if kind == 'cloud' else ''
        callback = ("function(){picker_target='" + target + "';picker_kind='" + kind + "';picker_open=true;picker_loading=false;"
                    "picker_valid=false;picker_path='';picker_parent='';picker_serial++;(" + load + ")("
                    + target + " || '" + start + "');}")
        return {'component': 'VBtn', 'props': {'onClick': callback, 'variant': 'tonal',
                'prepend-icon': 'mdi-folder-open'}, 'text': title}

    dialog = {'component': 'VDialog', 'props': {'model': 'picker_open', 'max-width': 760,
              'persistent': True, 'scrollable': True}, 'content': [
        {'component': 'VCard', 'content': [
            {'component': 'VCardTitle', 'props': {'class': 'text-wrap'}, 'text': '选择文件夹'},
            {'component': 'VCardText', 'content': [
                {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'class': 'mb-4',
                 'text': "{{ picker_kind === 'cloud' ? '读取 STRM 助手授权的网盘目录。' : '目录来自 MoviePilot 主机或容器；下载器路径不同仍需手动填写。' }}"}},
                {'component': 'div', 'props': {'class': 'text-body-2 mb-2'}, 'text': '当前路径'},
                {'component': 'VTextField', 'props': {'model': 'picker_path', 'readonly': True,
                 'aria-label': '当前路径', 'variant': 'outlined', 'hide-details': True}},
                {'component': 'VBtn', 'props': {'onClick': 'function(){(' + load + ')(picker_parent);}',
                 'disabled': '{{ picker_loading || !picker_valid || picker_path === picker_parent }}',
                 'class': 'my-3', 'variant': 'text', 'prepend-icon': 'mdi-arrow-up'}, 'text': '返回上级'},
                {'component': 'VBtn', 'props': {'onClick': 'function(){(' + load + ")(picker_kind === 'cloud' ? '/' : '');}",
                 'disabled': '{{ picker_loading }}', 'variant': 'text'}, 'text': '根目录'},
                {'component': 'VProgressLinear', 'props': {'indeterminate': True, 'show': 'picker_loading'}},
                {'component': 'VAlert', 'props': {'type': 'error', 'show': '!!picker_error',
                 'text': '{{ picker_error }}'}},
                {'component': 'div', 'props': {'show': 'picker_valid && !picker_items.length',
                 'class': 'pa-4'}, 'text': '此目录没有子文件夹，可以选择当前目录。'},
                {'component': 'VList', 'props': {'style': {'maxHeight': '360px', 'overflowY': 'auto'}},
                 'content': [
                     {'component': 'VListItem', 'props': {
                         'show': f'!!picker_items[picker_page*20+{index}]',
                         'title': '{{ picker_items[picker_page*20+' + str(index) + ']?.name || "" }}',
                         'prepend-icon': 'mdi-folder', 'disabled': '{{ picker_loading }}',
                         'onClick': 'function(){const item=picker_items[picker_page*20+' + str(index) + '];if(item)(' + load + ')(item.path);}',
                     }} for index in range(20)
                 ]},
                {'component': 'VBtn', 'props': {'show': 'picker_items.length > 20',
                 'disabled': '{{ picker_loading || picker_page === 0 }}', 'variant': 'text',
                 'onClick': 'function(){picker_page--;}'}, 'text': '上一页'},
                {'component': 'VBtn', 'props': {'show': 'picker_items.length > 20',
                 'disabled': '{{ picker_loading || (picker_page+1)*20 >= picker_items.length }}', 'variant': 'text',
                 'onClick': 'function(){picker_page++;}'}, 'text': '下一页'},
            ]},
            {'component': 'VCardActions', 'content': [
                {'component': 'VSpacer'},
                {'component': 'VBtn', 'props': {'disabled': '{{ picker_loading || !picker_valid }}',
                 'color': 'primary', 'onClick': "function(){if(!picker_valid || picker_loading)return;if(picker_target==='strm_path')strm_path=picker_path;else if(picker_kind==='cloud')rule_target=picker_path;else rule_local=picker_path;picker_open=false;picker_valid=false;}"},
                 'text': '选择当前目录'},
                {'component': 'VBtn', 'props': {'onClick': cancel}, 'text': '取消'},
            ]},
        ]},
    ]}
    defaults = {'picker_open': False, 'picker_kind': 'cloud', 'picker_target': 'rule_target', 'picker_loading': False,
                'picker_valid': False, 'picker_path': '', 'picker_parent': '', 'picker_items': [],
                'picker_error': '', 'picker_serial': 0, 'picker_page': 0}
    return (button('local', 'rule_local', '选择本地文件夹'),
            button('cloud', 'rule_target', '选择网盘文件夹'),
            button('local', 'strm_path', '选择 STRM 本地目录'), dialog, defaults)
