import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import whois as whois_lib
import dns.resolver
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Хранилище результатов поиска между сообщением и нажатием кнопки
SEARCH_CACHE: dict[str, str] = {}

PLATFORMS = {
    "GitHub": {"url": "https://github.com/{}", "error_type": "status_code", "error_code": 404},
    "Twitch": {"url": "https://www.twitch.tv/{}", "error_type": "message", "error_msg": "sorry_unavailable"},
    "YouTube (handle)": {"url": "https://www.youtube.com/@{}", "error_type": "status_code", "error_code": 404},
    "Reddit": {"url": "https://www.reddit.com/user/{}/about.json", "error_type": "status_code", "error_code": 404},
    "Instagram": {"url": "https://www.instagram.com/{}/", "error_type": "status_code", "error_code": 404},
    "Telegram": {"url": "https://t.me/{}", "error_type": "message", "error_msg": "If you have Telegram, you can contact"},
    "VK": {"url": "https://vk.com/{}", "error_type": "message", "error_msg": "Page not found"},
    "TikTok": {"url": "https://www.tiktok.com/@{}", "error_type": "message", "error_msg": "Couldn't find this account"},
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def check_platform(name: str, config: dict, username: str) -> tuple[str, str]:
    """Проверяет одну площадку по HTTP-статусу или маркеру в тексте."""
    url = config["url"].format(username)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
    except requests.RequestException as exc:
        return name, f"⚠️ Ошибка запроса ({exc.__class__.__name__})"

    if config["error_type"] == "status_code":
        exists = resp.status_code != config["error_code"] and resp.status_code < 400
    else:
        exists = config["error_msg"] not in resp.text

    if exists:
        return name, f"✅ Найден: {url}"
    return name, "❌ Не найден"


def generate_variants(username: str) -> list[str]:
    """Генерирует вероятные вариации ника (точки/подчёркивания/дефисы, обрезка цифр)."""
    variants: set[str] = set()

    stripped = re.sub(r"[._\-]", "", username)
    variants.add(stripped)

    for a, b in [(".", "_"), ("_", "."), ("-", "."), (".", "-"), ("_", "-"), ("-", "_")]:
        if a in username:
            variants.add(username.replace(a, b))

    match = re.match(r"^([A-Za-z]+)(\d+)$", username)
    if match:
        base, digits = match.groups()
        variants.add(f"{base}.{digits}")
        variants.add(f"{base}_{digits}")
        variants.add(f"{base}-{digits}")
        variants.add(base)

    variants.discard(username)
    return sorted(variants)


def search_variants(username: str) -> str:
    """Проверяет сгенерированные вариации ника на всех площадках."""
    variants = generate_variants(username)
    if not variants:
        return "ℹ️ Не удалось сгенерировать вариации для этого ника."

    all_checks = [(variant, name, cfg) for variant in variants for name, cfg in PLATFORMS.items()]

    found = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(check_platform, name, cfg, variant): (variant, name)
            for variant, name, cfg in all_checks
        }
        for future in as_completed(futures):
            variant, name = futures[future]
            _, status = future.result()
            if status.startswith("✅"):
                found.append((variant, name, status))

    if not found:
        return f"🔍 Проверено {len(variants)} вариаций «{username}»: {', '.join(variants)}\n\nСовпадений не найдено."

    lines = [f"🎯 Найдены похожие ники по «{username}»:\n"]
    for variant, name, status in found:
        lines.append(f"«{variant}» — {name}: {status}")
    return "\n".join(lines)


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

    order = list(PLATFORMS.keys())
    results.sort(key=lambda x: order.index(x[0]))

    lines = [f"🔎 Результаты поиска ника: {username}\n"]
    for name, status in results:
        lines.append(f"{name}: {status}")
    return "\n".join(lines)


IP_REGEX = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def is_valid_ipv4(ip: str) -> bool:
    match = IP_REGEX.match(ip)
    if not match:
        return False
    return all(0 <= int(octet) <= 255 for octet in match.groups())


def lookup_ip(ip: str) -> str:
    """Геолокация IP через ip-api.com (уровень узла провайдера, не точный адрес)."""
    if not is_valid_ipv4(ip):
        return "⚠️ Похоже, это не валидный IPv4-адрес."

    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}?lang=ru", timeout=8)
        data = resp.json()
    except requests.RequestException as exc:
        return f"⚠️ Ошибка запроса: {exc.__class__.__name__}"

    if data.get("status") != "success":
        return f"❌ Не удалось получить данные: {data.get('message', 'неизвестная ошибка')}"

    lines = [
        f"🌐 IP: {ip}",
        f"Страна: {data.get('country', '—')}",
        f"Регион: {data.get('regionName', '—')}",
        f"Город: {data.get('city', '—')}",
        f"Провайдер (ISP): {data.get('isp', '—')}",
        f"Организация: {data.get('org', '—')}",
        f"AS: {data.get('as', '—')}",
        f"Координаты (примерные): {data.get('lat')}, {data.get('lon')}",
    ]
    return "\n".join(lines)


