# CloudSubscribe Fork 改造清单

本文件是后续改造的任务清单，不是已实现的功能说明。代码位置以本仓库引入后的路径为准，对应上游 `plugins.v2/cloudsubscribe/` 下的同名相对路径。

## 来源

| 项 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/odomu/MoviePilot-Plugins |
| 上游提交 | `876462067593d4fc2f7bc038f2f73d3eaf099b76`（2026-11-05，说明为「更新版本v1.7.7」） |
| 上游版本 | `package.v2.json` 与当时的 `plugin_version` 都是 `1.7.7` |
| 许可 | GPL-3.0-only。插件目录与 `frontend/cloudsubscribe/LICENSE` 各有一份全文 |
| 本仓库版本 | `0.2.1`（`package.v3.json` 与 `plugin_version`）；引入起点为 `0.0.1-fork` |
| 插件代码 | `plugins.v3/cloudsubscribefork/` |
| 前端源码 | `frontend/cloudsubscribe/` |
| 上游使用说明副本 | `docs/cloudsubscribe.md` |

引入那一轮（`0.0.1-fork`）只引入代码并写清单。`0.1.0` 完成 M0 的 SDK 对齐和共存规则，转存、搜索、订阅的业务逻辑仍与上游一致。`DownloadCloudUpload` 的代码和 `package.v3.json` 条目始终未改。两份 `icons/cloud.png` 哈希相同，清单继续引用仓库里已有的 `cloud.png`。

`0.2.1` 起插件 ID 和入口类改为 `CloudSubscribeFork`（目录 `plugins.v3/cloudsubscribefork/`），名称「网盘订阅助手（改版）」。原因：MP V3 插件市场按插件 ID 合并卡片、只留最高版本，用原 ID `CloudSubscribe` 时本仓库的版本会被原作者仓库的 1.7.7 盖掉，装好后还可能把 1.7.7 当成更新覆盖改版。改名后数据目录、定时服务 ID、整理历史的下载器标记（`网盘订阅助手（改版）`）都与原版分开。入口类 `0.1.0` 起继承 `app.sdk.plugin._PluginBase`，与本仓库 DownloadCloudUpload 一致。

## 与 DownloadCloudUpload 共存

本仓库原有的「下载完成自动上传」(DownloadCloudUpload) 和本插件会装在同一个 MoviePilot 里。分工固定为：**下载器任务上传 115 归 DownloadCloudUpload，本插件只做网盘分享转存**。后面每一期都不能打破下面几条，规则集中在 `plugins.v3/cloudsubscribefork/core/coexist.py`。

| # | 重叠点（上游代码） | 规则 | 状态 |
| --- | --- | --- | --- |
| 1 | `enable_pt_upgrade`：MP 整理完成（`TransferComplete`）后，`PtUpgradeService.process_pt_upgrade` 把本地文件算 SHA1 上传到网盘，与 DownloadCloudUpload 做的是同一件事 | 固定关闭：`enforce_forced_values` 在应用配置时改回 `false` 并持久化；前端开关置灰并说明原因 | 0.1.0 已做 |
| 2 | `platform_download_policy` 上游默认 `block`；接管时段内 `_is_platform_search_blocked` 会全局拦截平台搜索，不区分订阅，下载器就收不到任务 | 新装默认 `subscribe_filter_mode=include`（列表为空即不接管任何订阅）、`platform_download_policy=allow`；`include` 模式下不做全局平台搜索拦截 | 0.1.0 已做 |
| 3 | 本插件自己扫码登录 115。扫码选的客户端类型和 P115StrmHelper 相同时，115 会踢掉旧会话，DownloadCloudUpload 复用的 Cookie 随之失效 | 新增 `p115_cookie_source`，默认 `strm_helper`：只读复用 P115StrmHelper 的 `cookies`（与 DownloadCloudUpload 的 `strm_cookie` 读法一致，不回写）；此模式下 115 扫码接口直接拒绝。选 `own` 才用本插件的 `cookies` 与扫码，界面提示要选不同客户端类型 | 0.1.0 已做 |
| 4 | `p115_transfer_path` 与 DownloadCloudUpload 规则的 `target` 重合时，转存进来的文件会被它的「核对已存在文件」误判为冲突 | 读取 DownloadCloudUpload 已启用规则的 `target`；转存目录相等、在其内部或包含它（`/` 除外）时，本次运行不注册 115 并在日志说明 | 0.1.0 已做 |
| 5 | 上游默认 `strm_generate_enabled=true`，会在 115 侧再写一套 STRM | 默认关闭；115 的 STRM 继续由 P115StrmHelper 负责（M3 再做按盘跳过） | 0.1.0 已做（默认值） |
| 6 | 回归 | 每期验收都要求：DownloadCloudUpload 的测试全部通过；启用、停用本插件前后，它的队列、MP 自动整理接管和 STRM 监控目录不变；停用本插件后订阅钩子完全恢复 | 每期执行 |

