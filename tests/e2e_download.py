"""Exercise the installed CLI and real alx against a local, generated-audio source.

No private LX data, third-party music, or physical device is used.
"""
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from mutagen.mp3 import MP3


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    fixtures = Path(__file__).resolve().parent / "fixtures"
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(fixtures)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    lyrics = "[00:00.00]中文歌词 テスト\n[00:02.00]Generated test audio"
    script = """/*!
 * @name Local E2E 测试源
 * @version 1.0.0
 */
lx.send('inited', {status: true, sources: {kw: {type: 'music', qualitys: ['128k']}}});
lx.on('request', async ({action}) => {
  if (action === 'musicUrl') return AUDIO_URL;
  if (action === 'lyric') return {lyric: LYRICS};
  throw new Error('Unsupported test action: ' + action);
});
""".replace("AUDIO_URL", json.dumps(origin + "/tone.mp3")).replace("LYRICS", json.dumps(lyrics))
    with tempfile.TemporaryDirectory(prefix="lxsync-e2e-") as temporary:
        root = Path(temporary)
        appdata = root / "App Data 中文"
        gui = appdata / "lx-music-desktop/LxDatas"
        state = root / "状态 目录"
        output = root / "音乐 输出"
        gui.mkdir(parents=True)
        (state / "bin").mkdir(parents=True)
        name = "alx.exe" if sys.platform == "win32" else "alx"
        installed = Path(os.environ.get("LXSYNC_STATE_DIR", Path.home() / ".local/share/lx-apple-sync"))
        shutil.copy2(installed / "bin" / name, state / "bin" / name)
        (gui / "config_v2.json").write_text(json.dumps({"setting": {"common.apiSource": "test"}}), encoding="utf-8-sig")
        (gui / "user_api.json").write_text(json.dumps({"userApis": [{"id": "test", "script": script}]}, ensure_ascii=False), encoding="utf-8")
        titles = ["中文曲目", "日本語の曲"]
        with sqlite3.connect(gui / "lx.data.db") as db:
            db.executescript('''
                CREATE TABLE my_list (id TEXT, name TEXT, position INTEGER);
                CREATE TABLE my_list_music_info (id TEXT, listId TEXT, source TEXT, name TEXT, singer TEXT, interval TEXT, meta TEXT);
                CREATE TABLE my_list_music_info_order (listId TEXT, musicInfoId TEXT, "order" INTEGER);
                CREATE TABLE lyric (id TEXT, source TEXT, type TEXT, text TEXT);
                INSERT INTO my_list VALUES ('test-list', '测试歌单', 0);
            ''')
            for index, title in enumerate(titles):
                identifier = "kw_" + str(index)
                meta = dict(songId=str(index), albumName="测试专辑", picUrl=origin + "/cover.jpg")
                db.execute("INSERT INTO my_list_music_info VALUES (?, 'test-list', 'kw', ?, 'テスト歌手', '00:05', ?)",
                    (identifier, title, json.dumps(meta, ensure_ascii=False)))
                db.execute('INSERT INTO my_list_music_info_order VALUES (\'test-list\', ?, ?)', (identifier, index))
                db.execute("INSERT INTO lyric VALUES (?, 'raw', 'lyric', ?)", (identifier, base64.b64encode(lyrics.encode()).decode()))
        env = {**os.environ, "APPDATA": str(appdata)}
        cli = [shutil.which("lxsync"), "--state-dir", str(state)]
        if sys.platform != "win32":
            cli += ["--gui-dir", str(gui)]

        def run(*args):
            result = subprocess.run([*cli, *args], env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
            print(result.stdout, end="")
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout

        try:
            lists = json.loads(run("playlists"))
            assert lists[0]["songs"] == 2
            args = ["download", "测试歌单", "--output", str(output), "--quality", "128k", "--timeout", "120"]
            run(*args)
            manifest = state / "manifests/test-list.json"
            tracks = json.loads(manifest.read_text(encoding="utf-8"))["tracks"]
            assert len(tracks) == 2 and all(t["status"] == "downloaded" for t in tracks)
            for index, track in enumerate(tracks):
                audio = MP3(track["file"])
                assert 4.5 < audio.info.length < 6
                assert str(audio.tags["TIT2"]) == titles[index]
                assert audio.tags.getall("APIC"), "Cover did not reach the actual MP3"
                assert "中文歌词 テスト" in audio.tags.getall("USLT")[0].text
                assert "中文歌词 テスト" in Path(track["lrc_file"]).read_text(encoding="utf-8-sig")
            downloads = requests.count("/tone.mp3")
            assert downloads == 2, requests
            run(*args)
            assert requests.count("/tone.mp3") == downloads, "Repeat downloaded the audio again"
            with sqlite3.connect(gui / "lx.data.db") as db:
                assert db.execute("SELECT COUNT(*) FROM my_list_music_info").fetchone()[0] == 2
            print("PASS: real source execution, 2 MP3 downloads, Unicode paths/tags/lyrics, covers, and cached repeat")
        finally:
            pidfile = state / "alx/cache/download.pid"
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                time.sleep(0.2)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
