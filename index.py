# index.py
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import yt_dlp
import os
import config
import asyncio
import re
import requests
import time
import json
from datetime import datetime, timedelta

# ----------------- CONFIG -----------------
APP_NAME = "yt_insta_bot"  # Client session name (o'zingiz javob berdingiz: bot nomi bor)
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
ANTI_SPAM_COUNT = 5     # 5 ta so'rov
ANTI_SPAM_WINDOW = 5 * 60  # 5 daqiqa (sekundda)
ANTI_SPAM_PUNISH_DAYS = 1  # 1 kun ban
WARN_LIMIT = 3
WARN_BAN_DAYS = 1  # warn 3 → 1 kun ban

# ----------------- FILE NAMES -----------------
ADMINS_FILE = "admins.json"
BANNED_FILE = "banned_users.json"
WARNS_FILE = "warns.json"
HISTORY_FILE = "history.json"
CACHE_FILE = "cache.json"
STATS_FILE = "stats.json"

# ----------------- IN-MEM -----------------
user_links = {}        # user_id -> last url
progress_messages = {} # message.id -> percent
user_warnings = {}     # user_id -> int (kept in memory for quick access; persisted in WARNS_FILE)
anti_spam = {}         # user_id -> list[timestamps]
cache = {}             # url -> { "file": filepath, "type": "audio/video", "title": "...", "time": iso }
banned_users = {}      # loaded from BANNED_FILE
admins = {}            # loaded from ADMINS_FILE
history = {}           # loaded from HISTORY_FILE
stats = {}             # loaded from STATS_FILE

# ----------------- UTIL: file load/save -----------------
def ensure_file(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)

def load_json(path, default):
    ensure_file(path, default)
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except:
            return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# initialize files
admins = load_json(ADMINS_FILE, {})
banned_users = load_json(BANNED_FILE, {})
warns_store = load_json(WARNS_FILE, {})  # { user_id: [ { "time": iso, "reason": str, "source": "auto/manual" }, ... ] }
history = load_json(HISTORY_FILE, {})    # { user_id: [ { "time": iso, "event": str, "note": str }, ... ] }
cache = load_json(CACHE_FILE, {})        # { url: { "file": path, "type": "audio"/"video", "title": "", "time": iso } }
stats = load_json(STATS_FILE, {"downloads":0, "audio":0, "video":0, "users":{}})

# mirror warns_store to memory counts
for uid, entries in warns_store.items():
    try:
        user_warnings[int(uid)] = len(entries)
    except:
        pass

