# LX Apple Sync

读取 LX 桌面版当前音源与歌单，通过 alx 批量下载 MP3，再经已配对 USB 导入 iPhone / iPad 的苹果自带「音乐」。保留歌单顺序、封面、曲名、歌手、专辑、已有元数据和静态歌词。

## 安装与运行

当前实测环境是 macOS + USB iPhone。需要 Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Git，以及用于构建下载器的 [Rust](https://www.rust-lang.org/tools/install)。LX 桌面版需已导入自定义音源并选中它；音源可用性决定哪些歌曲能够下载。

首次安装：

```sh
git clone https://github.com/xhzq233/lx-apple-sync.git
cd lx-apple-sync
uv sync
sh scripts/install-alx.sh
```

连接手机，在手机上信任这台电脑、完成配对，退出手机上的「音乐」，然后运行：

```sh
uv run lxsync playlists
uv run lxsync devices

# 只下载，保留前三首原始顺序
uv run lxsync download 精选 --limit 3 --quality 320k

# 下载后导入；一台 USB 设备时自动选中
uv run lxsync sync 精选 --limit 3 --quality 320k

# 多台设备时明确目标；完整歌单省略 --limit
uv run lxsync sync "精选金曲" --udid YOUR_DEVICE_UDID

# 完整同步 NieR 歌单（名称唯一时可用部分名称）
uv run lxsync sync NieR --quality 320k
```

若本机装有 ios-use，同步会尝试自动退出设备上的「音乐」；没有安装则需先手动退出，无需为同步安装真机自动化驱动。导入不需要 Apple Music 订阅，也不需要先同步到 Mac「音乐」。默认使用 USB；本项目还没有验证 Wi-Fi 同步。

`--quality 320k` 首先请求 320k。alx 可能在音源不支持时降级；结果记录实际码率，不会把 128k 标成 320k。

当前 GUI 音源无法解析某些曲目时，可仅为这次下载指定另一份 LX 音源脚本：

```sh
uv run lxsync sync NieR --source-script ~/Downloads/source.js
```

这不会导入或切换 GUI 的音源。项目不分发音源脚本或卡密。音频时长会与 LX 歌单记录核对；返回广告、提示音或试听片段时保留 `.rejected-*.mp3` 样本、记录失败，不导入手机。完整解码和时长相符仍不能证明任意音源返回的内容一定是正确版本。

## 本机实测（2026-10-01）

LX「精选金曲」前三首已完成批量下载和 iPhone iOS 26.5.1 USB 同步。原生歌单显示三首、约 14 分钟，顺序与 LX 一致。新增两首，第一首复用已有曲目；重跑没有重复歌曲或歌单条目，设备数据库回读正常，三首本地 MP3 完整解码通过。原生播放器逐首显示真实封面与静态歌词，播放进度推进，结束时暂停、音量保持 0%。

| 曲目 | 本次下载实际码率 | 手机实际码率 | 静态歌词 |
| --- | --- | --- | --- |
| コンプリケイション | 128k（请求及任务标记 320k，文件实际为 128k） | 320k（复用原音频） | 627 字 |
| コンプリケイション -still struggle version- | 320k | 320k | 647 字 |
| Any Love of Any Kind (Choir Version) | 320k | 320k | 1011 字 |

同步结果里的 `download_kbps` 是本机文件的实际码率，`kbps` 是设备回读码率。原有第一首的年份、流派、作曲信息保留；新增两首保留下载文件的标签及 LRC 作曲署名，源文件没有提供的年份与流派没有自动补全。iPad、Windows、Wi-Fi、完整 396 首歌单尚未实测。证据保存在本机 `e2e/` 和上述同步备份目录，不进入源码发布包。

2026-10-03 再次重跑上述三首，设备回读仍为三首、没有重复条目、数据库检查正常。另测试完整 NieR 歌单 22 首：GUI 当前 HYW 音源解析失败；Huibq 返回的 12 个文件均只有约 9.9 秒，被时长校验拒绝；其他曲目及备用源没有获得可用的完整音频。这轮没有向手机新增 NieR 曲目，尚未通过完整歌单验证。安装脚本从干净目录克隆、打补丁和构建成功，Python wheel 在独立环境安装后可读取真实 GUI 歌单与 USB 设备。

## 本地数据

- 下载音频、LRC：`~/Music/LX-Apple-Sync/`，按平台和歌曲 ID 命名。
- 独立下载器、当前源副本与任务：`~/.local/share/lx-apple-sync/alx/`。
- 下载清单：`~/.local/share/lx-apple-sync/manifests/`。
- 每批写入前的原设备数据库、暂存库、真实设备回读和结果：`~/.local/share/lx-apple-sync/backups/`。
- 最新同步结果：`~/.local/share/lx-apple-sync/last-sync.json`。

LX GUI 的配置、音源和歌单数据库只读。重跑会复用已下载音频、识别设备已有曲目，补充歌单关联，不重复添加歌曲。同步是增量追加；不会删除设备上的歌曲或清空原歌单，也不会重排已有条目。某首下载失败会保留失败结果，继续处理成功的歌曲，再以非零退出码提示；重新运行会重试失败任务。已有本地文件直接复用，改变 `--quality` 不会自动重新下载或升级它。

全局选项 `--gui-dir`、`--state-dir`、`--alx` 放在子命令前。例如：

```sh
uv run lxsync --gui-dir "/path/to/LxDatas" download "精选金曲" --limit 3
```

## 复用与边界

- [alx / agent-lx-music](https://github.com/Xuepoo/agent-lx-music)：执行 LX JS 音源、下载队列、标签、歌词与封面。独立配置不影响 GUI 或原 alx。`vendor/alx-0.4.0.patch` 修正异步音源初始化及请求回调等待、真实歌曲元数据传递、Mac 下载进程识别，以及 `{source}` / `{id}` 文件名占位符。
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)：已有配对与 USB AFC 文件访问。
- [ByeTunes](https://github.com/EduAlexxis/ByeTunes)：复用 MIT 媒体库字段映射和导入思路。ByeTunes 本身是 iOS App，并依赖额外的 Rust idevice 静态库、签名和配对文件；没有把它整套移植成 CLI。设备模块基于先前已在本机真机验证的 Python 原型。
- [Mutagen](https://mutagen.readthedocs.io/)：读取 MP3 标签与实际码率，把已有 LRC 转成静态 USLT 歌词。

当前导入器编辑设备已有媒体库，保留其版本、表结构和触发器；每批只交换一次数据库，写入前备份，写入后从设备回读。它不是苹果 ATC 同步协议的实现。

目前以 MP3 为导入格式。没有自定义逐行滚动歌词。Windows / Linux 与 iPad 的实测结果应另行记录，不能把 Python 库的平台支持等同于本项目验证通过。

## 在另一台机器安装下载器

已有可兼容 alx 时可用 `--alx /path/to/alx`。推荐构建本项目测试过的 0.4.0 修正版，需要 Git 和 Rust：

```sh
sh scripts/install-alx.sh
```

源脚本和源凭据不随项目发布，由本机 LX 当前选中的音源导入。
