"""Read LX GUI data; delegate source execution and downloads to isolated alx."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone

from mutagen.mp3 import MP3
from mutagen.id3 import USLT


def default_gui_dir():
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "lx-music-desktop/LxDatas"


def gui_connection(root):
    return sqlite3.connect((root / "lx.data.db").resolve().as_uri() + "?mode=ro", uri=True)


def playlists(root):
    with gui_connection(root) as db:
        return [dict(id=i, name=n, songs=count) for i, n, count in db.execute("""SELECT l.id,l.name,COUNT(m.id)
            FROM my_list l LEFT JOIN my_list_music_info m ON m.listId=l.id
            GROUP BY l.id ORDER BY l.position""")]


def select_playlist(root, name, limit):
    entries = playlists(root)
    exact = [p for p in entries if name in (p["id"], p["name"])]
    matches = exact or [p for p in entries if name in p["name"]]
    if len(matches) != 1:
        raise ValueError(f"歌单匹配 {len(matches)} 项，请使用 playlists 查看完整名称或 ID")
    selected = matches[0]
    with gui_connection(root) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT m.* FROM my_list_music_info m
            LEFT JOIN my_list_music_info_order o ON o.listId=m.listId AND o.musicInfoId=m.id
            WHERE m.listId=? ORDER BY o."order",m.rowid''', (selected["id"],)).fetchall()
        if limit is not None:
            rows = rows[:limit]
        tracks = []
        for row in rows:
            meta = json.loads(row["meta"])
            lyrics = db.execute("SELECT type,text FROM lyric WHERE id=? AND source='raw'", (row["id"],)).fetchall()
            tracks.append(dict(gui_id=row["id"], song_id=str(meta.get("songId") or meta.get("songmid") or row["id"].removeprefix(row["source"] + "_")),
                source=row["source"], title=row["name"], artist=row["singer"], album=meta.get("albumName", ""),
                interval=row["interval"], meta=meta,
                lrc=next((base64.b64decode(text).decode() for kind, text in lyrics if kind == "lyric"), "")))
    return selected, tracks


def plain_lyrics(lrc):
    return "\n".join(line.strip() for line in re.sub(r"\[[^\]\n]*\]", "", lrc).splitlines() if line.strip())


def write_config(path, data):
    lines = []
    def section(values, keys):
        if keys:
            lines.append("[" + ".".join(json.dumps(k) for k in keys) + "]")
        for key, value in values.items():
            if value is not None and not isinstance(value, dict):
                lines.append(json.dumps(key) + " = " + json.dumps(value, ensure_ascii=False))
        lines.append("")
        for key, value in values.items():
            if isinstance(value, dict):
                section(value, [*keys, key])
    section(data, [])
    path.write_text("\n".join(lines))
    path.chmod(0o600)


class Downloader:
    def __init__(self, state, gui, binary=None):
        self.state = state.expanduser().resolve()
        self.gui = gui.expanduser().resolve()
        self.home = self.state / "alx"
        self.home.mkdir(parents=True, exist_ok=True)
        private = self.state / "bin/alx"
        self.binary = str(binary or (private if private.exists() else shutil.which("alx") or "alx"))
        self.env = {**os.environ, "ALX_HOME": str(self.home)}

    def run(self, *args, json_output=True):
        result = subprocess.run([self.binary, *args, "--json"], env=self.env, capture_output=True, text=True, timeout=90)
        if result.returncode:
            # Source errors can include credentials or signed URLs.
            raise RuntimeError(f"alx {args[0]} {args[1] if len(args)>1 else ''} 失败（退出码 {result.returncode}）")
        return json.loads(result.stdout) if json_output else None

    def prepare(self, playlist, tracks, output):
        settings = json.loads((self.gui / "config_v2.json").read_text())["setting"]
        api = next(s for s in json.loads((self.gui / "user_api.json").read_text())["userApis"] if s["id"] == settings["common.apiSource"])
        script = api["script"]
        if script.startswith("gz_"):
            script = zlib.decompress(base64.b64decode(script[3:])).decode()
        source_path = self.home / "selected-source.js"
        source_path.write_text(script)
        source_path.chmod(0o600)
        self.run("source", "add", str(source_path), json_output=False)
        source = next(s for s in self.run("source", "list") if s["name"] == api["name"])
        Path(source["script_path"]).chmod(0o600)
        config = self.run("config")
        config["source"].update(priority=[source["id"]], js_priority=True)
        config["download"].update(output_dir=str(output), filename_template="{source}_{id}",
            embed_metadata=True, embed_cover=True, embed_lyrics=True,
            save_lyrics_file=True, lrc_encoding="utf8", max_concurrent=3,
            beet_import=False, use_beets_library=False)
        if settings.get("network.proxy.enable"):
            config["network"]["proxy"] = f'http://{settings["network.proxy.host"]}:{settings["network.proxy.port"]}'
        write_config(self.home / "config.toml", config)
        export = self.home / "selected-playlist.json"
        export.write_text(json.dumps([dict(title=t["title"], artist=t["artist"], album=t["album"], song_id=t["song_id"], source=t["source"]) for t in tracks], ensure_ascii=False))
        names = {p[0] for p in self.run("playlist", "list")}
        if playlist["id"] in names:
            self.run("playlist", "delete", playlist["id"])
        self.run("playlist", "import", str(export), "--name", playlist["id"])
        db = sqlite3.connect(self.home / "data/agent-lx-music.db")
        now = datetime.now(timezone.utc).isoformat()
        with db:
            for track in tracks:
                meta = track["meta"]
                db.execute("UPDATE search_cache SET interval=?,album_id=?,pic_url=?,extra=? WHERE song_id=? AND source=?",
                    (track["interval"], str(meta.get("albumId", "")), meta.get("picUrl"), json.dumps(meta, ensure_ascii=False), track["song_id"], track["source"]))
                if track["lrc"]:
                    db.execute("INSERT OR REPLACE INTO lyrics_cache(song_id,source,lyric,cached_at) VALUES(?,?,?,?)",
                        (track["song_id"], track["source"], track["lrc"], now))
        db.close()
        cached = {(s["source"], s["song_id"]): s["cli_id"] for s in self.run("playlist", "show", playlist["id"])}
        for track in tracks:
            track["cli_id"] = cached[track["source"], track["song_id"]]
        print(f"音源：{api['name']}；歌单：{playlist['name']}；选中 {len(tracks)} 首", flush=True)

    def download(self, tracks, output, quality, timeout):
        output.mkdir(parents=True, exist_ok=True)
        pending = []
        for track in tracks:
            track["file"] = str(output / f"{track['source']}_{track['song_id']}.mp3")
            if not Path(track["file"]).exists():
                pending.append(track["cli_id"])
        if pending:
            self.run("download", "add", *pending, "--quality", quality)
        deadline = time.monotonic() + timeout
        finished = set()
        while len(finished) < len(tracks):
            with sqlite3.connect(self.home / "data/agent-lx-music.db") as db:
                states = {(s, i): status for s, i, status in db.execute("SELECT source,song_id,status FROM downloads")}
            for index, track in enumerate(tracks):
                if index in finished:
                    continue
                path = Path(track["file"])
                if path.exists():
                    self.finish(track, quality)
                    finished.add(index)
                    print(f"[{len(finished)}/{len(tracks)}] {track['title']} · {track['kbps']} kbps · 歌词 {track['lyrics_characters']} 字", flush=True)
                elif states.get((track["source"], track["song_id"])) == "failed":
                    track["status"] = "failed"
                    track["error"] = "音源解析或下载失败；重新运行 download 可重试"
                    finished.add(index)
                    print(f"下载失败：{track['title']}", flush=True)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"下载超过 {timeout} 秒；已完成文件保留，可重新运行")
            if len(finished) < len(tracks):
                time.sleep(1)
        return tracks

    def finish(self, track, quality):
        path = Path(track["file"])
        audio = MP3(path)
        lyrics = track["lrc"]
        if not lyrics and audio.tags and audio.tags.getall("USLT"):
            lyrics = audio.tags.getall("USLT")[0].text
        if lyrics:
            path.with_suffix(".lrc").write_text(lyrics)
            audio.tags.delall("USLT")
            audio.tags.add(USLT(encoding=3, lang="und", desc="", text=plain_lyrics(lyrics)))
            audio.save()
        track.update(status="downloaded", requested_quality=quality,
            kbps=round(audio.info.bitrate / 1000), duration_ms=round(audio.info.length * 1000),
            lyrics_characters=len(plain_lyrics(lyrics)), cover=bool(audio.tags and audio.tags.getall("APIC")),
            lrc_file=str(path.with_suffix(".lrc")) if lyrics else None)