已知限制：P115StrmHelper 的 Cookie 在本插件应用配置时读取，对方换了 Cookie 后，需要在本插件保存一次配置（或重启）才会用上新值。

## 和预期不一致的地方（以代码为准）

下面几条和「按分享类型自动选盘」的目标描述不一致。清单按代码写，不按那个描述回写上游行为。

1. **没有「同盘优先」仲裁，也没有 `preferred_drives` / `same_cloud_first`。** 目标盘是单一配置 `cloud_drive`（界面文案「当前转存网盘」，`frontend/cloudsubscribe/src/config/fields/basic.js`）。`CloudSubscribe._apply_search_config` 把它写入 `_cloud_drive_key`，默认 `"115"`（`core/config.py` 的默认值也是 `"115"`）。`CloudDriveManager.initialize` 用这个 key 取 `CloudDriveRegistry.get`，结果放进 `active_drive`，再赋给插件的 `_cloud_drive`。转存、STRM、洗版都围绕这一个活动盘。
2. **多个网盘可以同时登录，但不会按分享类型自动切换目标。** `CloudDriveManager.register_provider` 会尝试注册扫描到的每个驱动。真正转存时，`SyncHandler._transfer_episode_batch` 只有在 `cross_transfer_enabled`（或手动跨盘标记）且 `source_provider.key != self._cloud_drive.key` 时才走跨盘。否则用活动盘做分享转存。
3. **订阅搜索会先按「当前转存网盘」裁掉其他分享类型。** `PanSouSearchService.search` 在非资源列表模式、且带订阅时，把 `cloud_types` 收成 `_cloud_drive_resource_types`；拿不到就退成 `{当前盘}`，仅当当前盘是 `115` 时再补上 `magnet` 和 `ed2k`。`CloudDriveManager._filter_supported_resource_types` 在跨盘关闭时会从 `_resource_type_order` 里删掉活动盘不支持的类型。所以现在不是「搜到 115 和夸克后再决定落哪」，而是搜索阶段就偏向当前盘。
4. **`resource_type_order` 是搜索优先级，不是目标盘列表。** 默认 `["115", "ed2k"]`。`SearchHandler.__init__` 若发现 `cloud_drive` 不在这个列表里，会把它插到最前。`ResourceTransferService._build_transfer_resource_batches` 按这个顺序组批次，跨盘批次放在最后。不要把它改造成 `preferred_drives` 的别名，两者以后要并存：一个管「先搜哪种资源」，一个管「落到哪个已登录的盘」。
5. **跨盘不是 OpenList。** 上游没有 OpenList / AList 客户端。跨盘实现是 `core/transfer.py` 的 `CrossDriveTransfer` 和 `CrossTransferTaskManager`：先按校验和尝试目标盘秒传（`LocalRapidUploadAdapter`；115 用 SHA1，夸克等用 MD5），失败再下载到本地缓存（配置 `cross_transfer_download_path`，界面默认提示 `/tmp`）再 `LOCAL_UPLOAD`。`handlers/sync/service.py` 的 `_transfer_file`、`_cross_transfer_staging_path`、`_cleanup_cross_transfer_staging` 负责编排。OpenList→115 是以后要新做的兜底，不是打开已有开关。
6. **磁力离线只存在于挂了 `OFFLINE_DOWNLOAD` 的驱动。** 有这个能力的是 115（`drive/p115/provider.py`）、123（`drive/p123/provider.py`）、百度（`drive/baidu/provider.py`）、光鸭（`drive/guangya/provider.py`）。夸克的 `resource_types` 只有 `quark`，服务表里没有 `OFFLINE_DOWNLOAD`。天翼的 `resource_types` 写了 `ed2k` 和 `magnet`，但 `create_tianyi_provider` 的 `services` 没有 `OFFLINE_DOWNLOAD`，不能把天翼当成磁力离线目标。`ResourceTransferService._is_supported_resource` 对 `ed2k` / `magnet` 还要求活动盘上的 `_offline_download` 不为空。
7. **「已配置」对 115 和夸克不对称。** `QuarkDriverDefinition.create_client` 在 `quark_cookie` 为空时返回 `None`，夸克不会进 registry。`P115DriverDefinition.create_client` 即使 `cookies` 为空也会构造 `P115ClientManager` 并注册。仲裁不能把「registry 里有 115」当成「115 已登录」。115 的凭证字段名就是 `cookies`，不是 `p115_cookie`；夸克是 `quark_cookie`。
8. **上游自己生成 STRM，不调用 P115StrmHelper。** 全库没有 `P115StrmHelper` 这个名字。STRM 由 `SyncHandler._generate_strm` 调用 `utils/strm.py` 的 `StrmGenerator.write`，写到 `local_resource_path`。默认模板是 `{base_url}/d/{pickcode}?/{file_name}`，对应 115 的 `P115PlaybackReference`（`proxy_identifier = "pickcode"`）。夸克的 `QuarkPlaybackReference.proxy_identifier` 是 `file_id`。全局只有一份 `strm_url_template`，不会按盘切换。和 P115StrmHelper 的分工是本 fork 的新边界，不是上游已有开关。
9. **整理不是 MP 的整理完成事件。** `organize_after_transfer` 为真时，插件在网盘内改名、移动，入口是 `handlers/sync/postprocess.py` 的 `_organize_and_finalize_single_item`。为假时，`_transfer_episode_batch` 调用分享服务的 `transfer_share` 整包转存，保留原目录。媒体库刷新是 `MediaServerNotifier._refresh_service`（`handlers/notification/media_server.py`），由 `media_server_refresh_enabled` 控制。没有单独的「整理事件」钩子。
10. **使用说明的搜索渠道表和代码数量不一致。** `docs/cloudsubscribe.md` 的渠道表没有「蜗牛」。代码里有 `search/woniu/WoniuSearchService`，上游 `package.v2.json` 的 v1.6.1 记录写了新增蜗牛。改造时以 `search/` 目录为准。当前目录有：`pansou`、`seedhub`、`onlinedocs`、`pianku`、`juying`、`xiaoya`、`dian115`、`hdhive`、`hdhaven`、`woniu`、`piratebay`、`uindex`、`animegarden`、`mikan`。

