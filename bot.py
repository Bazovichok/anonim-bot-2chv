import asyncio
import random
import html
import json
import os
from datetime import datetime, timedelta
from typing import Dict
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.exceptions import TelegramForbiddenError

# === Бот ТОКЕН ===
BOT_TOKEN = "8429191232:AAFxAJUgKNHMP_YdPfHOaQykux0GwBiUwE4"

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ---------- Persistence files ----------
USERS_FILE = "users.json"        # здесь будут храниться mapping user_id -> anon_id
BANNED_FILE = "banned_users.txt" # уже используемый файл для забаненных

# ---------- В памяти (будут загружены из файлов) ----------
user_data: Dict[int, str] = {}  # user_id -> anon_id
user_last_message = {}          # user_id -> (last_text, last_time, last_send_time)
banned_users = set()

# Администраторы (Telegram ID)
ADMINS = {272883423}

# Параметры
MAX_MESSAGE_LENGTH = 250
MAX_MEDIA_SIZE_MB = 20
SPAM_INTERVAL = timedelta(minutes=10)
SEND_INTERVAL = timedelta(seconds=3)  # ограничение на скорость сообщений

# ----------------- Helpers: load/save users -----------------
def load_users() -> Dict[int, str]:
    """Загружает users.json, возвращает dict user_id->anon_id (ключи int)."""
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # приводим ключи к int (в файле хранятся строки)
        return {int(k): v for k, v in data.items()}
    except Exception as e:
        print("Ошибка загрузки users.json:", e)
        return {}

