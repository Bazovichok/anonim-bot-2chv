# bot.py
import os
import json
import html
import base64
import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, Any, List

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, Update
from aiogram.exceptions import TelegramForbiddenError

# Optional: firebase-admin (if FIREBASE_CREDENTIALS_JSON provided)
USE_FIREBASE = False
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    USE_FIREBASE = True
except Exception:
    USE_FIREBASE = False  # will check env at runtime

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN environment variable")

# Webhook path (we'll use /webhook)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")  # keep leading slash
# Public URL where render will serve the app, e.g. https://your-service.onrender.com
# You can set WEBHOOK_URL manually, or after deploy run curl to set it.
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # optional, can set later via curl

# Persistence files (local fallback)
USERS_FILE = os.getenv("USERS_FILE", "users.json")
BANNED_FILE = os.getenv("BANNED_FILE", "banned_users.txt")

# Limits & timings
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "250"))
MAX_MEDIA_MB = int(os.getenv("MAX_MEDIA_MB", "20"))
SPAM_INTERVAL = timedelta(minutes=int(os.getenv("SPAM_INTERVAL_MINUTES", "10")))
SEND_INTERVAL = timedelta(seconds=int(os.getenv("SEND_INTERVAL_SECONDS", "3")))

# Admins (comma-separated IDs if you want)
ADMINS = set()
if os.getenv("ADMINS"):
    try:
        ADMINS = set(int(x.strip()) for x in os.getenv("ADMINS").split(",") if x.strip())
    except Exception:
        ADMINS = set()

# ========== FIRESTORE INIT (if provided) ==========
def init_firebase_if_env():
    # Check env vars
    raw = os.getenv("FIREBASE_CREDENTIALS_JSON")
    raw_b64 = os.getenv("FIREBASE_CREDENTIALS_BASE64")
    if not raw and not raw_b64:
        return False
    if not USE_FIREBASE:
        raise RuntimeError("firebase-admin not installed but FIREBASE_CREDENTIALS_JSON provided. Add firebase-admin to requirements.")
    try:
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                # maybe base64 in this var
                data = json.loads(base64.b64decode(raw).decode("utf-8"))
        else:
            data = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred)
        return True
    except Exception as e:
        raise RuntimeError("Failed to initialize Firebase: " + str(e))

FIRESTORE_ENABLED = False
try:
    FIRESTORE_ENABLED = init_firebase_if_env()
except Exception as e:
    print("Firebase init error:", e)
    FIRESTORE_ENABLED = False

if FIRESTORE_ENABLED:
    db = firestore.client()
    USERS_COL = db.collection("anon_bot_users")
else:
    USERS_COL = None

# ========== Bot & Dispatcher ==========
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ========== Helpers: local file persistence ==========
def load_users_local() -> Dict[int, str]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except Exception as e:
        print("Failed to load users.json:", e)
        return {}

def save_users_local(users: Dict[int, str]):
    tmp = USERS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in users.items()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USERS_FILE)
    except Exception as e:
        print("Failed to save users.json:", e)

def load_banned_local() -> set:
    if not os.path.exists(BANNED_FILE):
        return set()
    try:
        with open(BANNED_FILE, "r", encoding="utf-8") as f:
            return set(int(line.strip()) for line in f if line.strip().isdigit())
    except Exception as e:
        print("Failed to load banned file:", e)
        return set()

def append_banned_local(uid: int):
    try:
        with open(BANNED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{uid}\n")
    except Exception as e:
        print("Failed to append banned:", e)

def rewrite_banned_local(banned: set):
    try:
        with open(BANNED_FILE, "w", encoding="utf-8") as f:
            for uid in banned:
                f.write(f"{uid}\n")
    except Exception as e:
        print("Failed to write banned file:", e)

# ========== Firestore helpers (blocking calls run in executor) ==========
import functools, concurrent.futures
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

async def run_blocking(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))

def _get_user_doc_sync(uid: int):
    doc = USERS_COL.document(str(uid)).get()
    return doc.to_dict() if doc.exists else None

async def get_user_doc(uid: int):
    return await run_blocking(_get_user_doc_sync, uid)

def _set_user_doc_sync(uid:int, data: Dict[str,Any]):
    USERS_COL.document(str(uid)).set(data)

async def set_user_doc(uid:int, data: Dict[str,Any]):
    return await run_blocking(_set_user_doc_sync, uid, data)

def _update_user_doc_sync(uid:int, updates: Dict[str,Any]):
    USERS_COL.document(str(uid)).update(updates)

async def update_user_doc(uid:int, updates: Dict[str,Any]):
    return await run_blocking(_update_user_doc_sync, uid, updates)