## 目标

MoviePilot 的订阅搜索由本插件接管，用已有多源（至少包括 PanSou）找出分享或磁力，再决定落盘：

- 分享属于哪种网盘，就优先转存到同一种已启用的网盘。115 分享进 115（`p115_transfer_path`），夸克分享进夸克（`quark_transfer_path`）。
- 115 和夸克都真正登录时自动选，不要求用户再指定唯一的「当前转存网盘」。
- 只登录一个时，只用那一个。另一种分享不能假装转进没登录的盘。
- 同盘转不了时，按 `preferred_drives` 的顺序跨盘到首选盘；仍然不行再用磁力离线，且目标盘必须真有 `OFFLINE_DOWNLOAD`。
- 整理之后刷新媒体库。115 媒体库侧的 STRM 继续交给 P115StrmHelper；本插件负责搜索和转存。夸克没有对应的外部 STRM 插件，STRM/302 留在本插件。

## 上游现在怎么走

```text
订阅触发
  SubscriptionSearchHook
    -> SyncExecutionService.queue_subscribe_search
搜索
  SearchHandler
    -> PanSouSearchService.search（以及其他 search/*/service.py）
    -> resource_type_order 排序
选目标盘
  只有 _cloud_drive / cloud_drive
转存
  同盘: drive/*/share.py 的 transfer_share，或按文件转存到 _cloud_transfer_path
  跨盘: CrossTransferTaskManager（需 cross_transfer_enabled）
  磁力: 活动盘的 OFFLINE_DOWNLOAD
整理与 STRM
  postprocess._organize_and_finalize_single_item
  SyncHandler._generate_strm
刷新媒体库
  MediaServerNotifier._refresh_service
```

各盘路径已经按驱动拆开，不需要再发明路径字段。`DriverDefinition.get_transfer_path_key` / `get_media_path_key` 找以 `_transfer_path`、`_media_path` 结尾的表单键。`SyncHandler._configured_transfer_paths` 和 `_configured_media_paths` 读成字典。活动盘的路径再写进 `_cloud_transfer_path` 和 `_CLOUD_MEDIA_ROOT`。115 的键是 `p115_transfer_path`、`p115_media_path`；夸克是 `quark_transfer_path`、`quark_media_path`。同盘命中后，必须改用该盘自己的这两项，不能继续写活动盘的路径。