# ----------------- PYROGRAM CLIENT -----------------
app = Client(APP_NAME, api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

# ----------------- HELPERS -----------------
def now_iso():
    return datetime.now().isoformat()

def admin_level(user_id: int) -> int:
    # return admin level (0 if not admin)
    return int(admins.get(str(user_id), 0))

def add_history(user_id: int, event: str, note: str = ""):
    s = {"time": now_iso(), "event": event, "note": note}
    history.setdefault(str(user_id), []).append(s)
    save_json(HISTORY_FILE, history)

def incr_stat(user_id: int, kind: str):
    stats["downloads"] = stats.get("downloads", 0) + 1
    stats[kind] = stats.get(kind, 0) + 1
    stats["users"].setdefault(str(user_id), 0)
    stats["users"][str(user_id)] += 1
    save_json(STATS_FILE, stats)

def save_all():
    save_json(ADMINS_FILE, admins)
    save_json(BANNED_FILE, banned_users)
    save_json(WARNS_FILE, warns_store)
    save_json(HISTORY_FILE, history)
    save_json(CACHE_FILE, cache)
    save_json(STATS_FILE, stats)

# ----------------- BAN / WARN LOGIC -----------------
def is_user_banned(user_id: int) -> bool:
    uid = str(user_id)
    if uid not in banned_users:
        return False
    info = banned_users[uid]
    if info["until"] == "permanent":
        return True
    until_dt = datetime.fromisoformat(info["until"])
    if datetime.now() > until_dt:
        del banned_users[uid]
        save_json(BANNED_FILE, banned_users)
        return False
    return True

def ban_user(user_id: int, days=1, reason="Automatik/Manual ban"):
    uid = str(user_id)
    if days == 0:
        until = "permanent"
    else:
        until = (datetime.now() + timedelta(days=days)).isoformat()
    banned_users[uid] = {"until": until, "reason": reason}
    save_json(BANNED_FILE, banned_users)
    add_history(user_id, "ban", reason)

def warn_add(user_id: int, reason: str, source: str = "auto"):
    uid = str(user_id)
    entry = {"time": now_iso(), "reason": reason, "source": source}
    warns_store.setdefault(uid, []).append(entry)
    save_json(WARNS_FILE, warns_store)
    user_warnings[user_id] = user_warnings.get(user_id, 0) + 1
    add_history(user_id, "warn", reason + f" (source={source})")
    # check limit
    if user_warnings[user_id] >= WARN_LIMIT:
        ban_user(user_id, WARN_BAN_DAYS, "3 warns reached")
        # clear warns after ban
        warns_store[uid] = []
        save_json(WARNS_FILE, warns_store)
        user_warnings[user_id] = 0
        add_history(user_id, "auto-ban", "3 warns")
        return True  # banned
    return False

def warn_clear(user_id: int):
    uid = str(user_id)
    if uid in warns_store:
        warns_store[uid] = []
        save_json(WARNS_FILE, warns_store)
    user_warnings[user_id] = 0
    add_history(user_id, "unwarn", "Cleared by admin")

# ----------------- ANTI-SPAM -----------------
def anti_spam_record(user_id: int) -> bool:
    """Record event, return True if punish (ban) required."""
    now_ts = time.time()
    lst = anti_spam.setdefault(str(user_id), [])
    # remove old timestamps outside window
    cutoff = now_ts - ANTI_SPAM_WINDOW
    lst = [t for t in lst if t >= cutoff]
    lst.append(now_ts)
    anti_spam[str(user_id)] = lst
    # punish if length > threshold
    if len(lst) > ANTI_SPAM_COUNT:
        # punish: 1 day ban
        ban_user(user_id, ANTI_SPAM_PUNISH_DAYS, f"Anti-spam: {len(lst)} in {ANTI_SPAM_WINDOW//60}min")
        add_history(user_id, "antispam-ban", f"{len(lst)} msg in {ANTI_SPAM_WINDOW} sec")
        return True
    return False

# ----------------- EXPLICIT TITLE CHECK -----------------
def is_explicit_title(title: str) -> bool:
    if not title:
        return False
    bad_words = [
        "18+", "sex", "porn", "xxx", "nude", "boobs", "adult", "fuck",
        "erotic", "nsfw", "sexy", "hardcore", "naked", "hot video", "anal",
        "pornhub", "onlyfans"
    ]
    tl = title.lower()
    return any(w in tl for w in bad_words)

# ----------------- PROGRESS HOOK -----------------
async def progress_hook(d, message):
    if d.get('status') != 'downloading':
        return
    percent = d.get('_percent_str', '0.0%').strip()
    total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
    downloaded = d.get('downloaded_bytes', 0)
    if total_bytes:
        done_mb = downloaded / (1024 * 1024)
        total_mb = total_bytes / (1024 * 1024)
        text = f"📥 Yuklanmoqda... {percent}\n💾 {done_mb:.1f} MB / {total_mb:.1f} MB"
    else:
        text = f"📥 Yuklanmoqda... {percent}"
    try:
        mid = message.id
        if mid not in progress_messages or progress_messages[mid] != percent:
            progress_messages[mid] = percent
            await message.edit_text(text)
    except:
        pass

# ----------------- CACHE HELPERS -----------------
def cache_get(url: str):
    return cache.get(url)

def cache_set(url: str, file_path: str, kind: str, title: str):
    cache[url] = {"file": file_path, "type": kind, "title": title, "time": now_iso()}
    save_json(CACHE_FILE, cache)

# ----------------- DOWNLOAD HELPERS -----------------
def ytdl_extract_info(url, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info

# ----------------- ADMIN MANAGEMENT (admins.json) -----------------
def save_admins():
    save_json(ADMINS_FILE, admins)

def make_admin(target_user_id: int, level: int):
    admins[str(target_user_id)] = int(level)
    save_admins()
    add_history(target_user_id, "promoted", f"level={level}")

def unmake_admin(target_user_id: int):
    if str(target_user_id) in admins:
        del admins[str(target_user_id)]
        save_admins()
        add_history(target_user_id, "demoted", "")

# ----------------- HANDLERS: ADMIN COMMANDS (must be BEFORE text handler) -----------------
# /makeadmin @username level
@app.on_message(filters.command("makeadmin") & filters.private)
async def cmd_makeadmin(client, message):
    executor = message.from_user.id
    if admin_level(executor) < 2:  # require level 2 or higher to promote
        await message.reply("⛔ Ruxsat yo'q (faqat 2 yoki 3 daraja adminlar qo'sha oladi).")
        return
    try:
        parts = message.text.split()
        if len(parts) < 3:
            await message.reply("❗ Foydalanish: /makeadmin @username level\nMasalan: /makeadmin @user 2")
            return
        username = parts[1]
        level = int(parts[2])
        if level not in (1,2,3):
            await message.reply("❗ Daraja 1,2 yoki 3 bo'lishi kerak.")
            return
        user = await client.get_users(username)
        make_admin(user.id, level)
        await message.reply(f"✅ @{user.username} endi admin (level={level}).")
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /unmakeadmin @username
@app.on_message(filters.command("unmakeadmin") & filters.private)
async def cmd_unmakeadmin(client, message):
    executor = message.from_user.id
    if admin_level(executor) < 2:
        await message.reply("⛔ Ruxsat yo'q (faqat 2 yoki 3 daraja adminlar o'chira oladi).")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("❗ Foydalanish: /unmakeadmin @username")
            return
        username = parts[1]
        user = await client.get_users(username)
        if str(user.id) not in admins:
            await message.reply("ℹ️ Bu foydalanuvchi admin emas.")
            return
        unmake_admin(user.id)
        await message.reply(f"✅ @{user.username} adminlikdan olindi.")
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /warn @username reason
@app.on_message(filters.command("warn") & filters.private)
async def cmd_warn(client, message):
    if admin_level(message.from_user.id) < 1:
        await message.reply("⛔ Ruxsat yo'q.")
        return
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 2:
            await message.reply("❗ Foydalanish: /warn @username [sababi]")
            return
        username = parts[1]
        reason = parts[2] if len(parts) > 2 else "Sababsiz"
        user = await client.get_users(username)
        banned = warn_add(user.id, reason, source="manual")
        if banned:
            await message.reply(f"⚠️ @{user.username} 3 warnga yetdi va 1 kunga banlandi.")
        else:
            await message.reply(f"✅ @{user.username} ga warn berildi. Hozirgi warn: {user_warnings.get(user.id,0)}/3")
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /unwarn @username
@app.on_message(filters.command("unwarn") & filters.private)
async def cmd_unwarn(client, message):
    if admin_level(message.from_user.id) < 1:
        await message.reply("⛔ Ruxsat yo'q.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("❗ Foydalanish: /unwarn @username")
            return
        username = parts[1]
        user = await client.get_users(username)
        warn_clear(user.id)
        await message.reply(f"✅ @{user.username} ogohlantirishlari o‘chirildi.")
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /warns @username
@app.on_message(filters.command("warns") & filters.private)
async def cmd_warns(client, message):
    if admin_level(message.from_user.id) < 1:
        await message.reply("⛔ Ruxsat yo'q.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("❗ Foydalanish: /warns @username")
            return
        username = parts[1]
        user = await client.get_users(username)
        entries = warns_store.get(str(user.id), [])
        if not entries:
            await message.reply(f"ℹ️ @{user.username}da warn yo'q.")
            return
        text = f"⚠️ @{user.username} warnlari ({len(entries)}):\n"
        for e in entries:
            text += f"- {e['time']}: {e['reason']} (source={e.get('source','')})\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /ban @username 1d  OR /ban @username permanent
@app.on_message(filters.command("ban") & filters.private)
async def cmd_ban(client, message):
    if admin_level(message.from_user.id) < 2:
        await message.reply("⛔ Ruxsat yo'q (2+ daraja kerak).")
        return
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 3:
            await message.reply("❗ Foydalanish: /ban @username 1d yoki /ban @username permanent [sababi]")
            return
        username = parts[1]; duration = parts[2]; reason = parts[3] if len(parts)>3 else "Sababsiz"
        user = await client.get_users(username)
        if duration == "permanent":
            ban_user(user.id, 0, reason)
        else:
            # parse 1d or 5h
            if duration.endswith("d"):
                n = int(duration[:-1])
                ban_user(user.id, n, reason)
            elif duration.endswith("h"):
                # convert hours to days fraction not supported -> store iso until
                hours = int(duration[:-1])
                until = (datetime.now() + timedelta(hours=hours)).isoformat()
                banned_users[str(user.id)] = {"until": until, "reason": reason}
                save_json(BANNED_FILE, banned_users)
            else:
                await message.reply("❗ Vaqt formati xato. Misol: 1d yoki 5h yoki permanent")
                return
        await message.reply(f"✅ @{user.username} banlandi. Sabab: {reason}")
        add_history(user.id, "ban_manual", reason)
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /unban @username
@app.on_message(filters.command("unban") & filters.private)
async def cmd_unban(client, message):
    if admin_level(message.from_user.id) < 3:  # only level 3 can unban (as requested earlier)
        await message.reply("⛔ Ruxsat yo'q (3-daraja kerak).")
        return
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 2:
            await message.reply("❗ Foydalanish: /unban @username")
            return
        username = parts[1]; reason = parts[2] if len(parts)>2 else "Sababsiz"
        user = await client.get_users(username)
        if str(user.id) in banned_users:
            del banned_users[str(user.id)]
            save_json(BANNED_FILE, banned_users)
            add_history(user.id, "unban_manual", reason)
            await message.reply(f"✅ @{user.username} bandan chiqarildi.")
        else:
            await message.reply("ℹ️ Bu foydalanuvchi banlanmagan.")
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /stats (admins >=2)
@app.on_message(filters.command("stats") & filters.private)
async def cmd_stats(client, message):
    if admin_level(message.from_user.id) < 2:
        await message.reply("⛔ Ruxsat yo'q.")
        return
    total_banned = len(banned_users)
    total_warns = sum(len(v) for v in warns_store.values())
    total_admins = len(admins)
    downloads = stats.get("downloads", 0)
    audio = stats.get("audio", 0)
    video = stats.get("video", 0)
    text = (
        f"📊 Statistika:\n"
        f"- Yuklashlar: {downloads}\n"
        f"- Audio: {audio} | Video: {video}\n"
        f"- Banlanganlar: {total_banned}\n"
        f"- Jami warnlar: {total_warns}\n"
        f"- Adminlar: {total_admins}\n"
    )
    await message.reply(text)

# /history @username (admin>=2)
@app.on_message(filters.command("history") & filters.private)
async def cmd_history(client, message):
    if admin_level(message.from_user.id) < 2:
        await message.reply("⛔ Ruxsat yo'q.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("❗ Foydalanish: /history @username")
            return
        username = parts[1]
        user = await client.get_users(username)
        entries = history.get(str(user.id), [])
        if not entries:
            await message.reply("ℹ️ Tarix topilmadi.")
            return
        text = f"📜 @{user.username} tarixi:\n"
        for e in entries[-30:]:
            text += f"- {e['time']}: {e['event']} ({e.get('note','')})\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

# /sendall (broadcast) — ONLY level 3
@app.on_message(filters.command("sendall") & filters.private)
async def cmd_sendall(client, message):
    if admin_level(message.from_user.id) < 3:
        await message.reply("⛔ Ruxsat yo'q (faqat 3-daraja).")
        return
    try:
        text = message.text.split(maxsplit=1)
        if len(text) < 2:
            await message.reply("❗ Foydalanish: /sendall message_text")
            return
        body = text[1]
        await message.reply("📣 Broadcast boshlanmoqda...")

        # Foydalanuvchilarni users.json dan oling
        recipients = list(users.keys())
        success = 0
        failed = 0
        for uid in recipients:
            try:
                await client.send_message(int(uid), body)
                success += 1
            except:
                failed += 1
        await message.reply(f"✅ Yuborildi: {success}\n❌ Muvaffaqiyatsiz: {failed}")
    except Exception as e:
        await message.reply(f"⚠️ Xatolik: {e}")

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client, message):
    uid = str(message.from_user.id)

    # ban tekshiruvi
    if is_user_banned(message.from_user.id):
        await message.reply("🚫 Siz hozircha banlangansiz.")
        return

    # users.json ga qo'shish
    if uid not in users:
        users[uid] = {
            "username": message.from_user.username,
            "first_name": message.from_user.first_name,
            "last_name": message.from_user.last_name or "",
            "added": now_iso()
        }
        save_users(users)

    await message.reply(
        "🎬 Salom! YouTube/Instagram URL yuboring.\n"
        "🎥 Video/🎧 Audio yuklab beraman.\n"
        f"⚠️ 18+ yuborsangiz avtomatik warn olasiz. Anti-spam: {ANTI_SPAM_COUNT} so'rov / {ANTI_SPAM_WINDOW//60} min."
    )

# TEXT handler — **eng oxirida**
@app.on_message(filters.private & filters.text)
async def handler_text(client, message):
    user_id = message.from_user.id
    # admin commands already handled above because their handlers are registered earlier
    # anti-spam record
    punished = anti_spam_record(user_id)
    if punished:
        await message.reply("🚫 Siz cheksiz so'rov yubordingiz — 1 kunga banlandiz (anti-spam).")
        return
    if is_user_banned(user_id):
        await message.reply("🚫 Siz hozircha banlangansiz.")
        return
    text = message.text.strip()
    if not text.startswith("http"):
        await message.reply("❌ Iltimos, to'g'ri URL yuboring.")
        return
    url = text
    user_links[user_id] = url
    # update stats users map to know where to broadcast
    stats.setdefault("users", {})
    stats["users"].setdefault(str(user_id), 0)
    save_json(STATS_FILE, stats)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎥 Video", callback_data="video")],
        [InlineKeyboardButton("🎵 Musiqa", callback_data="audio")],
        [InlineKeyboardButton("🎶 To'liq musiqa", callback_data="full_song")]
    ])
    await message.reply("⬇️ Formatni tanlang:", reply_markup=keyboard)

# CALLBACK handler
@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    uid = callback_query.from_user.id
    if is_user_banned(uid):
        await callback_query.message.edit_text("🚫 Siz hozircha banlangansiz.")
        return
    url = user_links.get(uid)
    if not url:
        await callback_query.message.edit_text("⛔ Avval URL yuboring.")
        return
    action = callback_query.data
    # Check cache
    c = cache_get(url)
    if c and os.path.exists(c.get("file","")):
        # reuse
        if action == "audio" and c["type"] in ("audio","both"):
            await callback_query.message.edit_text("♻️ Oldingi audio topildi — yuborilmoqda...")
            await callback_query.message.reply_audio(c["file"], caption=c.get("title","Audio (cache)"))
            incr_stat(uid, "audio")
            add_history(uid, "download_cached", url)
            return
        if action == "video" and c["type"] in ("video","both"):
            await callback_query.message.edit_text("♻️ Oldingi video topildi — yuborilmoqda...")
            await callback_query.message.reply_video(c["file"], caption=c.get("title","Video (cache)"))
            incr_stat(uid, "video")
            add_history(uid, "download_cached", url)
            return
    # else perform download
    if action == "audio":
        await download_audio(callback_query, url)
    elif action == "video":
        # quality selection for youtube handled by subcallbacks q_*
        if "youtu" in url:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("360p", callback_data="q_360"),
                 InlineKeyboardButton("720p", callback_data="q_720")],
                [InlineKeyboardButton("1080p", callback_data="q_1080"),
                 InlineKeyboardButton("🔝 Eng yuqori", callback_data="q_best")]
            ])
            await callback_query.message.edit_text("📺 Sifatni tanlang:", reply_markup=keyboard)
        else:
            await download_video(callback_query, url, "best")
    elif action == "full_song":
        await search_full_song(callback_query, url)
    elif action.startswith("q_"):
        _, q = action.split("_",1)
        await download_video(callback_query, url, q)