def _list_user_docs_sync():
    docs = USERS_COL.stream()
    out = []
    for d in docs:
        dd = d.to_dict()
        dd["_doc_id"] = d.id
        out.append(dd)
    return out

async def list_user_docs():
    return await run_blocking(_list_user_docs_sync)

# ========== runtime state ==========
if FIRESTORE_ENABLED:
    print("Using Firestore for persistence")
    # no local load
    local_users = {}
    banned_users = set()
else:
    print("Using local files for persistence (users.json). NOTE: file not persistent across redeploys!")
    local_users = load_users_local()
    banned_users = load_banned_local()

# in-memory last-message metadata (not persisted)
user_last_message: Dict[int, Any] = {}

# ========== utility functions ==========
def generate_anon_id() -> str:
    return "ID" + str(random.randint(1000000000, 9999999999))

def sanitize_text(text: str) -> str:
    if not text:
        return ""
    return html.escape(text.strip())

async def ensure_user(uid: int) -> Dict[str,Any]:
    if FIRESTORE_ENABLED:
        doc = await get_user_doc(uid)
        if doc:
            return doc
        anon = generate_anon_id()
        data = {"anon_id": anon, "created_at": datetime.utcnow().timestamp(), "banned": False, "last_send": 0.0, "last_message": ""}
        await set_user_doc(uid, data)
        return data
    else:
        if uid in local_users:
            return {"anon_id": local_users[uid], "banned": False, "last_send": 0.0, "last_message": ""}
        anon = generate_anon_id()
        local_users[uid] = anon
        save_users_local(local_users)
        return {"anon_id": anon, "banned": False, "last_send": 0.0, "last_message": ""}

async def mark_banned(uid:int):
    if FIRESTORE_ENABLED:
        try:
            await update_user_doc(uid, {"banned": True})
        except Exception as e:
            print("Failed to mark banned in firestore:", e)
    else:
        banned_users.add(uid)
        append_banned_local(uid)

async def mark_unbanned(uid:int):
    if FIRESTORE_ENABLED:
        try:
            await update_user_doc(uid, {"banned": False})
        except Exception as e:
            print("Failed to unban in firestore:", e)
    else:
        banned_users.discard(uid)
        rewrite_banned_local(banned_users)

async def get_all_recipients() -> List[int]:
    if FIRESTORE_ENABLED:
        docs = await list_user_docs()
        out = []
        for d in docs:
            try:
                if not d.get("banned", False):
                    out.append(int(d["_doc_id"]))
            except Exception:
                pass
        return out
    else:
        return [uid for uid in local_users.keys() if uid not in banned_users]

async def is_banned(uid:int) -> bool:
    if FIRESTORE_ENABLED:
        doc = await get_user_doc(uid)
        return bool(doc.get("banned", False)) if doc else False
    else:
        return uid in banned_users

# ========== Handlers ==========
@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    if await is_banned(uid):
        await message.answer("🚫 Вы заблокированы и не можете участвовать.")
        return
    await ensure_user(uid)
    await message.answer("👋 Добро пожаловать в анонимный чатик. Отправь сообщение, чтобы его увидели другие участники.")