订阅接管相关配置和代码：

| 配置键 | 界面位置 | 作用 |
| --- | --- | --- |
| `takeover_new_subscribes` | 基础设置「拦截新增订阅」 | 订阅状态为 `N` 时也进入插件 |
| `block_system_subscribe` | 「始终接管系统订阅」 | 为真时全天接管 |
| `block_start_time` / `block_end_time` | 接管开始/结束 | `block_system_subscribe` 为假时的时段 |
| `platform_download_policy` | 平台下载策略 | `allow` / `block` / `cloud` |
| `subscribe_filter_mode` | 转存设置 | `exclude` 或 `include` |
| `exclude_subscribes` / `include_subscribes` | 转存设置 | 配合上面的模式 |
| `cron` | 订阅执行周期 | 巡检未完成订阅 |

逻辑在 `core/hook/subscription.py` 的 `SubscriptionSearchHook`：

- `_install_subscribe_search_takeover` 替换调度器里的 `subscribe_search`、`new_subscribe_search`、`subscribe_refresh`
- `_install_subscribe_chain_takeover` 包装 `SubscribeChain._process_search_subscription`，注释里写了适配 V3 持久化队列
- `_install_platform_search_block` 包装 `SearchChain` 的 `search_by_id`、`search_by_title` 以及对应的 async/stream 方法
- 是否接管由 `core/services/subscription.py` 的 `SubscriptionControlService._is_takeover_active` 决定
- 平台资源下载事件在 `core/hook/events.py` 的 `PluginEventHandler._takeover_platform_download`

这些是在 v2 插件基类上打的补丁，并带了一些 V3 方法名（例如 `execution_context.report_phase`）。它们还不是本仓库 `DownloadCloudUpload` 使用的 `app.sdk`。

洗版和重试也绑定在单一活动盘上，后续要跟着新的目标盘走：

- 洗版：`enable_cloud_upgrade`、`upgrade_mode`（`largest` / `smallest` / `coexist` / `replace`）、`upgrade_subscribe_ids`，实现是 `handlers/sync/upgrade.py` 的 `UpgradeService._upgrade_scan_single_sub`
- 重试：`handlers/sync/retry.py` 的 `retry_history_record`。其中有 `target_drive_key` 必须等于 `_cloud_drive.key` 的判断，多目标盘之后会误拒绝
- 风控冷却：`transfer_risk_cooldown`，默认 1800 秒。夸克分享转存用这份公共配置（`QuarkDriverDefinition` 的说明写了这一点）
- 历史里已经有 `source_drive_key`、`target_drive_key`（`handlers/sync/history.py`），可以继续用，不必另造记录格式

## 核心改造

### 落盘仲裁

新增一层选择目标盘的函数，放在 `CloudDriveManager` 或 `ResourceTransferService` 旁边，由 `SyncHandler._transfer_episode_batch` 在计算 `cross_batch` 之前调用。输入是分享 URL 的资源类型（`ResourceTransferService._supported_resource_type` / `search/types.py` 的 `normalize_resource_type`）和当前 registry。

建议顺序：

1. `same_cloud_first` 为真，且该分享类型对应的驱动已登录：目标就是这个盘，走该盘的 `transfer_share`（整理关闭）或按文件转存（整理打开）。115 分享对 `115`，夸克分享对 `quark`。
2. 否则按 `preferred_drives` 从前到后找一个已登录、并且能接住这份资源的盘。同类型能力不够时，才允许走现有的 `CrossDriveTransfer`（源盘要有 `SHARE_TRANSFER` 和 `FILE_DOWNLOAD`，目标盘要有 `LOCAL_UPLOAD` 和 `FILE_QUERY`，与 `_is_supported_resource` 的跨盘条件一致）。
3. 资源是 `magnet` 或 `ed2k` 时，只选挂了 `OFFLINE_DOWNLOAD` 的盘，优先 115。夸克不能当离线目标。
4. 只登录了一个盘：`preferred_drives` 里没有第二个可用项，全部落到这一个盘；另一种分享要么跨盘进来，要么明确失败，不能写入空 Cookie 的 115。

「已登录」不要用 registry 成员资格代替。夸克看 `quark_cookie` 非空且 `register_provider("quark")` 成功。115 要在 `cookies` 非空之外再看账号能力，避免空 Cookie 仍被 `create_client` 注册。

要一起改的调用点：

