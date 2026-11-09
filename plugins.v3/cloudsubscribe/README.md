# CloudSubscribe 源码目录

本目录 fork 自 [odomu/MoviePilot-Plugins](https://github.com/odomu/MoviePilot-Plugins) 的 `plugins.v2/cloudsubscribe`，上游提交 `876462067593d4fc2f7bc038f2f73d3eaf099b76`（2026-11-05，上游版本 v1.7.7，提交说明「更新版本v1.7.7」）。

相对上游的改动（0.1.0）：

- 适配 MP V3：插件入口改为 `app.sdk.plugin._PluginBase`，日志、配置、事件、缓存、媒体、网络等常用导入改为 `app.sdk.*`；其余没有 SDK 对应入口的旧导入（如 `app.db.*_oper`）仍由宿主兼容层承接
- 依赖清单从 `requirements.txt` 改为 V3 推荐的 `pyproject.toml`，去掉宿主已提供的 Pillow
- 新增 `core/coexist.py`：与本仓库「下载完成自动上传」(DownloadCloudUpload) 共存的规则，见下文
- 空 115 Cookie 不再注册 115 驱动
- 前端构建输出改到本目录 `dist/assets`，构建产物随源码提交

0.2.0 起新增同盘优先（M1）：`core/drive_routing.py` 决定一条分享落到哪个盘，`handlers/sync/drive_scope.py` 在转存期间把 SyncHandler 的网盘相关属性切到那个盘。细节见规划文档 M1 一节。

许可为 GPL-3.0-only，全文在本目录 `LICENSE`。后续修改仍须保留该协议与上述来源。本仓库里的 DownloadCloudUpload 继续使用仓库根目录的 MIT 许可，两套代码不要混用许可。

放置位置按本仓库现有约定：`plugins.v3/cloudsubscribe`，并在 `package.v3.json` 登记。改造范围、对应的函数和配置项见 [docs/cloudsubscribe-fork-plan.md](../../docs/cloudsubscribe-fork-plan.md)。

## 与「下载完成自动上传」共存

两个插件装在同一个 MoviePilot 里时，分工是：下载器任务上传 115 由 DownloadCloudUpload 负责，本插件只做网盘分享转存。`core/coexist.py` 保证：

- **PT 洗版上传固定关闭**（`enable_pt_upgrade`），避免同一批下载文件被两个插件各传一次
- **新装默认不接管**：订阅筛选默认「仅处理所选订阅」且列表为空；平台下载策略默认 `allow`；指定模式下不做全局平台搜索拦截。没勾选的订阅照常走下载器
- **115 Cookie 默认复用 P115StrmHelper**（只读，和 DownloadCloudUpload 一样），复用时禁止在本插件扫码 115，避免把对方会话挤下线。可改为「本插件自己登录」
- **115 转存目录不能与 DownloadCloudUpload 已启用规则的上传目录重叠**，重叠时本次运行停用 115 并在日志说明原因
- **默认不生成 STRM**（`strm_generate_enabled=false`），115 的 STRM 继续由 P115StrmHelper 负责

本目录是 `CloudSubscribe` 的后端源码。面向用户的安装、配置和功能说明见
[网盘订阅助手使用说明](../../docs/cloudsubscribe.md)（同样来自上述上游提交，正文未改）。前端源码在 [`frontend/cloudsubscribe`](../../frontend/cloudsubscribe)，同样带有 GPL-3.0 的 `LICENSE`。

## 目录职责

```text
cloudsubscribe/
├── __init__.py          # 插件入口、元数据及生命周期代理
├── pyproject.toml       # Python 运行依赖（V3 依赖清单）
├── core/                # 领域模型、配置、平台适配和通用服务
├── drive/               # 网盘能力接口及提供方实现
├── handlers/            # 搜索、订阅、同步、通知和 API 编排
├── search/              # 搜索源客户端
├── utils/               # 文件解析、匹配、Magnet 和 STRM 工具
└── dist/assets/         # Vue 模块联邦构建产物（随源码提交）
```

## 模块边界

- `core/` 定义插件自身业务模型与服务，不放具体网盘或搜索源实现。
- `drive/` 通过稳定能力协议接入网盘；调用方先检查能力，再获取对应服务。
- `search/` 只负责获取和标准化资源候选，不执行转存或文件处理。
- `handlers/` 组合领域服务并对接事件、API、通知和任务入口。
- `utils/` 仅保留无状态或低状态的通用辅助逻辑。

## 前端与发布

前端源码位于 [`frontend/cloudsubscribe`](../../frontend/cloudsubscribe)。
上游用 `plugins-release.yml` 在发布时把构建产物打进插件 ZIP。本仓库不走 Release，构建产物直接提交在本目录 `dist/assets`（`.gitignore` 对这个目录单独放行）。改了前端后需要重新构建：

```bash
cd frontend/cloudsubscribe
pnpm install --frozen-lockfile
pnpm build   # 输出到 plugins.v3/cloudsubscribe/dist/assets
```

在本仓库改版本时同时更新：

- `__init__.py` 中的 `plugin_version`
- 仓库根目录 [`package.v3.json`](../../package.v3.json) 里 `CloudSubscribe` 的 `version` 与 `history`

上游原来要求同步 `package.v2.json`。本仓库只有 `package.v3.json`。

发布标签与资产命名遵循 MoviePilot 约定：

```text
CloudSubscribe_v<version>
cloudsubscribe_v<version>.zip
```
