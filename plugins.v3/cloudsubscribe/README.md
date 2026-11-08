# CloudSubscribe 源码目录

本目录 fork 自 [odomu/MoviePilot-Plugins](https://github.com/odomu/MoviePilot-Plugins) 的 `plugins.v2/cloudsubscribe`，上游提交 `876462067593d4fc2f7bc038f2f73d3eaf099b76`（2026-11-05，上游版本 v1.7.7，提交说明「更新版本v1.7.7」）。

除下面两处标记外，Python 源码与上游一致：

- 本文件顶部的 fork 说明，以及 `__init__.py` 文件头注释
- `plugin_version` 从上游 `1.7.7` 改为 `0.0.1-fork`，与 `package.v3.json` 对齐，表示本仓库的改造起点

许可为 GPL-3.0-only，全文在本目录 `LICENSE`。后续修改仍须保留该协议与上述来源。本仓库里的 DownloadCloudUpload 继续使用仓库根目录的 MIT 许可，两套代码不要混用许可。

放置位置按本仓库现有约定：`plugins.v3/cloudsubscribe`，并在 `package.v3.json` 登记。入口仍是上游的 `from app.plugins import _PluginBase`，还没有改成 MP V3 的 `app.sdk.plugin._PluginBase`。改造范围、对应的函数和配置项见 [docs/cloudsubscribe-fork-plan.md](../../docs/cloudsubscribe-fork-plan.md)。

本目录是 `CloudSubscribe` 的 MoviePilot v2 后端源码。面向用户的安装、配置和功能说明见
[网盘订阅助手使用说明](../../docs/cloudsubscribe.md)（同样来自上述上游提交，正文未改）。前端源码在 [`frontend/cloudsubscribe`](../../frontend/cloudsubscribe)，同样带有 GPL-3.0 的 `LICENSE`。

## 目录职责

```text
cloudsubscribe/
├── __init__.py          # 插件入口、元数据及生命周期代理
├── requirements.txt     # Python 运行依赖
├── core/                # 领域模型、配置、平台适配和通用服务
├── drive/               # 网盘能力接口及提供方实现
├── handlers/            # 搜索、订阅、同步、通知和 API 编排
├── search/              # 搜索源客户端
├── utils/               # 文件解析、匹配、Magnet 和 STRM 工具
└── dist/assets/         # CI 构建后写入 Release ZIP 的 Vue 模块联邦产物
```

## 模块边界

- `core/` 定义插件自身业务模型与服务，不放具体网盘或搜索源实现。
- `drive/` 通过稳定能力协议接入网盘；调用方先检查能力，再获取对应服务。
- `search/` 只负责获取和标准化资源候选，不执行转存或文件处理。
- `handlers/` 组合领域服务并对接事件、API、通知和任务入口。
- `utils/` 仅保留无状态或低状态的通用辅助逻辑。

## 前端与发布

前端源码位于 [`frontend/cloudsubscribe`](../../frontend/cloudsubscribe)。
上游用 `plugins-release.yml` 在发布时把构建产物打进插件 ZIP。本仓库没有引入该工作流，源码树里也没有 `dist/assets`（本仓库 `.gitignore` 忽略 `dist/`）。

在本仓库改版本时同时更新：

- `__init__.py` 中的 `plugin_version`
- 仓库根目录 [`package.v3.json`](../../package.v3.json) 里 `CloudSubscribe` 的 `version` 与 `history`

上游原来要求同步 `package.v2.json`。本仓库只有 `package.v3.json`。

发布标签与资产命名遵循 MoviePilot 约定：

```text
CloudSubscribe_v<version>
cloudsubscribe_v<version>.zip
```