- `CloudDriveManager.initialize` / `_filter_supported_resource_types`：跨盘关闭时不要再用单一活动盘删掉另一种分享
- `PanSouSearchService.search`：订阅搜索不要把 `cloud_types` 收成一个 `_cloud_drive_key`
- `SearchHandler.__init__`：不要再把单一 `cloud_drive` 插进 `resource_type_order` 来代表目标盘
- `SyncHandler._transfer_episode_batch` 里 `cross_batch` 的判断
- `handlers/sync/retry.py` 里对 `target_drive_key` 的单盘比较
- 前端 `basic.js` 的「当前转存网盘」和 `transfer.js` 的「跨盘资源自动转存」

`cross_transfer_enabled` 保留。`same_cloud_first` 决定同盘是否优先；跨盘开关仍决定「不同盘时是否允许下载再上传」。两个都关、又没有同盘时，应当失败并写明原因，而不是静默落到默认 115。

### `preferred_drives` 与 `same_cloud_first`

新增配置，不删除旧键，避免已有配置无法读出：

| 新键 | 含义 | 取代关系 |
| --- | --- | --- |
| `preferred_drives` | 有序的驱动 id 列表，例如 `["115", "quark"]` | 替代「唯一目标盘」。`cloud_drive` 降为兼容字段 |
| `same_cloud_first` | 布尔，默认 true | 为真时按分享类型匹配已登录驱动；为假时保持现在的单盘行为 |

兼容规则：

- 旧配置只有 `cloud_drive`、没有 `preferred_drives`：把 `cloud_drive` 当作列表的第一项，`same_cloud_first` 默认为 true 时仍先尝试同盘，同盘没有再落到这个旧目标。
- `same_cloud_first` 为 false：行为与现在一致，只使用 `cloud_drive` / `preferred_drives` 的第一项作为 `_cloud_drive`。
- 界面上「当前转存网盘」改为有序列表，并加 `same_cloud_first` 开关。文案要写明：列表是兜底顺序，不是搜索类型顺序（搜索顺序仍是 `resource_type_order` 和 `search_source_order`）。

默认值放在 `core/config.py` 的默认字典，读取放在 `CloudSubscribe._apply_sync_config` 或 `_apply_search_config`，与 `cloud_drive` 放在一起。

### 与 P115StrmHelper 的边界

本插件继续负责：多源搜索、订阅接管、分享转存、跨盘、磁力离线、夸克 STRM。

115 媒体库 STRM 继续由 P115StrmHelper 负责。本仓库的 DownloadCloudUpload 已经按这个边界工作（读取 `P115StrmHelper.cookies`，不自己生成 115 播放串）。CloudSubscribe 上游则是自己写 STRM。后续在 115 命中时跳过 `SyncHandler._generate_strm`，不要再向 `local_resource_path` 写 pickcode 链接，避免和 P115StrmHelper 各写一套。

115 转存路径仍用 `p115_transfer_path` / `p115_media_path`。P115StrmHelper 监控的是它自己的媒体库目录，两边的 115 目录需要在配置说明里对齐，插件不要去改 P115StrmHelper 的配置。

夸克没有这层外部助手，继续用本插件的 `strm_generate_enabled`、`StrmGenerator` 和 `core/api/strm_proxy.py` 的 `StrmProxyApi`。

### 夸克 STRM / 302

上游已经有一半：

- `QuarkPlaybackReference` 声明 `file_id`
- `StrmGenerator.proxy_template` 能按标识变量生成 `/{route}/{file_id}?/{file_name}`
- `core/api/strm_proxy.py` 的模块说明写了「短链跳转，以及需要认证请求头的夸克流式中转」，`_RangeStream` 用来拼接夸克的分段响应

缺的是体验对齐，不是从零做 302：

- 默认 `strm_url_template` 是 115 的 `{pickcode}`。夸克文件没有 pickcode，沿用默认模板会生成无效 STRM。应按当前文件所属驱动选择标识：115 用 `pickcode`，夸克用 `file_id`。
- 115 这条一旦交给 P115StrmHelper，本插件的代理只需要把夸克短链做好。
- 播放要带上夸克 Cookie / UA。`StrmProxyApi` 里已有中转，M3 对照 115 的「点开即播」检查分段范围、认证丢失和低速（上游 v1.7.7 的更新说明提到过这类修复，改造时回归这一段）。

### V3 SDK 对齐

要改的是宿主接口，不是把业务重写一遍。对照本仓库 `plugins.v3/downloadcloudupload/__init__.py`：

