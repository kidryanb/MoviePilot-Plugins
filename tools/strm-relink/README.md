# 修复已断开硬链接的 STRM：给 grokbot 的指令

背景：0.5.5 之前，插件每次重写 STRM 都会换一个新文件。库里那份硬链接因此断开，内容停在旧地址上。0.5.5 起插件会在原文件上改，不会再断。这份指令处理的是之前已经断开的那些。

尖括号 `<...>` 是需要填的值。以下命令都在 **NAS** 上执行，路径按 **MP 容器里看到的路径** 填。

## 0. 先准备

| 值 | 含义 |
| --- | --- |
| `<MP容器名>` | `docker ps` 里 MoviePilot 的容器名，例如 `moviepilot` |
| `<中转目录>` | 插件「本地挂载媒体根路径」，也就是插件生成 STRM 的目录，例如 `/media/quark_strm` |
| `<库目录>` | MP 整理后 STRM 所在的媒体库目录，例如 `/media/链接/剧集`。电影和剧集分开的话，每个目录各跑一遍 |

前提：
- 插件已更新到 **0.5.5 或更高**（已确认是 0.5.6）。
- `<中转目录>` 和 `<库目录>` 必须在同一个磁盘或卷上，否则建不了硬链接，脚本会跳过并提示。

## 1. 放好脚本

在 NAS 宿主机上执行，下载到 MP 的配置目录（容器里就是 `/config/`）：

```bash
curl -fsSL -o /vol1/1000/docker/moviepilot/config/fix_strm_links.py \
  https://raw.githubusercontent.com/kidryanb/MoviePilot-Plugins/main/tools/strm-relink/fix_strm_links.py
```

脚本是这样配对的：
- 库里的 STRM 如果和中转目录某个文件是同一个 inode，就算正常。
- 不是同一个 inode 的，从内容里取出 32 位的夸克文件 ID，去中转目录找含有同一个 ID 的 STRM。

## 2. 先只列出，不改任何东西

```bash
docker exec <MP容器名> python3 /config/fix_strm_links.py \
  --src <中转目录> --lib <库目录>
```

输出里有三种行：
- `[断开·内容过期]`：硬链接断了，库里的还是旧地址，**需要修**。
- `[断开·内容相同]`：硬链接断了，但内容恰好一样，修了以后以后就能自动同步。
- `[找不到对应]`：中转目录里没找到同一个文件 ID 的 STRM，比如文件已被删除，或者是 115 的 STRM。脚本**不会动**这些。

最后一行是统计。把这一步的输出发给 ryan 确认后，再做第 3 步。

## 3. 修复（会先备份）

```bash
docker exec <MP容器名> python3 /config/fix_strm_links.py \
  --src <中转目录> --lib <库目录> --apply
```

每个断开的库内 STRM 会先复制一份到 `/config/strm_backup/<时间>/`，再换成指向中转目录那份的硬链接。文件名和路径不变，Emby 不用重新扫库。

## 4. 验证

```bash
# 再跑一次只列出，「断开」应该是 0
docker exec <MP容器名> python3 /config/fix_strm_links.py --src <中转目录> --lib <库目录>

# 随便挑一个修过的文件，链接数应该 ≥ 2，内容是新地址
docker exec <MP容器名> stat -c '%h %i %n' "<库目录>/<某个剧>/<某集>.strm"
docker exec <MP容器名> cat "<库目录>/<某个剧>/<某集>.strm"
```

然后在 Emby 里播放其中一集，确认能播。

## 出问题时怎么回退

把备份目录里的文件拷回去：

```bash
docker exec <MP容器名> sh -c 'cp -a /config/strm_backup/<时间>/. <库目录>/'
```

## 给 grokbot 的一句话版本

> 执行 `curl -fsSL -o /vol1/1000/docker/moviepilot/config/fix_strm_links.py https://raw.githubusercontent.com/kidryanb/MoviePilot-Plugins/main/tools/strm-relink/fix_strm_links.py` 把脚本下到 MP 配置目录（容器内 /config/），先执行 `docker exec <MP容器名> python3 /config/fix_strm_links.py --src <中转目录> --lib <库目录>`，只列出断开硬链接的 STRM，把输出发给我确认；确认后加 `--apply` 再跑一次修复（会先备份到 /config/strm_backup/），最后不加 `--apply` 再跑一次，确认「断开」为 0，并在 Emby 播一集验证。
