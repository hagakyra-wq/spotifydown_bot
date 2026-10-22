import os
import re
import json
import sqlite3
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta, time
import zoneinfo

from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TPE2, APIC
from openai import AsyncOpenAI

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# ИНИЦИАЛИЗАЦИЯ И КОНФИГУРАЦИЯ
# ==========================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

DB_FILE = "bot.db"
TEMP_DIR = Path("temp_music")
TEMP_DIR.mkdir(exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIPY_CLIENT_ID,
    client_secret=SPOTIPY_CLIENT_SECRET
))

groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

TIMEZONE_KZ = zoneinfo.ZoneInfo("Asia/Almaty")

# ==========================================
# РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ==========================================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        # Ключи доступа
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS access_keys (
                key_code TEXT PRIMARY KEY,
                max_uses INTEGER DEFAULT 1,
                uses_count INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TEXT
            )
        """)
        # Пользователи с доступом
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                activated_with_key TEXT,
                joined_at TEXT,
                is_banned INTEGER DEFAULT 0
            )
        """)
        # Кэш Telegram file_id
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS music_cache (
                spotify_id TEXT PRIMARY KEY,
                file_id TEXT,
                title TEXT,
                artist TEXT,
                created_at TEXT
            )
        """)
        # Очередь публикаций в канал
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT,
                caption TEXT,
                requested_by INTEGER,
                scheduled_date TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        conn.commit()

# ==========================================
# ВОССТАНОВЛЕНИЕ И БЭКАП БД С РЕНДЕРА
# ==========================================
async def backup_db_to_admin(reason="auto"):
    """Отправка БД в ЛС админа"""
    if not os.path.exists(DB_FILE):
        return
    try:
        doc = FSInputFile(DB_FILE)
        now_str = datetime.now(TIMEZONE_KZ).strftime("%Y-%m-%d %H:%M:%S")
        await bot.send_document(
            chat_id=ADMIN_ID,
            document=doc,
            caption=f"#db_backup [{reason}] Дата: {now_str}"
        )
        logger.info(f"Бэкап БД успешно отправлен админу ({reason}).")
    except Exception as e:
        logger.error(f"Ошибка при отправке бэкапа БД: {e}")

async def restore_db_from_telegram():
    """Реальное восстановление БД из последнего сообщения с документом в ЛС с админом"""
    if os.path.exists(DB_FILE) and os.path.getsize(DB_FILE) > 0:
        logger.info("Локальная база данных обнаружена.")
        return

    logger.info("База данных не найдена. Попытка восстановления из ЛС Админа...")
    init_db() 
    
    try:
        # Получаем обновления через getUpdates или скачиваем из истории чата с админом, 
        # если бот поддерживает хранение. Так как Bot API не имеет метода getHistory для ЛС,
        # используем резервный механизм: проверяем, отправлял ли админ файл или бот сохранял его.
        # На практике на Render при деплое рекомендуется использовать persistent disk, 
        # но здесь мы делаем программный перехват через поиск в чате (если бот видел сообщения).
        updates = await bot.get_updates(limit=100, timeout=1)
        backup_file_id = None
        for update in updates:
            if update.message and update.message.from_user.id == ADMIN_ID:
                if update.message.document and update.message.document.file_name == "bot.db":
                    backup_file_id = update.message.document.file_id
                elif update.message.caption and "#db_backup" in update.message.caption and update.message.document:
                    backup_file_id = update.message.document.file_id

        if backup_file_id:
            file_info = await bot.get_file(backup_file_id)
            await bot.download(file_info, destination=DB_FILE)
            logger.info("База данных успешно восстановлена из резервной копии Telegram!")
        else:
            logger.info("Резервная копия в последних сообщениях не найдена. Создана пустая БД.")
    except Exception as e:
        logger.error(f"Не удалось восстановить БД автоматически: {e}")

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ИИ GROQ
# ==========================================
def is_user_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    with get_db() as conn:
        res = conn.execute("SELECT is_banned FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
        if res and res["is_banned"] == 0:
            return True
    return False

def extract_spotify_id(url: str) -> str | None:
    match = re.search(r"open\.spotify\.com/track/([a-zA-Z0-9]+)", url)
    return match.group(1) if match else None

async def pick_best_candidate_via_groq(spotify_info: dict, candidates: list) -> int | None:
    prompt = f"""
    Дан оригинальный трек из Spotify:
    Исполнитель: {spotify_info['artists']}
    Название: {spotify_info['title']}
    Альбом: {spotify_info['album']}
    Длительность (сек): {spotify_info['duration_sec']}

    Вот список результатов с YouTube:
    {json.dumps(candidates, ensure_ascii=False, indent=2)}

    Задание:
    1. Отсеивай каверы (cover), ремиксы (remix), замедленные (slowed), ускоренные (sped up), live-версии и караоке, ЕСЛИ их нет в названии оригинала.
    2. Выбери наиболее подходящее точное аудио-совпадение.
    3. Верни JSON вида: {{"best_index": 0}} (где best_index — индекс от 0 до {len(candidates)-1}). Если ничего не подходит, верни {{"best_index": null}}.
    Отвечай ТОЛЬКО валидным JSON.
    """
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        content = response.choices[0].message.content.strip()
        data = json.loads(content)
        return data.get("best_index")
    except Exception as e:
        logger.error(f"Ошибка запроса к Groq: {e}")
        return None

# ==========================================
# СКАЧИВАНИЕ, КОНВЕРТАЦИЯ И ТЕГИ
# ==========================================
async def process_spotify_track(spotify_id: str, work_dir: Path) -> tuple[Path, dict]:
    track_data = sp.track(spotify_id)
    title = track_data["name"]
    artists = ", ".join([a["name"] for a in track_data["artists"]])
    album = track_data["album"]["name"]
    album_artist = track_data["album"]["artists"][0]["name"]
    year = track_data["album"]["release_date"].split("-")[0]
    track_num = track_data["track_number"]
    duration_sec = int(track_data["duration_ms"] / 1000)
    cover_url = track_data["album"]["images"][0]["url"] if track_data["album"]["images"] else None

    meta = {
        "spotify_id": spotify_id,
        "title": title,
        "artists": artists,
        "album": album,
        "album_artist": album_artist,
        "year": year,
        "track_number": track_num,
        "duration_sec": duration_sec,
        "cover_url": cover_url
    }

    search_query = f"{artists} - {title}"
    ydl_opts_search = {
        'extract_flat': 'in_playlist',
        'quiet': True,
        'skip_download': True
    }
    
    loop = asyncio.get_running_loop()
    candidates = []
    with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
        res = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch10:{search_query}", download=False))
        if res and 'entries' in res:
            for idx, entry in enumerate(res['entries']):
                candidates.append({
                    "index": idx,
                    "title": entry.get("title"),
                    "url": f"https://www.youtube.com/watch?v={entry.get('id')}",
                    "duration": entry.get("duration")
                })

    if not candidates:
        raise Exception("Кандидаты на YouTube не найдены.")

    best_idx = await pick_best_candidate_via_groq(meta, candidates)
    
    selected_url = None
    if best_idx is not None and 0 <= best_idx < len(candidates):
        selected_url = candidates[best_idx]["url"]
    else:
        # Безопасный резерв: ищем среди всех первый трек с подходящей длительностью (±4 сек)
        for c in candidates:
            if c["duration"] and abs(c["duration"] - duration_sec) <= 4:
                selected_url = c["url"]
                break
        # Если совсем ничего не подошло по длительности — берем первый результат как крайнюю мера
        if not selected_url:
            selected_url = candidates[0]["url"]

    out_filename = work_dir / f"{spotify_id}.mp3"
    ydl_opts_download = {
        'format': 'bestaudio/best',
        'outtmpl': str(work_dir / f"{spotify_id}.%(ext)s"),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
        await loop.run_in_executor(None, lambda: ydl.download([selected_url]))

    if out_filename.exists():
        audio = ID3(out_filename)
        audio.add(TIT2(encoding=3, text=title))
        audio.add(TPE1(encoding=3, text=artists))
        audio.add(TALB(encoding=3, text=album))
        audio.add(TPE2(encoding=3, text=album_artist))
        audio.add(TDRC(encoding=3, text=year))
        audio.add(TRCK(encoding=3, text=str(track_num)))

        if cover_url:
            import requests
            img_data = await loop.run_in_executor(None, lambda: requests.get(cover_url).content)
            audio.add(APIC(
                encoding=3,
                mime='image/jpeg',
                type=3,
                desc='Cover',
                data=img_data
            ))
        audio.save()

    return out_filename, meta

# ==========================================
# ОЧЕРЕДЬ АВТОПОСТИГА (06:00 UTC+5)
# ==========================================
def calculate_next_publish_time() -> str:
    now_kz = datetime.now(TIMEZONE_KZ)
    target_time = time(6, 0, 0)
    
    check_date = now_kz.date()
    if now_kz.time() >= target_time:
        check_date += timedelta(days=1)

    with get_db() as conn:
        while True:
            date_str = check_date.strftime("%Y-%m-%d")
            count = conn.execute("""
                SELECT COUNT(*) FROM scheduled_posts 
                WHERE DATE(scheduled_date) = ? AND status != 'cancelled'
            """, (date_str,)).fetchone()[0]

            if count < 3:
                scheduled_dt = datetime.combine(check_date, target_time, tzinfo=TIMEZONE_KZ)
                return scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
            check_date += timedelta(days=1)

async def check_and_publish_scheduled_posts():
    now_str = datetime.now(TIMEZONE_KZ).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        posts = conn.execute("""
            SELECT * FROM scheduled_posts 
            WHERE scheduled_date <= ? AND status = 'pending'
        """, (now_str,)).fetchall()

        for post in posts:
            try:
                await bot.send_audio(
                    chat_id=CHANNEL_ID,
                    audio=post["file_id"],
                    caption=post["caption"]
                )
                conn.execute("UPDATE scheduled_posts SET status = 'published' WHERE id = ?", (post["id"],))
                conn.commit()
                logger.info(f"Опубликован трек из очереди ID: {post['id']}")
            except Exception as e:
                logger.error(f"Ошибка автопостинга поста ID {post['id']}: {e}")

# ==========================================
# ХЕНДЛЕРЫ И ИНТЕРФЕЙС (Aiogram)
# ==========================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if is_user_allowed(user_id):
        await message.answer("👋 Привет! Отправь мне ссылку на трек из Spotify, и я скачаю его в максимальном качестве.")
    else:
        await message.answer("🔒 Бот закрыт. Введи ключ доступа (инвайт-код), чтобы начать работу:")

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Создать ключ", callback_data="adm_gen_key"),
         InlineKeyboardButton(text="📋 Список ключей", callback_data="adm_keys")],
        [InlineKeyboardButton(text="👥 Пользователи и баны", callback_data="adm_users")],
        [InlineKeyboardButton(text="📅 Очередь постов", callback_data="adm_queue")],
        [InlineKeyboardButton(text="💾 Скачать БД", callback_data="adm_download_db")]
    ])
    await message.answer("🛠 **Панель администратора**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_"))
async def handle_admin_callbacks(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    action = call.data

    if action == "adm_gen_key":
        import secrets
        new_key = f"MEGURA-{secrets.token_hex(3).upper()}"
        now_str = datetime.now(TIMEZONE_KZ).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO access_keys (key_code, max_uses, created_by, created_at) VALUES (?, 1, ?, ?)",
                (new_key, ADMIN_ID, now_str)
            )
            conn.commit()
        await call.message.answer(f"🔑 **Сгенерирован новый одноразовый ключ:**\n`{new_key}`", parse_mode="Markdown")
        await backup_db_to_admin("gen_key")

    elif action == "adm_keys":
        with get_db() as conn:
            keys = conn.execute("SELECT key_code, uses_count, max_uses FROM access_keys").fetchall()
        if not keys:
            await call.message.answer("🔑 Активных ключей нет.")
        else:
            rows = []
            for k in keys:
                kb_del = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Отозвать", callback_data=f"delkey_{k['key_code']}")]])
                rows.append(f"`{k['key_code']}` (использований: {k['uses_count']}/{k['max_uses']})")
            await call.message.answer("🔑 **Список ключей:**\n" + "\n".join(rows), parse_mode="Markdown")

    elif action == "adm_users":
        with get_db() as conn:
            users = conn.execute("SELECT user_id, username, is_banned FROM allowed_users").fetchall()
        if not users:
            await call.message.answer("👥 Нет зарегистрированных пользователей.")
            return
        for u in users:
            status = "🔴 Забанен" if u['is_banned'] else "🟢 Активен"
            btn_text = "Разбанить" if u['is_banned'] else "Забанить"
            btn_action = f"unban_{u['user_id']}" if u['is_banned'] else f"ban_{u['user_id']}"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, callback_data=btn_action)]])
            await call.message.answer(f"👤 @{u['username']} (`{u['user_id']}`) — {status}", reply_markup=kb, parse_mode="Markdown")

    elif action == "adm_queue":
        with get_db() as conn:
            posts = conn.execute("SELECT id, scheduled_date, status FROM scheduled_posts WHERE status='pending'").fetchall()
        if not posts:
            await call.message.answer("📅 Очередь автопостинга пуста.")
        else:
            text = "📅 **Запланированные посты:**\n" + "\n".join([f"#{p['id']} — {p['scheduled_date']}" for p in posts])
            await call.message.answer(text, parse_mode="Markdown")

    elif action == "adm_download_db":
        doc = FSInputFile(DB_FILE)
        await call.message.answer_document(doc, caption="💾 Актуальный файл базы данных.")
    
    await call.answer()

@dp.callback_query(F.data.startswith("delkey_"))
async def cb_delete_key(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key_code = call.data.split("_")[1]
    with get_db() as conn:
        conn.execute("DELETE FROM access_keys WHERE key_code = ?", (key_code,))
        conn.commit()
    await call.message.edit_text(f"🗑 Ключ `{key_code}` удален.", parse_mode="Markdown")
    await call.answer()

@dp.callback_query(F.data.startswith(("ban_", "unban_")))
async def cb_toggle_ban(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    action, user_id = call.data.split("_")
    user_id = int(user_id)
    new_status = 1 if action == "ban" else 0
    with get_db() as conn:
        conn.execute("UPDATE allowed_users SET is_banned = ? WHERE user_id = ?", (new_status, user_id))
        conn.commit()
    await call.message.edit_text(f"✅ Статус блокировки пользователя `{user_id}` изменен.", parse_mode="Markdown")
    await call.answer()

# Исправленная безопасная активация ключей (без сброса бана)
@dp.message(F.text.startswith("MEGURA-"))
async def process_key_activation(message: types.Message):
    user_id = message.from_user.id
    key_code = message.text.strip()

    with get_db() as conn:
        # Проверяем, не забанен ли пользователь уже
        existing_user = conn.execute("SELECT is_banned FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
        if existing_user and existing_user["is_banned"] == 1:
            await message.answer("❌ Ваш аккаунт заблокирован.")
            return

        key_data = conn.execute("SELECT * FROM access_keys WHERE key_code = ?", (key_code,)).fetchone()
        if key_data and key_data["uses_count"] < key_data["max_uses"]:
            now_str = datetime.now(TIMEZONE_KZ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE access_keys SET uses_count = uses_count + 1 WHERE key_code = ?", (key_code,))
            
            # Используем безопасный INSERT OR IGNORE для избежания сброса параметров существующей записи
            conn.execute("""
                INSERT INTO allowed_users (user_id, username, activated_with_key, joined_at, is_banned)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET 
                activated_with_key = excluded.activated_with_key
            """, (user_id, message.from_user.username or "NoUsername", key_code, now_str))
            conn.commit()
            
            await message.answer("✅ **Доступ успешно получен!** Теперь ты можешь присылать ссылки на Spotify.")
            await backup_db_to_admin("key_activated")
        else:
            await message.answer("❌ Недействительный или уже использованный ключ доступа.")

# Обработка ссылок Spotify
@dp.message(F.text.contains("open.spotify.com/track/"))
async def handle_spotify_link(message: types.Message):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        await message.answer("🔒 Введи ключ доступа для работы с ботом.")
        return

    url = message.text.strip()
    spotify_id = extract_spotify_id(url)
    if not spotify_id:
        await message.answer("❌ Неверный формат ссылки на трек Spotify.")
        return

    status_msg = await message.answer("🔎 Ищу и скачиваю трек...")

    with get_db() as conn:
        cached = conn.execute("SELECT file_id, title, artist FROM music_cache WHERE spotify_id = ?", (spotify_id,)).fetchone()

    if cached:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Закинуть в ТГ", callback_data=f"pub_{spotify_id}"),
             InlineKeyboardButton(text="⚡ Опубликовать сейчас", callback_data=f"now_{spotify_id}")],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_track")]
        ])
        await status_msg.delete()
        await message.answer_audio(
            audio=cached["file_id"],
            caption=f"🎵 {cached['artist']} — {cached['title']}\n*(из кэша)*",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return

    work_dir = TEMP_DIR / f"{user_id}_{int(datetime.now().timestamp())}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        file_path, meta = await process_spotify_track(spotify_id, work_dir)
        audio_file = FSInputFile(file_path)

        sent_audio = await message.answer_audio(
            audio=audio_file,
            title=meta["title"],
            performer=meta["artists"],
            duration=meta["duration_sec"],
            caption=f"🎵 **{meta['artists']} — {meta['title']}**\n💽 Альбом: {meta['album']}\n👤 Запросил: @{message.from_user.username or 'User'}"
        )

        file_id = sent_audio.audio.file_id
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO music_cache (spotify_id, file_id, title, artist, created_at) VALUES (?, ?, ?, ?, ?)",
                (spotify_id, file_id, meta["title"], meta["artists"], datetime.now(TIMEZONE_KZ).strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Закинуть в ТГ", callback_data=f"pub_{spotify_id}"),
             InlineKeyboardButton(text="⚡ Опубликовать сейчас", callback_data=f"now_{spotify_id}")],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_track")]
        ])
        await message.answer("Выбери действие для канала:", reply_markup=kb)
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка обработки трека: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка при загрузке: {e}")
    finally:
        for item in work_dir.glob("*"):
            item.unlink()
        work_dir.rmdir()

# Защищенный колбэк публикации в очередь с проверкой прав
@dp.callback_query(F.data.startswith("pub_"))
async def cb_publish_track(call: types.CallbackQuery):
    if not is_user_allowed(call.from_user.id):
        await call.answer("⛔ Доступ запрещен.", show_alert=True)
        return

    spotify_id = call.data.split("_")[1]
    with get_db() as conn:
        cached = conn.execute("SELECT file_id, title, artist FROM music_cache WHERE spotify_id = ?", (spotify_id,)).fetchone()
        
        if not cached:
            await call.answer("Ошибка: трек не найден в кэше.", show_alert=True)
            return

        scheduled_time = calculate_next_publish_time()
        caption = f"🎵 **{cached['artist']} — {cached['title']}**\n\n#music"

        conn.execute("""
            INSERT INTO scheduled_posts (file_id, caption, requested_by, scheduled_date)
            VALUES (?, ?, ?, ?)
        """, (cached["file_id"], caption, call.from_user.id, scheduled_time))
        conn.commit()

    await call.message.edit_text(f"📅 **Песня добавлена в очередь!**\nПубликация запланирована на: `{scheduled_time}` (UTC+5)", parse_mode="Markdown")
    await backup_db_to_admin("add_to_queue")
    await call.answer()

# Мгновенная публикация трека в канал («сейчас»)
@dp.callback_query(F.data.startswith("now_"))
async def cb_publish_now(call: types.CallbackQuery):
    if not is_user_allowed(call.from_user.id):
        await call.answer("⛔ Доступ запрещен.", show_alert=True)
        return

    spotify_id = call.data.split("_")[1]
    with get_db() as conn:
        cached = conn.execute("SELECT file_id, title, artist FROM music_cache WHERE spotify_id = ?", (spotify_id,)).fetchone()
        if not cached:
            await call.answer("Ошибка: трек не найден.", show_alert=True)
            return

    try:
        await bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=cached["file_id"],
            caption=f"🎵 **{cached['artist']} — {cached['title']}**\n\n#music"
        )
        await call.message.edit_text("⚡ **Трек успешно опубликован в канал прямо сейчас!**", parse_mode="Markdown")
    except Exception as e:
        await call.answer(f"Ошибка публикации: {e}", show_alert=True)
    await call.answer()

@dp.callback_query(F.data == "skip_track")
async def cb_skip_track(call: types.CallbackQuery):
    await call.message.edit_text("⏭ Трек пропущен (оставлен только для личного прослушивания).")
    await call.answer()

# ==========================================
# ТОЧКА ВХОДА И ЗАПУСК
# ==========================================
async def main():
    await restore_db_from_telegram()
    
    scheduler.add_job(check_and_publish_scheduled_posts, 'interval', minutes=1)
    scheduler.add_job(backup_db_to_admin, 'interval', hours=6, kwargs={"reason": "cron_6h"})
    scheduler.start()

    logger.info("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())