| 现在 | V3 侧 |
| --- | --- |
| `from app.plugins import _PluginBase` | `from app.sdk.plugin import _PluginBase` |
| `from app.log import logger` | `from app.sdk.logging import logger` |
| `BackgroundScheduler` 自己建调度器 | 优先 `app.sdk.scheduler`，与 DownloadCloudUpload 一致 |
| `app.api.endpoints.plugin.register_plugin_api` | 按 V3 插件类上的路由方式挂 API |
| `SubscriptionSearchHook` 替换调度器和 `SubscribeChain` 方法 | 先确认 MP V3 上这些符号还在；在则保留行为，不在则改到 V3 的订阅队列接口。上游注释已指向 `SubscribeChain._process_search_subscription` 和 `execution_context` |
| 整理 | 继续插件内部后处理，不要假装去订阅一个不存在的整理事件。若 V3 提供整理完成回调，只用于「转存完成后通知」，网盘内改名仍留在 `postprocess.py` |
| 通知 | `_apply_notification_config`、`notify`、`WebhookHandler`、`MediaServerNotifier`。刷新开关是 `media_server_refresh_enabled`、`media_servers`、`media_server_refresh_delay`、`media_server_path_mappings` |

前端源码在 `frontend/cloudsubscribe`，构建产物不在 git 里。M0 要单独决定配置页怎么出现在 V3 里：沿用这套 Vue，或先做一版能登录两个盘的最小页面。在这之前，市场能看到插件，不代表打开设置页就有界面。

## 分期

### M0 引入可装可启用，双盘能登录

这一轮已经完成引入和清单登记。下面没勾的都还没做。

- [x] 源码在 `plugins.v3/cloudsubscribefork`，前端在 `frontend/cloudsubscribe`，`package.v3.json` 登记为 `0.0.1-fork`
- [x] 保留 GPL-3.0 全文，并在 README / `__init__.py` 写明上游仓库与提交
- [x] 入口改为 `app.sdk.plugin._PluginBase`；日志、配置、事件、缓存、媒体、网络等改为 `app.sdk.*`。按 MoviePilot `v3` 分支源码静态核对过，全部 `app.*` 导入都能解析（无 SDK 入口的 `app.db.*_oper` 等仍走宿主兼容层）
- [x] 依赖清单改为 `pyproject.toml`
- [x] 115 扫码：上游已有（`P115ClientManager`）。共存模式默认复用 P115StrmHelper，只有 `p115_cookie_source=own` 时开放扫码
- [x] 夸克扫码：上游已有（`QuarkClient`，`quark_cookie`），未改
- [x] 空 `cookies` 不再把 115 当成已就绪：`P115DriverDefinition.create_client` 返回 `None`，扫码走临时服务
- [x] 设置页沿用现有 Vue 前端；构建输出改到 `plugins.v3/cloudsubscribefork/dist/assets` 并随源码提交（`.gitignore` 单独放行）
- [x] 共存规则 1–5（见「与 DownloadCloudUpload 共存」）
- [ ] 在真实 MP V3 宿主里安装、启用、停用、重载各一次，确认日志无导入错误、`DEBUG=true` 下旧导入提示只剩无 SDK 入口的几项
- [ ] 联邦组件 CSS 是否污染主程序（V3 开发指南 8.3），在真实页面检查

验收：

- 插件市场能看到「网盘订阅助手」`0.1.0`，`package.v3.json` 可被解析
- 在 MP V3 安装后启用，日志没有导入错误，停用再启用也不残留调度器钩子
- 只填 115 或只填夸克，账号信息能显示对应那一个
- 两个都填时，registry 里同时有 `115` 和 `quark`，凭证不会写串
- 此阶段可以仍使用单一 `cloud_drive`，不要求同盘仲裁已经生效
- 新装启用后不勾选任何订阅：MP 订阅、下载器和「下载完成自动上传」的行为与安装前完全一样
- PT 洗版开关无法打开；复用 P115StrmHelper 时 115 扫码被拒绝；115 转存目录与上传目录重叠时日志提示且不转存
- DownloadCloudUpload 的测试全部通过

### M1 `same_cloud_first` 仲裁，并和订阅接管联调

0.2.0 的实现方式（`core/drive_routing.py` + `handlers/sync/drive_scope.py`）：

