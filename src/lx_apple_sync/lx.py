"""Read LX GUI data; delegate source execution and downloads to isolated alx."""
import base64
import json
import html
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen
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


def seconds(interval):
    return sum(int(value) * 60 ** position for position, value in enumerate(reversed(interval.split(":"))))


def same_name(value):
    return "".join(unicodedata.normalize("NFKC", html.unescape(value)).casefold().split())


def provider_duration(track):
    if track["source"] != "wy":
        return None
    url = "https://music.163.com/api/song/detail?" + urlencode({"id": track["song_id"], "ids": "[" + track["song_id"] + "]"})
    try:
        with urlopen(url, timeout=15) as response:
            songs = json.load(response).get("songs", [])
        for song in songs:
            if (str(song["id"]) == track["song_id"] and same_name(song["name"]) == same_name(track["title"])
                and same_name(song["album"]["name"]) == same_name(track["album"])
                and same_name("、".join(a["name"] for a in song["artists"])) == same_name(track["artist"])):
                return song["duration"] / 1000
    except (OSError, ValueError, KeyError):
        pass
    return None


def missing_lyrics(text):
    plain = plain_lyrics(text)
    return not plain or ("纯音乐" in plain and len(plain) < 100) or bool(re.fullmatch(r"Object\(0x[0-9a-fA-F]+\)", plain))