def save_users(users: Dict[int, str]):
    """Атомарно сохраняет users в USERS_FILE."""
    tmp = USERS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # ключи в JSON должны быть строками
            json.dump({str(k): v for k, v in users.items()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USERS_FILE)
    except Exception as e:
        print("Ошибка записи users.json:", e)

# ----------------- Загружаем бан-лист и users при старте -----------------
try:
    with open(BANNED_FILE, "r", encoding="utf-8") as f:
        banned_users = set(int(line.strip()) for line in f if line.strip().isdigit())
except FileNotFoundError:
    banned_users = set()
except Exception as e:
    print("Ошибка загрузки banned_users.txt:", e)
    banned_users = set()

# загружаем подписчиков (user_data)
user_data = load_users()

# ----------------- Основные функции бота -----------------
def get_anon_id(user_id: int) -> str:
    """
    Возвращает существующий anon_id или создаёт новый и сразу сохраняет users.json.
    """
    if user_id in user_data:
        return user_data[user_id]
    anon_id = "ID" + str(random.randint(1000000000, 9999999999))
    user_data[user_id] = anon_id
    # сохраняем сразу, чтобы при перезапуске новый пользователь остался в списке
    try:
        save_users(user_data)
    except Exception as e:
        print("Ошибка при сохранении users.json в get_anon_id:", e)
    return anon_id

def is_spam(user_id: int, text: str) -> bool:
    now = datetime.now()
    last = user_last_message.get(user_id)
    if last:
        last_text, last_time, _ = last
        if text == last_text and now - last_time < SPAM_INTERVAL:
            return True
    return False

def can_send(user_id: int) -> bool:
    now = datetime.now()
    last = user_last_message.get(user_id)
    if last:
        _, _, last_send_time = last
        if last_send_time and now - last_send_time < SEND_INTERVAL:
            return False
    return True

def sanitize_text(text: str) -> str:
    if not text:
        return ""
    return html.escape(text.strip())

def ban_user(user_id: int):
    banned_users.add(user_id)
    try:
        with open(BANNED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{user_id}\n")
    except Exception as e:
        print("Ошибка записи в banned_users.txt:", e)
    print(f"⚠️ Пользователь {user_id} заблокирован!")

def unban_user(user_id: int):
    banned_users.discard(user_id)
    try:
        with open(BANNED_FILE, "w", encoding="utf-8") as f:
            for uid in banned_users:
                f.write(f"{uid}\n")
    except Exception as e:
        print("Ошибка записи в banned_users.txt:", e)
    print(f"✅ Пользователь {user_id} разбанен!")

# ----------------- Handlers -----------------
@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id
    if user_id in banned_users:
        await message.answer("🚫 Вы заблокированы и не можете участвовать в чате.")
        return
    anon_id = get_anon_id(user_id)
    await message.answer(
        f"👋 Привет! Теперь ты в анонимном чате.\n"
        f"Твой новый случайный ID:\n<code>[{anon_id}]</code>\n\n"
        f"Напиши сообщение сюда — и его увидят все участники."
    )

@dp.message()
async def msg_handler(message: Message):
    # игнорируем group / channel сообщения
    if message.chat.type != "private":
        return

    user_id = message.from_user.id
    if user_id in banned_users:
        await message.answer("🚫 Вы заблокированы и не можете отправлять сообщения.")
        return

    anon_id = user_data.get(user_id)
    if not anon_id:
        anon_id = get_anon_id(user_id)

    # Определяем тип сообщения
    if message.text:
        text = sanitize_text(message.text)
        display_text = text
    elif message.caption:
        text = sanitize_text(message.caption)
        display_text = f"<caption>{text}</caption>"
    elif message.photo:
        text = "<photo>"
        display_text = text
    elif message.video:
        text = "<video>"
        display_text = text
    elif message.document:
        text = "<document>"
        display_text = text
    elif message.sticker:
        text = "<sticker>"
        display_text = text
    elif message.animation:
        text = "<animation>"
        display_text = text
    elif message.audio:
        text = "<audio>"
        display_text = text
    elif message.voice:
        text = "<voice>"
        display_text = text
    else:
        return

    # Проверка длины
    if len(display_text) > MAX_MESSAGE_LENGTH:
        await message.reply(f"⚠️ Сообщение слишком длинное. Максимум {MAX_MESSAGE_LENGTH} символов.")
        return

    # Спам и скорость
    if is_spam(user_id, display_text):
        await message.reply(f"⚠️ Нельзя отправлять одинаковые сообщения чаще, чем раз в 10 минут.")
        return
    if not can_send(user_id):
        await message.reply(f"⚠️ Подожди 3 секунды перед отправкой следующего сообщения.")
        return

    # Проверка размера медиа
    media_checks = [
        (message.document, "Файл"),
        (message.video, "Видео"),
        (message.audio, "Аудио"),
        (message.voice, "Голосовое сообщение"),
        (message.animation, "GIF"),
    ]
    for media, name in media_checks:
        if media and media.file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
            await message.reply(f"⚠️ {name} слишком большое. Максимум {MAX_MEDIA_SIZE_MB} МБ.")
            return
    if message.photo and message.photo[-1].file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
        await message.reply(f"⚠️ Фото слишком большое. Максимум {MAX_MEDIA_SIZE_MB} МБ.")
        return

    # Сохраняем последнее сообщение и время отправки
    user_last_message[user_id] = (display_text, datetime.now(), datetime.now())

    # Консольный вывод
    print(f"[TelegramID: {user_id} | ChatID: {anon_id}] -> {display_text}")

    # Формируем сообщение для рассылки
    caption = f"<code>[{anon_id}]</code>\n{display_text}"

    # Рассылка всем пользователям (кроме автора и заблокированных)
    for uid in list(user_data.keys()):
        if uid == user_id or uid in banned_users:
            continue
        try:
            if message.text:
                await bot.send_message(chat_id=uid, text=caption)
            elif message.photo:
                await bot.send_photo(chat_id=uid, photo=message.photo[-1].file_id, caption=caption)
            elif message.video:
                await bot.send_video(chat_id=uid, video=message.video.file_id, caption=caption)
            elif message.document:
                await bot.send_document(chat_id=uid, document=message.document.file_id, caption=caption)
            elif message.sticker:
                await bot.send_sticker(chat_id=uid, sticker=message.sticker.file_id)
            elif message.animation:
                await bot.send_animation(chat_id=uid, animation=message.animation.file_id, caption=caption)
            elif message.voice:
                await bot.send_voice(chat_id=uid, voice=message.voice.file_id, caption=caption)
            elif message.audio:
                await bot.send_audio(chat_id=uid, audio=message.audio.file_id, caption=caption)
        except TelegramForbiddenError:
            print(f"⚠️ Бот заблокирован пользователем {uid}. Игнорируем.")
        except Exception as e:
            print(f"Ошибка отправки {uid}: {e}")

# --- Админ-команды ---
@dp.message(Command(commands=["ban"]))
async def cmd_ban(message: Message):
    if message.from_user.id not in ADMINS:
        return
    if not message.reply_to_message:
        await message.reply("Используй команду в ответ на сообщение пользователя.")
        return
    target_id = message.reply_to_message.from_user.id
    ban_user(target_id)
    await message.reply(f"⚠️ Пользователь {target_id} заблокирован.")

@dp.message(Command(commands=["unban"]))
async def cmd_unban(message: Message):
    if message.from_user.id not in ADMINS:
        return
    if not message.reply_to_message:
        await message.reply("Используй команду в ответ на сообщение пользователя.")
        return
    target_id = message.reply_to_message.from_user.id
    unban_user(target_id)
    await message.reply(f"✅ Пользователь {target_id} разбанен.")

# --------- Run ----------
async def main():
    print("🤖 Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
