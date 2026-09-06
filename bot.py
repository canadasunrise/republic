"""
OSINT-бот для Telegram: извлечение EXIF-метаданных из фото
и поиск публичного присутствия никнейма на разных площадках.

Использует ТОЛЬКО публично доступные HTTP-запросы (проверка,
существует ли страница с таким юзернеймом) — не обходит
авторизацию, не парсит закрытые данные, не использует утечки.

Требуемые библиотеки:
    pip install python-telegram-bot==21.* pillow exifread requests

Запуск:
    export BOT_TOKEN="твой_токен_от_BotFather"
    python bot.py
"""

import logging
import os
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import exifread
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СВОЙ_ТОКЕН_СЮДА")

# ---------------------------------------------------------------------------
# 1. Список площадок для проверки username.
#    Логика: подставляем ник в URL и смотрим на код ответа/содержимое.
#    Некоторые сайты возвращают 200 даже для несуществующих страниц,
#    поэтому для части площадок дополнительно проверяем маркер в тексте.
# ---------------------------------------------------------------------------
PLATFORMS = {
    "GitHub": {
        "url": "https://github.com/{}",
        "error_type": "status_code",
        "error_code": 404,
    },
    "Twitch": {
        "url": "https://www.twitch.tv/{}",
        "error_type": "message",
        "error_msg": "sorry_unavailable",  # Twitch рендерит через JS, работает не всегда надёжно
    },
    "YouTube (handle)": {
        "url": "https://www.youtube.com/@{}",
        "error_type": "status_code",
        "error_code": 404,
    },
    "Reddit": {
        "url": "https://www.reddit.com/user/{}/about.json",
        "error_type": "status_code",
        "error_code": 404,
    },
    "Instagram": {
        "url": "https://www.instagram.com/{}/",
        "error_type": "status_code",
        "error_code": 404,
    },
    "Telegram": {
        "url": "https://t.me/{}",
        "error_type": "message",
        "error_msg": "If you have Telegram, you can contact",
        # На публичной странице t.me это сообщение появляется вместе
        # с кнопкой "Send message" даже для существующих юзеров —
        # поэтому Telegram лучше проверять вручную, тут только намёк.
    },
    "VK": {
        "url": "https://vk.com/{}",
        "error_type": "message",
        "error_msg": "Page not found",
    },
    "TikTok": {
        "url": "https://www.tiktok.com/@{}",
        "error_type": "message",
        "error_msg": "Couldn't find this account",
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def check_platform(name: str, config: dict, username: str) -> tuple[str, str]:
    """Проверяет одну площадку. Возвращает (название, результат)."""
    url = config["url"].format(username)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
    except requests.RequestException as exc:
        return name, f"⚠️ Ошибка запроса ({exc.__class__.__name__})"

    if config["error_type"] == "status_code":
        exists = resp.status_code != config["error_code"] and resp.status_code < 400
    else:  # message-based check
        exists = config["error_msg"] not in resp.text

    if exists:
        return name, f"✅ Найден: {url}"
    return name, "❌ Не найден"


def search_username(username: str) -> str:
    """Проверяет никнейм на всех площадках параллельно."""
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(check_platform, name, cfg, username): name
            for name, cfg in PLATFORMS.items()
        }
        for future in as_completed(futures):
            results.append(future.result())

    # Сортируем в исходном порядке словаря PLATFORMS
    order = list(PLATFORMS.keys())
    results.sort(key=lambda x: order.index(x[0]))

    lines = [f"🔎 Результаты поиска ника: {username}\n"]
    for name, status in results:
        lines.append(f"{name}: {status}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. Извлечение EXIF из фото
# ---------------------------------------------------------------------------
def _dms_to_decimal(dms, ref) -> float:
    """Конвертирует GPS-координаты формата EXIF (градусы/минуты/секунды) в десятичные."""
    degrees = float(dms.values[0].num) / float(dms.values[0].den)
    minutes = float(dms.values[1].num) / float(dms.values[1].den)
    seconds = float(dms.values[2].num) / float(dms.values[2].den)
    result = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        result = -result
    return result


def extract_exif(file_bytes: bytes) -> str:
    """Извлекает и форматирует EXIF-данные из байтов файла изображения."""
    tags = exifread.process_file(io.BytesIO(file_bytes), details=False)

    if not tags:
        return "ℹ️ EXIF-данные отсутствуют (файл был сжат/очищен, например Telegram-превью или соцсеть)."

    lines = ["📋 Найденные EXIF-метаданные:\n"]

    interesting = {
        "Image Make": "Производитель устройства",
        "Image Model": "Модель устройства",
        "EXIF DateTimeOriginal": "Дата/время съёмки",
        "EXIF ExposureTime": "Выдержка",
        "EXIF FNumber": "Диафрагма",
        "EXIF ISOSpeedRatings": "ISO",
        "EXIF FocalLength": "Фокусное расстояние",
        "Image Software": "ПО обработки",
    }
    for tag, label in interesting.items():
        if tag in tags:
            lines.append(f"• {label}: {tags[tag]}")

    # GPS
    gps_lat = tags.get("GPS GPSLatitude")
    gps_lat_ref = tags.get("GPS GPSLatitudeRef")
    gps_lon = tags.get("GPS GPSLongitude")
    gps_lon_ref = tags.get("GPS GPSLongitudeRef")

    if gps_lat and gps_lon and gps_lat_ref and gps_lon_ref:
        lat = _dms_to_decimal(gps_lat, str(gps_lat_ref))
        lon = _dms_to_decimal(gps_lon, str(gps_lon_ref))
        maps_url = f"https://www.google.com/maps?q={lat},{lon}"
        lines.append(f"\n📍 GPS-координаты: {lat:.6f}, {lon:.6f}")
        lines.append(f"🗺 Карта: {maps_url}")
    else:
        lines.append("\n📍 GPS-данные не найдены в этом файле.")

    if len(lines) == 1:
        lines.append("Только служебные теги без содержательной информации.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Хендлеры Telegram
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 OSINT-помощник.\n\n"
        "Команды:\n"
        "/nick <username> — проверить ник на GitHub, Twitch, YouTube, "
        "Reddit, Instagram, VK, TikTok, Telegram\n\n"
        "Просто пришли фото ФАЙЛОМ (как документ, не сжатым) — "
        "покажу его EXIF-метаданные, если они есть.\n\n"
        "⚠️ Инструмент работает только с публично доступными данными "
        "и метаданными файлов, которые тебе прислали. Используй ответственно "
        "и не для преследования конкретных людей."
    )


async def nick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /nick <username>")
        return

    username = context.args[0]
    await update.message.reply_text(f"Проверяю «{username}» на площадках, подожди...")

    result = search_username(username)
    await update.message.reply_text(result)


async def handle_document_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает фото, присланное как документ (сохраняет EXIF)."""
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        await update.message.reply_text("Это не изображение.")
        return

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()

    result = extract_exif(bytes(file_bytes))
    await update.message.reply_text(result)


async def handle_compressed_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает фото, присланное обычным способом (Telegram обычно чистит EXIF)."""
    await update.message.reply_text(
        "⚠️ Это фото отправлено сжатым — Telegram, скорее всего, уже стёр EXIF.\n"
        "Чтобы проверить реальные метаданные, отправь файл как ДОКУМЕНТ "
        "(скрепка → Файл, без превью-сжатия)."
    )


def main() -> None:
    if BOT_TOKEN == "ВСТАВЬ_СВОЙ_ТОКЕН_СЮДА":
        raise SystemExit(
            "Укажи токен бота: export BOT_TOKEN='твой_токен' перед запуском"
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nick", nick_command))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_photo))
    app.add_handler(MessageHandler(filters.PHOTO, handle_compressed_photo))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