def lookup_whois(domain: str) -> str:
    """Публичные регистрационные данные домена."""
    domain = domain.strip().lower().removeprefix("http://").removeprefix("https://").split("/")[0]

    try:
        data = whois_lib.whois(domain)
    except Exception as exc:
        return f"⚠️ Не удалось получить WHOIS: {exc}"

    if not data or not data.domain_name:
        return f"❌ Данные по домену «{domain}» не найдены."

    def fmt(value):
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value) if value else "—"

    lines = [
        f"🌍 WHOIS: {domain}",
        f"Регистратор: {fmt(data.registrar)}",
        f"Дата регистрации: {fmt(data.creation_date)}",
        f"Дата истечения: {fmt(data.expiration_date)}",
        f"Обновлён: {fmt(data.updated_date)}",
        f"NS-серверы: {fmt(data.name_servers)}",
        f"Страна: {fmt(getattr(data, 'country', None))}",
        f"Организация: {fmt(getattr(data, 'org', None))}",
    ]
    return "\n".join(lines)


EMAIL_REGEX = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")


def check_email(email: str) -> str:
    """Проверяет формат email и наличие MX-записей у домена (не сам ящик)."""
    match = EMAIL_REGEX.match(email.strip())
    if not match:
        return "⚠️ Неверный формат email."

    domain = match.group(1)
    lines = [f"📧 {email}", "Формат: ✅ корректный"]

    try:
        answers = dns.resolver.resolve(domain, "MX")
        mx_hosts = sorted(str(r.exchange).rstrip(".") for r in answers)
        lines.append(f"MX-записи домена: ✅ найдены ({len(mx_hosts)})")
        lines.append("Почтовые серверы: " + ", ".join(mx_hosts[:5]))
    except dns.resolver.NXDOMAIN:
        lines.append("MX-записи: ❌ домен не существует")
    except dns.resolver.NoAnswer:
        lines.append("MX-записи: ❌ не найдены")
    except Exception as exc:
        lines.append(f"MX-записи: ⚠️ ошибка проверки ({exc.__class__.__name__})")

    lines.append("\nℹ️ Не подтверждает существование конкретного ящика, только домена.")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 OSINT-помощник.\n\n"
        "Команды:\n"
        "/nick <username> — проверить ник на GitHub, Twitch, YouTube, Reddit, Instagram, VK, TikTok, Telegram\n"
        "После результата — кнопка «🎯 Похожие совпадения» (проверка вариаций ника).\n\n"
        "/ip <адрес> — геолокация IP\n"
        "/whois <domain.com> — регистрационные данные домена\n"
        "/email <адрес@домен> — проверка формата и MX-записей\n\n"
        "⚠️ Работает только с публично доступными данными. "
        "Используй ответственно и не для преследования людей."
    )


async def nick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /nick <username>")
        return

    username = context.args[0]
    await update.message.reply_text(f"Проверяю «{username}» на площадках, подожди...")

    result = search_username(username)

    search_id = uuid.uuid4().hex[:12]
    SEARCH_CACHE[search_id] = username

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎯 Похожие совпадения", callback_data=f"variants:{search_id}")]]
    )
    await update.message.reply_text(result, reply_markup=keyboard)


async def variants_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатие кнопки «Похожие совпадения»."""
    query = update.callback_query
    await query.answer()

    search_id = query.data.split(":", 1)[1]
    username = SEARCH_CACHE.get(search_id)

    if username is None:
        await query.message.reply_text("⚠️ Данные устарели. Повтори /nick <username>.")
        return

    await query.message.reply_text(f"Ищу вариации ника «{username}», подожди...")
    result = search_variants(username)
    await query.message.reply_text(result)


async def ip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /ip <адрес>")
        return
    await update.message.reply_text(lookup_ip(context.args[0]))


async def whois_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /whois <domain.com>")
        return
    await update.message.reply_text(f"Запрашиваю WHOIS для «{context.args[0]}»...")
    await update.message.reply_text(lookup_whois(context.args[0]))


async def email_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /email <адрес@домен.com>")
        return
    await update.message.reply_text(check_email(context.args[0]))


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Переменная окружения BOT_TOKEN не задана. "
            "На Railway: Variables → добавить BOT_TOKEN. "
            "Локально: export BOT_TOKEN='твой_токен'"
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nick", nick_command))
    app.add_handler(CallbackQueryHandler(variants_callback, pattern=r"^variants:"))
    app.add_handler(CommandHandler("ip", ip_command))
    app.add_handler(CommandHandler("whois", whois_command))
    app.add_handler(CommandHandler("email", email_command))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