# DOWNLOAD FUNCTIONS
async def download_audio(callback_query, url):
    uid = callback_query.from_user.id
    msg = await callback_query.message.edit_text("🎧 Yuklanmoqda... 0%")
    def hook(d): asyncio.create_task(progress_hook(d, msg))
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audio.%(ext)s',
        'quiet': True,
        'nocheckcertificate': True,
        'progress_hooks': [hook],
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}],
    }
    try:
        # extract info first to check title
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title","")
        if is_explicit_title(title):
            banned = warn_add(uid, "Explicit title detected (auto)", source="auto")
            if banned:
                await msg.edit_text("🚫 3 warnga yetib, 1 kunga banlandingiz.")
            else:
                await msg.edit_text(f"⚠️ Nomaqbul nom topildi. ({user_warnings.get(uid,0)}/3)")
            return
        # download
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
        if not os.path.exists(filename):
            await msg.edit_text("❌ Fayl topilmadi.")
            return
        await callback_query.message.reply_audio(audio=filename, caption=title)
        cache_set(url, filename, "audio", title)
        incr_stat(uid, "audio")
        add_history(uid, "download_audio", url)
    except Exception as e:
        await msg.edit_text(f"⚠️ Xatolik: {e}")

async def download_video(callback_query, url, quality):
    uid = callback_query.from_user.id
    msg = await callback_query.message.edit_text(f"🎬 Video ({quality}) yuklanmoqda... 0%")
    def hook(d): asyncio.create_task(progress_hook(d, msg))
    fmt = "bestvideo+bestaudio/best" if quality == "best" else f"bestvideo[height<={quality}]+bestaudio/best/best"
    ydl_opts = {
        'format': fmt,
        'outtmpl': 'video.%(ext)s',
        'merge_output_format':'mp4',
        'quiet': True,
        'nocheckcertificate': True,
        'progress_hooks': [hook]
    }
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title","")
        if is_explicit_title(title):
            banned = warn_add(uid, "Explicit title detected (auto)", source="auto")
            if banned:
                await msg.edit_text("🚫 3 warnga yetib, 1 kunga banlandingiz.")
            else:
                await msg.edit_text(f"⚠️ Nomaqbul nom topildi. ({user_warnings.get(uid,0)}/3)")
            return
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        if os.path.getsize(filename) > MAX_FILE_SIZE:
            await msg.edit_text("❗ Fayl juda katta (2GB dan katta)."); os.remove(filename); return
        await callback_query.message.reply_video(video=filename, caption=title)
        cache_set(url, filename, "video", title)
        incr_stat(uid, "video")
        add_history(uid, "download_video", url)
    except Exception as e:
        await msg.edit_text(f"⚠️ Xatolik: {e}")

# full song search
async def search_full_song(callback_query, url):
    msg = await callback_query.message.edit_text("🔎 To'liq versiya qidirilmoqda...")
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title","")
        query = re.sub(r'[^a-zA-Z0-9 ]','', title)
        search_url = f"https://www.youtube.com/results?search_query={query}+official+audio"
        resp = requests.get(search_url).text
        ids = re.findall(r"watch\?v=(\S{11})", resp)
        if not ids:
            await msg.edit_text("❌ To'liq musiqani topib bo'lmadi.")
            return
        full_url = f"https://www.youtube.com/watch?v={ids[0]}"
        await msg.edit_text("🎵 To'liq musiqani yuklanmoqda...")
        # reuse audio downloader by passing callback_query
        await download_audio(callback_query, full_url)
    except Exception as e:
        await msg.edit_text(f"⚠️ Xatolik: {e}")

USERS_FILE = "users.json"

def load_users():
    ensure_file(USERS_FILE, {})
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except:
            return {}

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

# global dict
users = load_users()


# ----------------- START BOT -----------------
if __name__ == "__main__":
    print("Bot ishga tushdi...")
    # ensure files saved
    save_all()
    app.run()