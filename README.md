# LX Apple Sync

**免费把 LX 歌单带进苹果「音乐」。**

沿用已有 LX Music 的歌单与音源，批量下载，经 USB 直接同步到 iPhone 自带「音乐」。

- **免费开源**：MIT 许可，无需 Apple Music 订阅。
- **复用 LX Music**：无需修改 LX 或迁移歌单、音源配置。
- **手机无需额外 App**：保留封面、曲目信息和可用静态歌词，无需先导入 Mac「音乐」。

[查看宣传页与真机效果 →](https://xhzq233.github.io/lx-apple-sync/)

## 安装与运行

当前实测 **macOS + USB iPhone**。需要 Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Git 和 [Rust](https://www.rust-lang.org/tools/install)。LX Music 桌面版需已选中可用的自定义音源。

首次安装：

```sh
git clone https://github.com/xhzq233/lx-apple-sync.git
cd lx-apple-sync
uv sync
sh scripts/install-alx.sh
```

用 USB 连接手机并信任电脑，在手机 App 切换器上划关闭「音乐」，然后运行：

```sh
uv run lxsync playlists
uv run lxsync sync "精选金曲" --music-closed
```

把 `精选金曲` 换成你的歌单名称。安装完成后，在项目目录运行第二条命令即可下载并同步整张歌单。

常用选项：

| 选项 | 用途 |
| --- | --- |
| `--limit 3` | 先用前三首试同步 |
| `--quality 320k` | 请求 320k（默认），实际码率取决于音源 |
| `--udid YOUR_DEVICE_UDID` | 连接多台设备时指定目标，设备列表用 `uv run lxsync devices` 查看 |
| `--timeout 1800` | 为大歌单延长下载等待时间 |

只下载时用 `uv run lxsync download "歌单名称"`；更多选项见 `uv run lxsync sync --help`。

## 使用说明

- 目前导入 MP3，歌词为静态文本。iPad、Windows、Linux、Wi-Fi 尚未实测。
- 同步为增量追加：已有歌曲直接复用，不清空设备歌曲或重排已有条目；重跑可重试下载失败的歌曲。改变音质选项不会自动升级已有音频。
- 每批导入前自动备份媒体库。音频与 LRC 保存在 `~/Music/LX-Apple-Sync/`，备份与同步结果在 `~/.local/share/lx-apple-sync/`。
- 音源脚本与凭据由你的 LX 配置提供，项目不分发。可用性与实际音质由音源决定。

复用 [alx](https://github.com/Xuepoo/agent-lx-music) 下载、[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) USB 连接、[ByeTunes](https://github.com/EduAlexxis/ByeTunes) 媒体库映射和 [Mutagen](https://mutagen.readthedocs.io/) 标签处理。