- 选盘规则是纯函数：`preferred_drives` 里第一个已登录的盘是默认盘；同盘目标 = 已登录、支持分享转存、且在 `preferred_drives` 里的盘。`route_share` 只在分享所属网盘本身是同盘目标时切换，否则沿用默认盘（默认盘能接或交给现有跨盘逻辑）。
- 上游 SyncHandler 把当前网盘、各能力服务、`_cloud_transfer_path`、`_CLOUD_MEDIA_ROOT`、批大小、STRM 生成器存成实例属性，上百处代码直接读。0.2.0 不逐处改，而是把这 18 个属性换成按作用域取值的描述符：默认取值与上游一致；在 `drive_scope("quark")` 里取夸克那一套。作用域用 `contextvars` 记录，只影响当前线程，跨盘批次的线程池用 `run_in_current_scope` 显式带过去。
- 进入作用域的位置：电影 / 剧集的逐资源循环（`ExitStack`，每条资源按分享链接切换）、`_is_supported_resource` / `_is_cross_drive_resource`、历史重试线程、后处理监控（待处理记录新增 `drive_key`，按网盘分组后进入对应作用域）。
- 历史记录新增 `cloud_drive_key`，`cloud_drive_name` 随作用域变化；`target_drive_key` 仍只在跨盘时写。
- 洗版（剧集洗版流程、网盘洗版扫描、电影洗版候选）用 `no_drive_routing()` 固定在默认盘，按实际落盘处理留到 M2。
- 非默认盘不生成 STRM（夸克 STRM 属于 M3）。

- [x] 增加 `preferred_drives` 和 `same_cloud_first`，旧的 `cloud_drive` 仍能读（没有 `preferred_drives` 时旧值排第一；保存时 `cloud_drive` 跟随列表第一项，供旧界面逻辑读取）
- [x] 前端基础设置改成有序多选（按选择顺序）和开关
- [x] 订阅搜索不再按单一 `_cloud_drive_key` 裁剪：`_cloud_drive_resource_types` 取默认盘与同盘目标的并集，同盘目标自动补进 `resource_type_order` 末尾
- [x] 115 分享转到 115 的 `p115_transfer_path`，夸克分享转到夸克的 `quark_transfer_path`（整理后进各自的 `*_media_path`）
- [x] 只登录一个盘时，另一种分享不写入未登录的盘（未登录的盘不注册，也就不是同盘目标；只登录夸克时默认盘自动变成夸克）
- [ ] 在真实 MP 上按下面的验收各走一遍
- [ ] `takeover_new_subscribes` 或 `block_system_subscribe` 打开时，新订阅进入 `queue_subscribe_search`，而不是平台下载器（只对勾选的订阅生效，见共存规则 2）
- [ ] `platform_download_policy=block` 时，接管期内 `SearchChain` 搜索被拦住；`cloud` 时平台磁力能转成网盘离线（仅当目标盘有 `OFFLINE_DOWNLOAD`）

验收：

- 两个盘都登录、`same_cloud_first` 为真：一条 115 分享和一条夸克分享分别出现在各自转存路径，历史里的 `cloud_drive_key` 与分享类型一致
- 关掉 `same_cloud_first`：行为回到现在的单盘，只进 `cloud_drive` 指定的盘
- 只配置夸克：115 分享不会调用 `P115` 的 `transfer_share`
- 只配置 115：夸克分享同样不会调用 `QuarkShareService.transfer_share`
- 新建一条 MP 订阅后，插件日志出现「订阅搜索转入网盘任务」，平台下载器没有新增对应任务
- `subscribe_filter_mode=include` 时，未选中的订阅仍走系统原来的搜索

### M2 跨盘与离线兜底、洗版、失败重试

- [x] 同盘未命中时，调用现有 `CrossTransferTaskManager` 转进默认转存网盘（先秒传，再本地中继）。登录了但不在 `preferred_drives` 里的盘作为中转来源，其分享类型并进订阅搜索（`CloudDriveManager.relay_drive_keys`）。来源盘必须有登录账号：分享只能先存进同类账号再下载上传
- [ ] OpenList→115 作为单独的新兜底，默认关闭。上游没有对应模块，不要改 `CrossDriveTransfer` 去假装它已经存在
- [x] 磁力 / ed2k 只提交给有 `OFFLINE_DOWNLOAD` 的盘；默认盘不能离线时交给第一个能离线的同盘目标（`route_offline`）
- [x] 洗版时的查找、删除和转存指向该订阅实际落过的盘（`landed_drive_key` + `subscription_drive_scope`），期间不按分享类型切换
- [x] `retry_history_record` 按记录里的 `target_drive_key` / `cloud_drive_key` 重试（`fixed_drive_scope`）；原目标盘未登录时给出可读原因
- [x] 风控（`transfer_risk_cooldown`）和离线超时（`offline_timeout`）在新仲裁下仍然中断整批，并留下可重试的历史（冷却按来源盘记，夸克风控不影响 115）

