"""LX playlist download and incremental USB library import."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from . import device
from .lx import Downloader, default_gui_dir, playlists, select_playlist


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


async def connected():
    from pymobiledevice3.usbmux import list_devices
    entries = await list_devices()
    result = []
    for entry in entries:
        if str(entry.connection_type) != "USB":
            continue
        phone = await device.create_using_usbmux(serial=entry.serial, autopair=False, connection_type="USB")
        try:
            result.append(dict(udid=entry.serial, name=phone.all_values.get("DeviceName"),
                model=phone.all_values.get("ProductType"), ios=phone.all_values.get("ProductVersion")))
        finally:
            await phone.close()
    return result


def ensure_playlist_member(db, item, name):
    row = db.execute("SELECT container_pid FROM container WHERE name=? AND distinguished_kind=0", (name,)).fetchone()
    now = int(time.time() - 978307200)
    if row:
        container = row[0]
    else:
        container = device.pid()
        order, _ = device.sort_name(db, name)
        device.insert(db, "container", container_pid=container, name=name, name_order=order,
            date_created=now, date_modified=now, contained_media_type=8, is_owner=1, is_editable=1)
    if db.execute("SELECT 1 FROM container_item WHERE container_pid=? AND item_pid=?", (container, item)).fetchone():
        return False
    position = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM container_item WHERE container_pid=?", (container,)).fetchone()[0]
    device.insert(db, "container_item", container_item_pid=device.pid(), container_pid=container,
        item_pid=item, position=position, uuid=str(uuid.uuid4()).upper())
    db.execute("UPDATE container SET date_modified=? WHERE container_pid=?", (now, container))
    return True


def quit_music(udid):
    if shutil.which("ios-use"):
        result = subprocess.run(["ios-use", "terminateApp", "com.apple.Music", "--udid", udid],
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("无法退出设备上的音乐 App，请手动退出后重试")
    else:
        print("同步前请确认设备上的音乐 App 已退出。", file=sys.stderr)


async def import_batch(tracks, playlist, udid, state):
    files = [(track, Path(track["file"]), Path(track["lrc_file"]) if track.get("lrc_file") else None)
             for track in tracks if track.get("status") == "downloaded"]
    songs = [(track, path, device.song_metadata(path, lrc)) for track, path, lrc in files]
    if not songs:
        raise ValueError("没有成功下载的 MP3 可以同步")
    quit_music(udid)
    phone = await device.create_using_usbmux(serial=udid, autopair=False, connection_type="USB")
    session = state / "backups" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    uploaded = []
    committed = False
    try:
        async with device.AfcService(phone) as afc:
            await device.notify(phone, "syncWillStart")
            original = await device.download_database(afc, session / "original")
            shutil.copytree(original.parent, session / "staged")
            staged = session / "staged" / device.DB_NAME
            db = sqlite3.connect(staged)
            receipts = []
            media = []
            artwork = []
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
                with db:
                    for track, path, song in songs:
                        filename = "LX_" + uuid.uuid4().hex + ".mp3"
                        item, added = device.add_song(db, song, filename, None)
                        linked = ensure_playlist_member(db, item, playlist)
                        if added:
                            media.append(("/" + device.MUSIC_FOLDER + "/" + filename, path))
                            art = device.register_artwork(db, item, song["cover"])
                            if art:
                                artwork.append((device.ITUNES + "/Artwork/Originals/" + art[0], art[1]))
                        receipts.append(dict(title=song["title"], artist=song["artist"], item_pid=item,
                            status="added" if added else "already_present", playlist_added=linked,
                            kbps=song["bitrate"], lyrics_characters=len(song["lyrics"]), cover=bool(song["cover"])))
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("媒体库检查失败")
            finally:
                db.close()
            changed = bool(media) or any(row["playlist_added"] for row in receipts)
            if changed:
                await afc.makedirs("/" + device.MUSIC_FOLDER)
                for remote, path in media:
                    await afc.set_file_contents(remote, path.read_bytes())
                    uploaded.append(remote)
                    print(f"已上传音频：{path.name}", flush=True)
                for remote, data in artwork:
                    await afc.makedirs(str(Path(remote).parent))
                    await afc.set_file_contents(remote, data)
                    uploaded.append(remote)
                await device.replace_database(afc, staged, session.name)
                committed = True
            # Read the real device back; the staged database alone is not proof.
            snapshot = await device.download_database(afc, session / "readback")
            with sqlite3.connect(snapshot) as actual:
                integrity = actual.execute("PRAGMA quick_check").fetchone()[0]
                order = actual.execute("""SELECT e.title FROM container_item ci JOIN container c USING(container_pid)
                    JOIN item_extra e USING(item_pid) WHERE c.name=? ORDER BY ci.position""", (playlist,)).fetchall()
                count = actual.execute("SELECT COUNT(*) FROM item").fetchone()[0]
                for receipt in receipts:
                    receipt["download_kbps"] = receipt["kbps"]
                    bitrate, lyric_length = actual.execute("""SELECT p.bit_rate,LENGTH(l.lyrics)
                        FROM item_playback p LEFT JOIN lyrics l USING(item_pid)
                        WHERE p.item_pid=?""", (receipt["item_pid"],)).fetchone()
                    receipt["kbps"] = bitrate
                    receipt["lyrics_characters"] = lyric_length or 0
            result = dict(playlist=playlist, tracks=receipts, total_library_tracks=count,
                playlist_order=[r[0] for r in order], integrity=integrity, backup=str(session / "original"))
            save(session / "receipt.json", result)
            return result
    except BaseException:
        if not committed:
            async with device.AfcService(phone) as afc:
                for remote in uploaded:
                    await afc.rm_single(remote, force=True)
        raise
    finally:
        try:
            await device.notify(phone, "syncDidFinish")
        finally:
            await phone.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui-dir", type=Path, default=default_gui_dir())
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/share/lx-apple-sync")
    parser.add_argument("--alx", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("playlists", help="查看 LX GUI 的歌单")
    commands.add_parser("devices", help="列出已配对的 USB iPhone / iPad")
    for name in ("download", "sync"):
        action = commands.add_parser(name, help="批量下载" if name == "download" else "下载并增量导入原生音乐")
        action.add_argument("playlist")
        action.add_argument("--limit", type=int)
        action.add_argument("--quality", choices=["128k", "320k"], default="320k")
        action.add_argument("--output", type=Path, default=Path.home() / "Music/LX-Apple-Sync")
        action.add_argument("--source-script", type=Path, help="本次使用的 LX JS 音源；省略时继承 GUI 当前音源")
        action.add_argument("--timeout", type=int, default=600)
        if name == "sync":
            action.add_argument("--udid")
    args = parser.parse_args()
    try:
        if args.command == "playlists":
            emit(playlists(args.gui_dir))
            return
        if args.command == "devices":
            emit(asyncio.run(connected()))
            return
        if args.limit is not None and args.limit <= 0:
            raise ValueError("--limit 必须大于 0")
        playlist, tracks = select_playlist(args.gui_dir, args.playlist, args.limit)
        engine = Downloader(args.state_dir, args.gui_dir, args.alx)
        output = args.output.expanduser().resolve()
        source_name = engine.prepare(playlist, tracks, output, args.source_script)
        tracks = engine.download(tracks, output, args.quality, args.timeout)
        manifest = dict(playlist=playlist, source_name=source_name,
            tracks=[{k: v for k, v in t.items() if k not in ("meta", "lrc")} for t in tracks])
        save(engine.state / "manifests" / (playlist["id"] + ".json"), manifest)
        if args.command == "sync":
            udid = args.udid
            if not udid:
                devices = asyncio.run(connected())
                if len(devices) != 1:
                    raise ValueError("需要恰好一台已配对 USB 设备，或使用 --udid 选择目标")
                udid = devices[0]["udid"]
            result = asyncio.run(import_batch(tracks, playlist["name"], udid, engine.state))
            save(engine.state / "last-sync.json", result)
            emit(result)
        else:
            emit(manifest)
        if any(t.get("status") != "downloaded" for t in tracks):
            raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
