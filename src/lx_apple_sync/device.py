"""Native Music library helpers, adapted from the verified USB prototype.

MIT License
Copyright (c) 2026 Edualexxis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


import json
import hashlib
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import uuid

from mutagen.mp3 import MP3
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.notification_proxy import NotificationProxyService

ITUNES = "/iTunes_Control/iTunes"
DB_NAME = "MediaLibrary.sqlitedb"
MUSIC_FOLDER = "iTunes_Control/Music/F00"


def pid():
    return uuid.uuid4().int & ((1 << 63) - 1)


def insert(db, table, **values):
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))


def grouping_key(name):
    return bytes(ord(c) - 64 if "A" <= c <= "Z" else {" ": 4, "/": 10}[c]
                 for c in name.upper() if "A" <= c <= "Z" or c in " /")


def sort_name(db, name):
    row = db.execute("SELECT name_order,name_section FROM sort_map WHERE name=?", (name,)).fetchone()
    if row:
        return row
    order = db.execute("SELECT COALESCE(MAX(name_order),0)+4294967296 FROM sort_map").fetchone()[0]
    first = unicodedata.normalize("NFD", name.upper())[:1]
    section = ord(first) - 65 if first and "A" <= first <= "Z" else 26
    insert(db, "sort_map", name=name, name_order=order, name_section=section, sort_key=grouping_key(name))
    return order, section


def entity(db, table, id_column, name_column, name, **values):
    query = f"SELECT {id_column} FROM {table} WHERE {name_column}=?"
    parameters = [name]
    if table == "album":
        query += " AND album_artist_pid=?"
        parameters.append(values["album_artist_pid"])
    row = db.execute(query, parameters).fetchone()
    if row:
        return row[0]
    identifier = pid()
    insert(db, table, **{id_column: identifier, name_column: name}, grouping_key=grouping_key(name), **values)
    return identifier


def song_metadata(path, lrc, extra=None):
    audio = MP3(path)
    tags = audio.tags

    def text(key, fallback=""):
        frame = tags.get(key) if tags else None
        return str(frame.text[0]) if frame and frame.text else fallback

    def numbers(key):
        raw = text(key)
        parts = raw.split("/") if raw else []
        return (int(parts[0]) if parts else 0, int(parts[1]) if len(parts) > 1 else 0)

    lyrics = ""
    if lrc:
        lyrics = re.sub(r"\[[^\]\n]*\]", "", lrc.read_text(encoding="utf-8-sig"))
        lyrics = "\n".join(line.strip() for line in lyrics.splitlines() if line.strip())
    elif tags and tags.getall("USLT"):
        lyrics = tags.getall("USLT")[0].text
    track, track_count = numbers("TRCK")
    disc, disc_count = numbers("TPOS")
    credit = re.search(r"^(?:作曲|Composer)\s*[:：]\s*(.+)$", lyrics, re.MULTILINE)
    covers = tags.getall("APIC") if tags else []
    cover = next((f for f in covers if f.type == 3), covers[0] if covers else None)
    song = dict(title=text("TIT2", path.stem), artist=text("TPE1", "Unknown Artist"),
                album=text("TALB", "Unknown Album"), album_artist=text("TPE2", text("TPE1", "Unknown Artist")),
                genre=text("TCON", "Unknown"), track=track, track_count=track_count,
                disc=disc, disc_count=disc_count, composer=text("TCOM", credit[1].strip() if credit else ""),
                copyright=text("TCOP"), cover=cover.data if cover else None,
                year=int(text("TDRC", "0")[:4]), duration_ms=round(audio.info.length * 1000),
                bitrate=round(audio.info.bitrate / 1000), sample_rate=audio.info.sample_rate,
                size=path.stat().st_size, lyrics=lyrics)
    if extra:
        for key in ("album_artist", "genre", "year", "track", "track_count", "disc", "disc_count", "composer", "copyright"):
            if key in extra:
                song[key] = extra[key]
    return song


def find_song(db, song):
    return db.execute("""SELECT i.item_pid FROM item i JOIN item_extra e USING(item_pid)
        JOIN item_artist a ON a.item_artist_pid=i.item_artist_pid
        JOIN album b ON b.album_pid=i.album_pid
        WHERE e.title=? AND a.item_artist=? AND b.album=?""",
        (song["title"], song["artist"], song["album"])).fetchone()


def add_song(db, song, filename, playlist):
    existing = find_song(db, song)
    if existing:
        return existing[0], False
    item = pid()
    now = int(time.time() - 978307200)  # Apple's 2001 reference date.
    title_order, title_section = sort_name(db, song["title"])
    artist_order, artist_section = sort_name(db, song["artist"])
    aa_order, aa_section = sort_name(db, song["album_artist"])
    album_order, album_section = sort_name(db, song["album"])
    genre_order, genre_section = sort_name(db, song["genre"])
    artist = entity(db, "item_artist", "item_artist_pid", "item_artist", song["artist"],
                    sort_item_artist=song["artist"], sync_id=pid(), keep_local=1, representative_item_pid=item)
    album_artist = entity(db, "album_artist", "album_artist_pid", "album_artist", song["album_artist"],
                          sort_album_artist=song["album_artist"], sync_id=pid(), keep_local=1,
                          representative_item_pid=item, sort_order=aa_order, name_order=aa_order,
                          sort_order_section=aa_section)
    album = entity(db, "album", "album_pid", "album", song["album"], sort_album=song["album"],
                   album_artist_pid=album_artist, album_year=song["year"], sync_id=pid(),
                   keep_local=1, representative_item_pid=item)
    genre = entity(db, "genre", "genre_id", "genre", song["genre"], representative_item_pid=item)
    composer, composer_order, composer_section = composer_info(db, song["composer"], item)
    location = db.execute("SELECT base_location_id FROM base_location WHERE path=?", (MUSIC_FOLDER,)).fetchone()
    if location:
        base = location[0]
    else:
        base = max(3840, db.execute("SELECT COALESCE(MAX(base_location_id),0)+1 FROM base_location").fetchone()[0])
        insert(db, "base_location", base_location_id=base, path=MUSIC_FOLDER)
    insert(db, "item", item_pid=item, media_type=8,
           title_order=title_order, title_order_section=title_section,
           item_artist_pid=artist, item_artist_order=artist_order, item_artist_order_section=artist_section,
           album_pid=album, album_order=album_order, album_order_section=album_section,
           album_artist_pid=album_artist, album_artist_order=aa_order, album_artist_order_section=aa_section,
           genre_id=genre, genre_order=genre_order, genre_order_section=genre_section,
           composer_pid=composer, composer_order=composer_order, composer_order_section=composer_section,
           series_name_order_section=26,
           disc_number=song["disc"], track_number=song["track"], episode_sort_id=1,
           base_location_id=base, keep_local=1, keep_local_status=2, in_my_library=1,
           date_added=now, date_downloaded=now)
    insert(db, "item_extra", item_pid=item, title=song["title"], sort_title=song["title"],
           disc_count=song["disc_count"], track_count=song["track_count"],
           total_time_ms=song["duration_ms"], year=song["year"], copyright=song["copyright"],
           location=filename, file_size=song["size"], integrity=b"", date_modified=now,
           media_kind=1, location_kind_id=42)
    insert(db, "item_playback", item_pid=item, audio_format=301, bit_rate=song["bitrate"],
           codec_type=int.from_bytes(b".mp3", "big"), codec_subtype=0, sample_rate=song["sample_rate"])
    insert(db, "item_stats", item_pid=item, date_accessed=now)
    insert(db, "item_store", item_pid=item, sync_id=pid(), sync_in_my_library=1)
    insert(db, "item_video", item_pid=item)
    insert(db, "item_search", item_pid=item, search_title=title_order, search_album=album_order,
           search_artist=artist_order, search_album_artist=aa_order)
    insert(db, "chapter", item_pid=item)
    insert(db, "lyrics", item_pid=item, lyrics=song["lyrics"])
    if playlist:
        row = db.execute("SELECT container_pid FROM container WHERE name=? AND distinguished_kind=0", (playlist,)).fetchone()
        if row:
            container = row[0]
        else:
            container = pid()
            order, _ = sort_name(db, playlist)
            insert(db, "container", container_pid=container, name=playlist, name_order=order,
                   date_created=now, date_modified=now, contained_media_type=8, is_owner=1, is_editable=1)
        position = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM container_item WHERE container_pid=?", (container,)).fetchone()[0]
        insert(db, "container_item", container_item_pid=pid(), container_pid=container,
               item_pid=item, position=position, uuid=str(uuid.uuid4()).upper())
    return item, True


def composer_info(db, name, item):
    if not name:
        return 0, 0, 26
    order, section = sort_name(db, name)
    composer = entity(db, "composer", "composer_pid", "composer", name,
                      sort_composer=name, sync_id=pid(), keep_local=1, representative_item_pid=item)
    return composer, order, section


def update_song(db, song):
    row = find_song(db, song)
    if not row:
        raise ValueError("Song is not in Music; import it with add first")
    item = row[0]
    aa_order, aa_section = sort_name(db, song["album_artist"])
    album_artist = entity(db, "album_artist", "album_artist_pid", "album_artist", song["album_artist"],
                          sort_album_artist=song["album_artist"], sync_id=pid(), keep_local=1,
                          representative_item_pid=item, sort_order=aa_order, name_order=aa_order,
                          sort_order_section=aa_section)
    album = entity(db, "album", "album_pid", "album", song["album"], sort_album=song["album"],
                   album_artist_pid=album_artist, album_year=song["year"], sync_id=pid(),
                   keep_local=1, representative_item_pid=item)
    db.execute("""UPDATE item SET album_pid=?,album_artist_pid=?,album_artist_order=?,
        album_artist_order_section=? WHERE item_pid=?""", (album, album_artist, aa_order, aa_section, item))
    db.execute("UPDATE item_search SET search_album_artist=? WHERE item_pid=?", (aa_order, item))
    for key, column in [("track", "track_number"), ("disc", "disc_number")]:
        if song[key]:
            db.execute(f"UPDATE item SET {column}=? WHERE item_pid=?", (song[key], item))
    for key in ["year", "track_count", "disc_count", "copyright"]:
        if song[key]:
            db.execute(f"UPDATE item_extra SET {key}=? WHERE item_pid=?", (song[key], item))
    if song["year"]:
        db.execute("UPDATE album SET album_year=? WHERE album_pid=(SELECT album_pid FROM item WHERE item_pid=?)", (song["year"], item))
    if song["genre"] != "Unknown":
        genre = entity(db, "genre", "genre_id", "genre", song["genre"], representative_item_pid=item)
        order, section = sort_name(db, song["genre"])
        db.execute("UPDATE item SET genre_id=?,genre_order=?,genre_order_section=? WHERE item_pid=?", (genre, order, section, item))
    if song["composer"]:
        composer, order, section = composer_info(db, song["composer"], item)
        db.execute("UPDATE item SET composer_pid=?,composer_order=?,composer_order_section=? WHERE item_pid=?", (composer, order, section, item))
        db.execute("UPDATE item_search SET search_composer=? WHERE item_pid=?", (order, item))
    if song["lyrics"]:
        db.execute("UPDATE lyrics SET lyrics=? WHERE item_pid=?", (song["lyrics"], item))
    return item


def register_artwork(db, item, data):
    if not data:
        return None
    token = str(item)
    # Native Artwork/Originals names are derived from the artwork token.
    # This is the device's file format, not a file-integrity check.
    filename = hashlib.sha1(token.encode()).hexdigest()
    relative = filename[:2] + "/" + filename[2:]
    palette = json.dumps({"ColorAnalysis": {"1": {
        "primaryTextColorLight": "NO", "secondaryTextColorLight": "NO",
        "primaryTextColor": "#FFFFFF", "secondaryTextColor": "#FFFFFF",
        "tertiaryTextColor": "#CCCCCC", "tertiaryTextColorLight": "NO",
        "backgroundColorLight": "NO", "backgroundColor": "#333333"}}})
    for source, art_type in [(1, 1), (300, 6)]:
        db.execute("""INSERT OR REPLACE INTO artwork
            (artwork_token,artwork_source_type,relative_path,artwork_type,interest_data,artwork_variant_type)
            VALUES (?,?,?,?,?,0)""", (token, source, relative, art_type, palette))
    album = db.execute("SELECT album_pid FROM item WHERE item_pid=?", (item,)).fetchone()[0]
    for entity_id, entity_type, art_type in [(item, 0, 1), (album, 1, 1), (album, 4, 1), (album, 4, 6)]:
        source = 300 if entity_type == 0 or art_type == 6 else 1
        db.execute("""INSERT OR REPLACE INTO artwork_token
            (entity_pid,entity_type,artwork_token,artwork_source_type,artwork_type,artwork_variant_type)
            VALUES (?,?,?,?,?,0)""", (entity_id, entity_type, token, source, art_type))
        db.execute("""INSERT OR REPLACE INTO best_artwork_token
            (entity_pid,entity_type,artwork_type,available_artwork_token,artwork_variant_type)
            VALUES (?,?,?,?,0)""", (entity_id, entity_type, art_type, token))
    return relative, data


async def download_database(afc, folder):
    folder.mkdir(parents=True)
    names = await afc.listdir(ITUNES)
    for name in (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm"):
        if name in names:
            (folder / name).write_bytes(await afc.get_file_contents(f"{ITUNES}/{name}"))
    return folder / DB_NAME


async def notify(device, name):
    async with NotificationProxyService(device) as proxy:
        await proxy.notify_post("com.apple.itunes." + name)


async def replace_database(afc, staged, backup_suffix):
    remote = ITUNES + "/" + DB_NAME
    temporary = remote + ".lx-temp"
    moved = []
    try:
        await afc.set_file_contents(temporary, staged.read_bytes())
        for suffix in ("-wal", "-shm", ""):
            source = remote + suffix
            if await afc.exists(source):
                target = source + ".lx-backup-" + backup_suffix
                await afc.rename(source, target)
                moved.append((source, target))
        await afc.rename(temporary, remote)
    except BaseException:
        for source, target in reversed(moved):
            await afc.rename(target, source)
        await afc.rm_single(temporary, force=True)
        raise