def kugou_lyrics(track, song_id, duration):
    def fetch(path, params):
        request = Request("http://lyrics.kugou.com/" + path + "?" + urlencode(params),
            headers={"User-Agent": "KuGou2012-9020-ExpandSearchManager"})
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    try:
        result = fetch("search", dict(ver=1, man="yes", client="pc", hash=song_id,
            keyword=track["title"], timelength=round(duration), lrctxt=1))
        base = same_name(track["title"].split(" / ")[0])
        candidate = next((c for c in result.get("candidates", []) if same_name(c.get("song") or "").startswith(base)
            and abs(c.get("duration", 0) / 1000 - duration) <= 3), None)
        if candidate:
            data = fetch("download", dict(ver=1, client="pc", id=candidate["id"],
                accesskey=candidate["accesskey"], fmt="lrc", charset="utf8"))
            return base64.b64decode(data["content"]).decode("utf-8-sig")
    except (OSError, ValueError, KeyError):
        pass
    return ""


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

    def run(self, *args, json_output=True, home=None):
        env = self.env if home is None else {**self.env, "ALX_HOME": str(home)}
        result = subprocess.run([self.binary, *args, "--json"], env=env, capture_output=True, text=True, timeout=90)
        if result.returncode:
            # Source errors can include credentials or signed URLs.
            raise RuntimeError(f"alx {args[0]} {args[1] if len(args)>1 else ''} 失败（退出码 {result.returncode}）")
        return json.loads(result.stdout) if json_output else None

    def prepare(self, playlist, tracks, output, source_script=None):
        self.playlist = playlist
        manifest = self.state / "manifests" / (playlist["id"] + ".json")
        if manifest.exists():
            previous = {t["gui_id"]: t for t in json.loads(manifest.read_text())["tracks"]}
            for track in tracks:
                old = previous.get(track["gui_id"], {})
                canonical = output / f"{track['source']}_{track['song_id']}.mp3"
                if canonical.exists() and old.get("file") == str(canonical):
                    for key in ("resolved_source", "resolved_song_id", "resolved_title", "resolved_artist", "resolved_album", "verified_duration_ms", "duration_source"):
                        if key in old:
                            track.setdefault(key, old[key])
        settings = json.loads((self.gui / "config_v2.json").read_text())["setting"]
        if source_script:
            script = source_script.expanduser().read_text()
        else:
            api = next((s for s in json.loads((self.gui / "user_api.json").read_text())["userApis"] if s["id"] == settings["common.apiSource"]), None)
            if not api:
                raise ValueError("请在 LX 选中自定义音源，或用 --source-script 指定音源脚本")
            script = api["script"]
            if script.startswith("gz_"):
                script = zlib.decompress(base64.b64decode(script[3:])).decode()
        source_path = self.home / "selected-source.js"
        source_path.write_text(script)
        source_path.chmod(0o600)
        self.run("source", "add", str(source_path), json_output=False)
        source = next(s for s in self.run("source", "list") if Path(s["script_path"]).read_text() == script)
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
            db.execute("UPDATE sources SET enabled=(id=?)", (source["id"],))
            # A new explicit batch should try its selected source again.
            db.execute("UPDATE source_health SET consecutive_fails=0,circuit_broken_until=NULL WHERE source_id=?", (source["id"],))
            for track in tracks:
                meta = track["meta"]
                db.execute("UPDATE search_cache SET interval=?,album_id=?,pic_url=?,extra=?,hash=?,songmid=? WHERE song_id=? AND source=?",
                    (track["interval"], str(meta.get("albumId", "")), meta.get("picUrl"), json.dumps(meta, ensure_ascii=False),
                     meta.get("hash"), str(meta.get("songmid") or track["song_id"]), track["song_id"], track["source"]))
                if track["lrc"]:
                    db.execute("INSERT OR REPLACE INTO lyrics_cache(song_id,source,lyric,cached_at) VALUES(?,?,?,?)",
                        (track["song_id"], track["source"], track["lrc"], now))
        db.close()
        cached = {(s["source"], s["song_id"]): s["cli_id"] for s in self.run("playlist", "show", playlist["id"])}
        for track in tracks:
            track["cli_id"] = cached[track["source"], track["song_id"]]
        print(f"音源：{source['name']}；歌单：{playlist['name']}；选中 {len(tracks)} 首", flush=True)
        return source["name"]

    def download(self, tracks, output, quality, timeout):
        output.mkdir(parents=True, exist_ok=True)
        pending = []
        for track in tracks:
            track["file"] = str(output / f"{track['source']}_{track['song_id']}.mp3")
            if not Path(track["file"]).exists():
                pending.append(track["cli_id"])
        if pending:
            # alx ignores a song already in its queue, including failed tasks.
            # Requeue only missing files in this batch; leave active tasks alone.
            with sqlite3.connect(self.home / "data/agent-lx-music.db") as db:
                for track in tracks:
                    if track["cli_id"] in pending:
                        db.execute("DELETE FROM downloads WHERE source=? AND song_id=? AND status IN ('failed','completed')",
                            (track["source"], track["song_id"]))
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
                    if track["status"] == "downloaded":
                        print(f"[{len(finished)}/{len(tracks)}] {track['title']} · {track['kbps']} kbps · 歌词 {track['lyrics_characters']} 字", flush=True)
                    else:
                        print(f"下载失败：{track['title']} · {track['error']}", flush=True)
                elif states.get((track["source"], track["song_id"])) == "failed":
                    track["status"] = "failed"
                    track["error"] = "音源解析或下载失败；重新运行 download 可重试"
                    finished.add(index)
                    print(f"下载失败：{track['title']}", flush=True)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"下载超过 {timeout} 秒；已完成文件保留，可重新运行")
            if len(finished) < len(tracks):
                time.sleep(1)
        return self.recover(tracks, output, quality, timeout)

    def recover(self, tracks, output, quality, timeout):
        failed = [t for t in tracks if t.get("status") == "failed" and t["source"] != "kg" and t["album"] and t["interval"]]
        if not failed:
            return tracks
        print("原 ID 下载失败，按原版专辑、曲名和时长查找酷狗可用版本……", flush=True)
        matched = []
        for track in failed:
            base_title = track["title"].split(" / ")[0]
            queries = [track["title"], track["title"] + " " + re.sub(r"[^\w\s]", " ", track["album"]), base_title]
            choice = None
            for query in dict.fromkeys(queries):
                try:
                    choices = self.run("search", query, "--source", "kg", "--limit", "50", home=self.state / "catalog")
                except (RuntimeError, subprocess.TimeoutExpired):
                    continue
                versions = [c for c in choices if same_name(c.get("album_name") or "") == same_name(track["album"])
                    and c.get("interval") and abs(seconds(c["interval"]) - seconds(track["interval"])) <= 3]
                original_name = lambda c: json.loads(c.get("extra") or "{}").get("original_name") or c["name"]
                choice = next((c for c in versions if same_name(original_name(c)) == same_name(track["title"])), None)
                if not choice and base_title != track["title"]:
                    # Platforms can attach different version labels; require a
                    # single recording with this base title, album and duration.
                    candidates = {c["song_id"]: c for c in versions if same_name(original_name(c)) == same_name(base_title)}
                    if len(candidates) == 1:
                        choice = next(iter(candidates.values()))
                if choice:
                    break
            if not choice:
                continue
            meta = json.loads(choice.get("extra") or "{}")
            meta.update(albumId=choice.get("album_id"), picUrl=track["meta"].get("picUrl"))
            alternative = {**track, "source": "kg", "song_id": choice["song_id"], "meta": meta}
            matched.append((track, alternative, choice))
            print(f"匹配原版：{track['title']} · {choice['album_name']}", flush=True)
        if not matched:
            return tracks
        playlist = {**self.playlist, "id": self.playlist["id"] + "-kg"}
        alternatives = [alternative for _, alternative, _ in matched]
        self.prepare(playlist, alternatives, output, self.home / "selected-source.js")
        config = self.run("config")
        config["source"]["js_priority"] = False
        write_config(self.home / "config.toml", config)
        self.download(alternatives, output, quality, timeout)
        for track, alternative, choice in matched:
            if alternative["status"] != "downloaded":
                continue
            canonical = output / f"{track['source']}_{track['song_id']}.mp3"
            Path(alternative["file"]).replace(canonical)
            lrc = Path(alternative["lrc_file"]) if alternative.get("lrc_file") else None
            if lrc:
                lrc.replace(canonical.with_suffix(".lrc"))
            track.update({key: alternative[key] for key in ("status", "requested_quality", "kbps", "duration_ms", "lyrics_characters", "cover")})
            track.update(file=str(canonical), lrc_file=str(canonical.with_suffix(".lrc")) if lrc else None,
                resolved_source="kg", resolved_song_id=alternative["song_id"],
                resolved_title=choice["name"], resolved_artist=choice["singer"], resolved_album=choice["album_name"])
            track.pop("error", None)
        return tracks

    def finish(self, track, quality):
        path = Path(track["file"])
        audio = MP3(path)
        if track["interval"]:
            expected = seconds(track["interval"])
            if expected and abs(audio.info.length - expected) > max(3, expected * 0.02):
                confirmed = track["verified_duration_ms"] / 1000 if track.get("verified_duration_ms") else provider_duration(track)
                if confirmed:
                    expected = confirmed
                    track["verified_duration_ms"] = round(confirmed * 1000)
                    track["duration_source"] = "netease-song-detail"
            if expected and abs(audio.info.length - expected) > max(3, expected * 0.02):
                rejected = path.with_name(f"{path.stem}.rejected-{time.time_ns()}.mp3")
                path.rename(rejected)
                track.update(status="failed", rejected_file=str(rejected),
                    error=f"音源返回 {audio.info.length:.1f} 秒音频，与 LX 时长 {track['interval']} 不符；已保留样本，重新运行可重试")
                return
        lyrics = track["lrc"]
        if missing_lyrics(lyrics) and path.with_suffix(".lrc").exists():
            lyrics = path.with_suffix(".lrc").read_text(encoding="utf-8-sig")
        if not lyrics and audio.tags and audio.tags.getall("USLT"):
            lyrics = audio.tags.getall("USLT")[0].text
        if re.fullmatch(r"Object\(0x[0-9a-fA-F]+\)", lyrics.strip()):
            # Earlier alx versions serialized a JS object pointer as lyric text.
            lyrics = ""
            if path.with_suffix(".lrc").exists():
                path.with_suffix(".lrc").replace(path.with_suffix(".invalid-text.lrc"))
            audio.tags.delall("USLT")
            audio.save()
        kg_id = track["song_id"] if track["source"] == "kg" else track.get("resolved_song_id") if track.get("resolved_source") == "kg" else None
        if kg_id and missing_lyrics(lyrics):
            fetched = kugou_lyrics(track, kg_id, audio.info.length)
            if fetched and not missing_lyrics(fetched):
                lyrics = fetched
        if lyrics:
            path.with_suffix(".lrc").write_text(lyrics)
            audio.tags.delall("USLT")
            audio.tags.add(USLT(encoding=3, lang="und", desc="", text=plain_lyrics(lyrics)))
            audio.save()
        track.update(status="downloaded", requested_quality=quality,
            kbps=round(audio.info.bitrate / 1000), duration_ms=round(audio.info.length * 1000),
            lyrics_characters=len(plain_lyrics(lyrics)), cover=bool(audio.tags and audio.tags.getall("APIC")),
            lrc_file=str(path.with_suffix(".lrc")) if lyrics else None)