验收：

- 夸克未登录、115 在 `preferred_drives` 里靠前、`cross_transfer_enabled` 为真：夸克分享经跨盘进入 115，历史 `source_drive_key=quark`、`target_drive_key=115`
- 跨盘开关关闭且没有同盘：任务失败，原因可读，网盘里没有半份文件
- 磁力在 115 已登录时进入 115 离线队列，不会向夸克提交离线
- OpenList 未配置时，上面的本地中继仍然成功；配置了之后才走 OpenList→115，并且失败时能退回本地中继或明确报错（实现时二选一，写进配置说明）
- 打开 `enable_cloud_upgrade` 后，洗版文件落在原目标盘
- 对一条失败历史点重试，重试打到原 `target_drive_key`，成功后状态不再卡在失败

### M3 夸克 STRM/302 对齐 115 的使用体验

- [x] 115 命中时不调用 `_generate_strm`，媒体库 STRM 只由 P115StrmHelper 生成（P115StrmHelper 已启用时；0.2.4）
- [x] 夸克 STRM 使用 `file_id`，不使用默认的 `{pickcode}` 模板（`StrmGenerator.drive_template`；默认盘以外的网盘用 `/d/<网盘>/<标识>` 短链，需开启播放代理）
- [x] `StrmProxyApi` 按短链里的网盘前缀取链，夸克走带 Cookie 的分段中转，范围请求能拖动进度
- [x] `media_server_refresh_enabled` 打开时，`MediaServerNotifier._refresh_service` 能刷新到夸克 STRM 所在目录（夸克 STRM 生成后按其本地路径通知；路径映射仍由 `media_server_path_mappings` 配置）
- [x] 115 转存完成后的媒体库刷新不依赖本插件再写一份 STRM；P115StrmHelper 负责 115 时本插件跳过兜底刷新（`_strm_by_helper`）

验收：

- 115 转存目录下没有本插件新建的 `.strm`；P115StrmHelper 负责的媒体库里能看到对应条目
- 夸克 `.strm` 的内容是 `file_id` 短链，播放器打开后能起播，拖动进度条不会回到开头或报 401
- 关闭 `strm_generate_enabled` 时，夸克也不写 STRM，只保留网盘文件
- 刷新失败只记日志，不把转存任务标成失败（`_refresh_service` 现在就是这个策略，保持它）

## 风险

- **GPL-3.0。** 本插件及 `frontend/cloudsubscribe` 是上游 GPL-3.0 的衍生作品。修改、再分发都要保留协议全文、版权和来源说明。不要把这些文件改成仓库根目录的 MIT，也不要把 GPL 源码拷进 DownloadCloudUpload。
- **搜索源不稳定。** PanSou 依赖 `pansou_url`（`PanSouSourceDefinition.create_client`，地址为空则没有客户端）。其他渠道还有账号、Cloudflare（`cf_solver_url` / `utils/cloudflare.py`）和熔断（`search_circuit_breaker_*`，`SearchCircuitBreaker`）。仲裁做完后，搜索失败仍应保持现在的跳过和熔断，不要为了「必须有结果」关掉熔断。
- **夸克风控。** 分享转存走公共的 `transfer_risk_cooldown`、`batch_size`、`batch_interval`。`QuarkDriverDefinition` 另外有 `quark_request_timeout`。同盘优先会提高夸克 API 调用量。冷却期间应整批停止并留下可重试记录，不要立刻换盘重试把风控扩大到 115。
- **和 P115StrmHelper 的边界。** 上游默认自己写 115 STRM。若 M3 之前就在已经安装 P115StrmHelper 的环境启用 `strm_generate_enabled`，两套 STRM 会叠在一起。M0/M1 联调 115 时先关掉本插件的 STRM 生成，或不要把 `local_resource_path` 指到 P115StrmHelper 的目录。本插件也不读取、不改写 P115StrmHelper 的 Cookie；115 登录凭证继续用本插件的 `cookies` 字段。
- **V3 宿主差异。** 上游在 v2 插件基类上兼容了一部分 V3 订阅队列。放到 `plugins.v3/` 之后，在完成 M0 之前，市场可见不等于能启用。订阅钩子如果替换失败，要能恢复原函数（现有 `_restore_subscribe_search_takeover`），避免停用插件后系统搜索仍被拦住。
