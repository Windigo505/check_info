import httpx
import hashlib
import re
from bs4 import BeautifulSoup
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = "8325253736:AAFY5kaSu81zLVsXHm09LhCCCQfT2e5YeRQ"
CHECK_INTERVAL = 300  # каждые 5 минут
SITE_URL = "https://comicconastana.kz"
API_BASE = "https://widget.afisha.yandex.kz/api/tickets/v1"
 
CLIENT_KEY = "95ce097f-864a-49a6-b84b-847c07c2d8af"
HEADERS = {
    "Referer": "https://widget.afisha.yandex.kz/",
    "Origin": "https://widget.afisha.yandex.kz",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def fetch_sessions():
    r = httpx.get(
        f"{API_BASE}/events/832469/venues/sessions",
        params={
            "clientKey": CLIENT_KEY,
            "offset": "0", "limit": "20",
            "dateFrom": "2026-08-06",
            "dateTo": "2026-08-09",
            "regionId": "163",
            "req_number": "2"
        },
        headers=HEADERS
    )
    return r.json()["result"]["venues"]["items"][0]["sessions"]


def fetch_levels(session_key):
    r = httpx.get(
        f"{API_BASE}/sessions/{session_key}/hallplan/async",
        params={"clientKey": CLIENT_KEY, "req_number": "1"},
        headers=HEADERS
    )
    return r.json()["result"]["hallplan"]["levels"]


def format_message(sessions):
    lines = ["🎪 *Comic Con Astana 2026 — статистика билетов*\n"]
    total_available = 0

    for s in sessions:
        date = s["presentationSessionDate"]
        available = s["availableSeatCount"]
        status = s["saleStatus"]
        total_available += available

        status_emoji = "🟢" if status == "available" else "🔴"
        lines.append(f"{status_emoji} *{date}* — всего доступно: `{available:,}`")

        try:
            levels = fetch_levels(s["key"])
            for level in levels:
                name = level["name"]
                count = level["availableSeatCount"]
                price = level["prices"][0]["value"] // 100
                lines.append(f"   🎫 {name} — `{price:,} ₸` | мест: `{count:,}`")
        except Exception:
            lines.append("   ⚠️ Не удалось загрузить категории")

        lines.append("")

    lines.append(f"📊 *Итого доступно: {total_available:,} мест*")
    lines.append(f"🕐 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)

 


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Получаю данные...")
    try:
        sessions = fetch_sessions()
        message = format_message(sessions)
        await msg.edit_text(message, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")
# Хранилище предыдущего состояния
previous_state = {}


def fetch_site():
    r = httpx.get(SITE_URL, headers=HEADERS, follow_redirects=True)
    return r.text


def extract_state(html):
    soup = BeautifulSoup(html, "html.parser")

    # Все ссылки
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith("#"):
            links.add(href)

    # Все картинки
    images = set()
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if src:
            images.add(src)

    # Весь текст (очищенный)
    text = soup.get_text(separator=" ", strip=True)
    # Убираем пробелы и нормализуем
    text = re.sub(r'\s+', ' ', text).strip()

    # Хэш всей страницы
    page_hash = hashlib.md5(html.encode()).hexdigest()

    return {
        "hash": page_hash,
        "links": links,
        "images": images,
        "text": text,
    }


def compare_states(old, new):
    changes = []

    if old["hash"] == new["hash"]:
        return []  # ничего не изменилось

    # Новые ссылки
    new_links = new["links"] - old["links"]
    removed_links = old["links"] - new["links"]
    for link in new_links:
        changes.append(f"🔗 Новая ссылка: `{link}`")
    for link in removed_links:
        changes.append(f"❌ Удалена ссылка: `{link}`")

    # Новые картинки
    new_images = new["images"] - old["images"]
    removed_images = old["images"] - new["images"]
    for img in new_images:
        changes.append(f"🖼 Новая картинка: `{img}`")
    for img in removed_images:
        changes.append(f"🗑 Удалена картинка: `{img}`")

    # Изменения текста
    if old["text"] != new["text"]:
        old_words = set(old["text"].split())
        new_words = set(new["text"].split())
        added_words = new_words - old_words
        if added_words and len(added_words) < 50:
            sample = " ".join(list(added_words)[:20])
            changes.append(f"📝 Новый текст: `{sample}`")
        elif added_words:
            changes.append(f"📝 Текст изменился (много изменений)")

    return changes


# === КОМАНДЫ ===
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Comic Con Astana — мониторинг сайта*\n\n"
        "/check\_tickets — статистика билетов\n"
        "/check\_site — проверить сайт сейчас\n"
        "/monitor\_site — запустить мониторинг сайта\n"
        "/stop\_site — остановить мониторинг",
        parse_mode="Markdown"
    )


async def cmd_check_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Проверяю сайт...")
    try:
        html = fetch_site()
        state = extract_state(html)

        if not previous_state:
            previous_state.update(state)
            await msg.edit_text(
                f"✅ Начальное состояние сохранено\n"
                f"🔗 Ссылок: `{len(state['links'])}`\n"
                f"🖼 Картинок: `{len(state['images'])}`\n"
                f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                parse_mode="Markdown"
            )
        else:
            changes = compare_states(previous_state, state)
            if changes:
                previous_state.update(state)
                text = "🚨 *Найдены изменения на сайте!*\n\n" + "\n".join(changes[:20])
                text += f"\n\n🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                await msg.edit_text(text, parse_mode="Markdown")
            else:
                await msg.edit_text(
                    f"✅ Изменений нет\n"
                    f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="Markdown"
                )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_monitor_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if "site_task" in context.chat_data and not context.chat_data["site_task"].done():
        await update.message.reply_text("⚠️ Мониторинг уже запущен.")
        return

    # Сохраняем начальное состояние
    try:
        html = fetch_site()
        state = extract_state(html)
        previous_state.update(state)
        await update.message.reply_text(
            f"✅ Мониторинг сайта запущен!\n"
            f"🔗 Ссылок: `{len(state['links'])}`\n"
            f"🖼 Картинок: `{len(state['images'])}`\n"
            f"Проверка каждые {CHECK_INTERVAL // 60} минут",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при запуске: {e}")
        return

    import asyncio

    async def monitor_loop():
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            try:
                html = fetch_site()
                new_state = extract_state(html)
                changes = compare_states(previous_state, new_state)

                if changes:
                    previous_state.update(new_state)
                    text = "🚨 *Изменения на comicconastana.kz!*\n\n"
                    text += "\n".join(changes[:20])
                    text += f"\n\n🔗 {SITE_URL}\n"
                    text += f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="Markdown"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка мониторинга: {e}")

    import asyncio
    task = asyncio.create_task(monitor_loop())
    context.chat_data["site_task"] = task


async def cmd_stop_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.chat_data.get("site_task")
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 Мониторинг сайта остановлен.")
    else:
        await update.message.reply_text("⚠️ Мониторинг не был запущен.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check_site", cmd_check_site))
    app.add_handler(CommandHandler("monitor_site", cmd_monitor_site))
    app.add_handler(CommandHandler("stop_site", cmd_stop_site))
    app.add_handler(CommandHandler("check_tickets", cmd_check))
    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