@dp.message()
async def all_msg_handler(message: Message):
    # only private
    if message.chat.type != "private":
        return

    uid = message.from_user.id
    if await is_banned(uid):
        await message.answer("🚫 Вы заблокированы и не можете отправлять сообщения.")
        return

    user_doc = await ensure_user(uid)
    anon_id = user_doc.get("anon_id") if user_doc else None

    # type and text
    if message.text:
        text = sanitize_text(message.text)
        kind = "text"
    elif message.caption:
        text = sanitize_text(message.caption)
        kind = "caption"
    elif message.photo:
        text = "<photo>"
        kind = "photo"
    elif message.video:
        text = "<video>"
        kind = "video"
    elif message.document:
        text = "<document>"
        kind = "document"
    elif message.sticker:
        text = "<sticker>"
        kind = "sticker"
    elif message.animation:
        text = "<animation>"
        kind = "animation"
    elif message.voice:
        text = "<voice>"
        kind = "voice"
    elif message.audio:
        text = "<audio>"
        kind = "audio"
    else:
        return

    if kind in ("text","caption") and len(text) > MAX_MESSAGE_LENGTH:
        await message.reply(f"⚠️ Сообщение слишком длинное (макс {MAX_MESSAGE_LENGTH}).")
        return

    # spam: identical within SPAM_INTERVAL
    last = user_last_message.get(uid)
    now_ts = datetime.utcnow().timestamp()
    if last:
        last_text, last_time, last_send_time = last
        if kind in ("text","caption") and text == last_text and datetime.utcnow() - last_time < SPAM_INTERVAL:
            await message.reply("⚠️ Нельзя.")
            return
        if last_send_time and datetime.utcnow() - last_send_time < SEND_INTERVAL:
            await message.reply(f"⚠️ Подожди {int(SEND_INTERVAL.total_seconds())} сек перед следующим сообщением.")
            return

    # media size checks
    if message.photo and getattr(message.photo[-1], "file_size", 0) and message.photo[-1].file_size > MAX_MEDIA_MB * 1024 * 1024:
        await message.reply(f"⚠️ Фото слишком большое (макс {MAX_MEDIA_MB} МБ)."); return
    if message.document and getattr(message.document, "file_size", 0) and message.document.file_size > MAX_MEDIA_MB * 1024 * 1024:
        await message.reply(f"⚠️ Файл слишком большой (макс {MAX_MEDIA_MB} МБ)."); return
    if message.video and getattr(message.video, "file_size", 0) and message.video.file_size > MAX_MEDIA_MB * 1024 * 1024:
        await message.reply(f"⚠️ Видео слишком большое (макс {MAX_MEDIA_MB} МБ)."); return

    # update last message meta
    user_last_message[uid] = (text if kind in ("text","caption") else kind, datetime.utcnow(), datetime.utcnow())

    # console output
    print(f"[TelegramID: {uid} | ChatID: {anon_id}] -> {text if kind in ('text','caption') else kind}")

    # prepare caption
    caption = f"<code>[{anon_id}]</code>\n"
    if kind in ("text","caption"):
        caption += text

    # get recipients
    recipients = await get_all_recipients()
    for rid in recipients:
        if rid == uid:
            continue
        try:
            if kind == "text":
                await bot.send_message(chat_id=rid, text=caption)
            elif kind == "photo":
                await bot.send_photo(chat_id=rid, photo=message.photo[-1].file_id, caption=caption)
            elif kind == "video":
                await bot.send_video(chat_id=rid, video=message.video.file_id, caption=caption)
            elif kind == "document":
                await bot.send_document(chat_id=rid, document=message.document.file_id, caption=caption)
            elif kind == "sticker":
                await bot.send_sticker(chat_id=rid, sticker=message.sticker.file_id)
            elif kind == "animation":
                await bot.send_animation(chat_id=rid, animation=message.animation.file_id, caption=caption)
            elif kind == "voice":
                await bot.send_voice(chat_id=rid, voice=message.voice.file_id, caption=caption)
            elif kind == "audio":
                await bot.send_audio(chat_id=rid, audio=message.audio.file_id, caption=caption)
        except TelegramForbiddenError:
            print(f"Bot blocked by {rid} — ignoring.")
        except Exception as e:
            print(f"Error sending to {rid}: {e}")

# --- admin commands ---
@dp.message(Command(commands=["ban"]))
async def cmd_ban(message: Message):
    if message.from_user.id not in ADMINS:
        return
    if not message.reply_to_message:
        await message.reply("Ответьте командой на сообщение пользователя.")
        return
    target_id = message.reply_to_message.from_user.id
    await mark_banned(target_id)
    await message.reply(f"Пользователь {target_id} заблокирован (и будет игнорироваться).")

@dp.message(Command(commands=["unban"]))
async def cmd_unban(message: Message):
    if message.from_user.id not in ADMINS:
        return
    if not message.reply_to_message:
        await message.reply("Ответьте командой на сообщение пользователя.")
        return
    target_id = message.reply_to_message.from_user.id
    await mark_unbanned(target_id)
    await message.reply(f"Пользователь {target_id} разбанен.")

# ========== AIOHTTP APP to receive webhook updates ==========
async def handle_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")
    try:
        update = Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        print("Failed to process update:", e)
    return web.Response(status=200, text="ok")

async def health(request):
    return web.Response(text="ok")

async def on_startup(app):
    # Optionally set webhook from env
    url = WEBHOOK_URL
    if not url:
        # instruct user to set webhook manually after deploy
        print("WEBHOOK_URL not set. Please run setWebhook manually after deploy.")
    else:
        try:
            await bot.set_webhook(url + WEBHOOK_PATH)
            print("Webhook set to", url + WEBHOOK_PATH)
        except Exception as e:
            print("Failed to set webhook on startup:", e)

async def on_shutdown(app):
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    await bot.session.close()

def create_app():
    app = web.Application()
    app.add_routes([web.post(WEBHOOK_PATH, handle_webhook), web.get("/", health)])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)
    return app

# ========== RUN ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # when firestore disabled: local_users already loaded
    app = create_app()
    print("Starting aiohttp on port", port)
    web.run_app(app, host="0.0.0.0", port=port)
