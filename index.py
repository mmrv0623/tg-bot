from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import yt_dlp
import os
import config
import asyncio
import re
import requests

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
user_links = {}

app = Client(
    "yt_downloader_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

progress_messages = {}

# 🔄 Yuklanish jarayoni
async def progress_hook(d, message):
    if d['status'] == 'downloading':
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
            if message.id not in progress_messages or progress_messages[message.id] != percent:
                progress_messages[message.id] = percent
                await message.edit_text(text)
        except:
            pass


@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply(
        "🎬 Salom! Men YouTube videolarini yuklab beradigan botman.\n\n"
        "🔗 Videoni yuboring — men uni video yoki musiqa sifatida yuklab beraman.\n"
        "🎶 Endi esa *video musiqasining to‘liq versiyasini* ham topib bera olaman!"
    )


@app.on_message(filters.private & filters.text)
async def ask_choice(client, message):
    url = message.text.strip()

    if "youtube.com" not in url and "youtu.be" not in url:
        await message.reply("❌ Bu YouTube linki emas. To‘g‘ri link yuboring.")
        return

    if "youtu.be" in url:
        video_id = url.split("/")[-1]
        url = f"https://www.youtube.com/watch?v={video_id}"

    user_links[message.from_user.id] = url

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎥 Video", callback_data="video")],
        [InlineKeyboardButton("🎵 Musiqa", callback_data="audio")],
        [InlineKeyboardButton("🎶 Video musiqasi (to‘liq)", callback_data="full_song")]
    ])

    await message.reply("⬇️ Qaysi formatni yuklamoqchisiz?", reply_markup=keyboard)


@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    action = callback_query.data
    url = user_links.get(user_id)

    if not url:
        await callback_query.message.edit_text("⛔ Avval YouTube link yuboring.")
        return

    if action == "audio":
        await download_audio(callback_query, url)

    elif action == "video":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p", callback_data="q_360"),
                InlineKeyboardButton("720p", callback_data="q_720"),
            ],
            [
                InlineKeyboardButton("1080p", callback_data="q_1080"),
                InlineKeyboardButton("🔝 Eng yuqori", callback_data="q_best"),
            ]
        ])
        await callback_query.message.edit_text("📺 Qaysi sifatda yuklamoqchisiz?", reply_markup=keyboard)

    elif action == "full_song":
        await search_full_song(callback_query, url)

    elif action.startswith("q_"):
        quality = action.split("_")[1]
        await download_video(callback_query, url, quality)


# 🎵 Audio yuklash
async def download_audio(callback_query, url):
    msg = await callback_query.message.edit_text("🎧 Musiqa yuklanmoqda... 0%")

    def hook(d):
        asyncio.create_task(progress_hook(d, msg))

    try:
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio/best',
            'outtmpl': 'audio.%(ext)s',
            'quiet': True,
            'nocheckcertificate': True,
            'progress_hooks': [hook],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            filename = base + ".mp3"

        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            await msg.edit_text("❌ Xatolik: Yuklab olingan fayl bo‘sh chiqdi. Iltimos, boshqa video yuboring.")
            return

        await msg.edit_text("✅ Yuklandi! Fayl yuborilmoqda...")
        await callback_query.message.reply_audio(audio=filename, caption=info.get("title", "Audio"))
        os.remove(filename)

    except Exception as e:
        await msg.edit_text(f"⚠️ Xatolik: {e}")


# 🎬 Video yuklash
async def download_video(callback_query, url, quality):
    msg = await callback_query.message.edit_text(f"🎬 Video ({quality}) yuklanmoqda... 0%")

    def hook(d):
        asyncio.create_task(progress_hook(d, msg))

    try:
        if quality == "best":
            fmt = "bestvideo+bestaudio/best"
        else:
            fmt = f"bestvideo[height<={quality}]+bestaudio/best/best"

        ydl_opts = {
            'format': fmt,
            'outtmpl': 'video.%(ext)s',
            'merge_output_format': 'mp4',
            'quiet': True,
            'nocheckcertificate': True,
            'progress_hooks': [hook],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            title = info.get('title', 'Video')

        if os.path.getsize(filename) > MAX_FILE_SIZE:
            await msg.edit_text("❗ Video juda katta, Telegramga yuborolmayman.")
            os.remove(filename)
            return

        await msg.edit_text("✅ Yuklandi! Fayl yuborilmoqda...")
        await callback_query.message.reply_video(video=filename, caption=title)
        os.remove(filename)
    except Exception as e:
        await msg.edit_text(f"⚠️ Xatolik: {e}")


# 🎶 Video musiqasining to‘liq versiyasini topish
async def search_full_song(callback_query, url):
    msg = await callback_query.message.edit_text("🔎 Videodagi musiqa aniqlanmoqda...")

    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "")
            artist = info.get("artist") or ""
            track = info.get("track") or ""

        search_query = f"{artist} {track}" if track else title
        search_query = re.sub(r'[^a-zA-Z0-9а-яА-Я ]', '', search_query)

        await msg.edit_text(f"🎶 To‘liq versiya qidirilmoqda: {search_query}")

        search_url = f"https://www.youtube.com/results?search_query={search_query}+official+audio"
        response = requests.get(search_url).text
        video_ids = re.findall(r"watch\?v=(\S{11})", response)
        if not video_ids:
            await msg.edit_text("❌ To‘liq musiqani topib bo‘lmadi.")
            return

        full_song_url = f"https://www.youtube.com/watch?v={video_ids[0]}"

        await msg.edit_text("🎵 To‘liq musiqani yuklanmoqda...")

        def hook(d):
            asyncio.create_task(progress_hook(d, msg))

        audio_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio/best',
            'outtmpl': 'full_song.%(ext)s',
            'quiet': True,
            'nocheckcertificate': True,
            'progress_hooks': [hook],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }

        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            info = ydl.extract_info(full_song_url, download=True)
            audio_file = ydl.prepare_filename(info)
            base, _ = os.path.splitext(audio_file)
            audio_file = base + ".mp3"

        await msg.edit_text("✅ To‘liq versiya tayyor! Fayl yuborilmoqda...")
        await callback_query.message.reply_audio(audio_file, caption=info.get("title", "To‘liq musiqasi"))
        os.remove(audio_file)
    except Exception as e:
        await msg.edit_text(f"⚠️ Xatolik yuz berdi: {e}")


app.run()