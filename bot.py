import httpx
import hashlib
import re
from bs4 import BeautifulSoup
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import asyncio

BOT_TOKEN = "8325253736:AAELcVhZOfv9tX4G_-FwVYXmPHV-vQxeOmI"
CHECK_INTERVAL = 300
TICKET_CHECK_INTERVAL = 60  # каждую минуту
SITE_URL = "https://comicconastana.kz"
API_BASE = "https://widget.afisha.yandex.kz/api/tickets/v1"

CLIENT_KEY = "95ce097f-864a-49a6-b84b-847c07c2d8af"
WORKER_URL = "https://yandex-proxy.daniil17032008f.workers.dev"

HEADERS = {
    "Referer": "https://widget.afisha.yandex.kz/",
    "Origin": "https://widget.afisha.yandex.kz",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Хранилище предыдущего состояния билетов
previous_tickets = {}
# Подписчики трекинга: chat_id -> True
ticket_subscribers = {}
# Задача трекинга
ticket_task = None


def fetch_sessions():
    r = httpx.get(
        WORKER_URL,
        params={
            "_target": f"{API_BASE}/events/832469/venues/sessions",
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
        WORKER_URL,
        params={
            "_target": f"{API_BASE}/sessions/{session_key}/hallplan/async",
            "clientKey": CLIENT_KEY,
            "req_number": "1"
        },
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


def get_ticket_snapshot():
    """Получить снимок всех билетов по датам и категориям"""
    sessions = fetch_sessions()
    snapshot = {}
    for s in sessions:
        date = s["presentationSessionDate"]
        status = s["saleStatus"]
        total = s["availableSeatCount"]

        levels_data = {}
        try:
            levels = fetch_levels(s["key"])
            for level in levels:
                levels_data[level["name"]] = {
                    "count": level["availableSeatCount"],
                    "price": level["prices"][0]["value"] // 100
                }
        except Exception:
            pass

        snapshot[date] = {
            "status": status,
            "total": total,
            "levels": levels_data
        }
    return snapshot


def compare_tickets(old, new):
    """Сравнить два снимка и вернуть список изменений"""
    changes = []

    for date in new:
        if date not in old:
            continue

        old_day = old[date]
        new_day = new[date]

        # Статус изменился на sold out
        if old_day["status"] == "available" and new_day["status"] != "available":
            changes.append(f"🔴 *{date}* — билеты ЗАКОНЧИЛИСЬ (sold out)!")

        # Статус вернулся
        elif old_day["status"] != "available" and new_day["status"] == "available":
            changes.append(f"🟢 *{date}* — билеты снова в продаже!")

        # Общее количество
        old_total = old_day["total"]
        new_total = new_day["total"]
        diff = new_total - old_total

        if diff < 0:
            changes.append(f"📉 *{date}* — куплено билетов: `{abs(diff)}` (осталось: `{new_total:,}`)")
        elif diff > 0:
            changes.append(f"📈 *{date}* — добавлено билетов: `{diff}` (стало: `{new_total:,}`)")

        # По категориям
        for name in new_day["levels"]:
            if name not in old_day["levels"]:
                continue

            old_count = old_day["levels"][name]["count"]
            new_count = new_day["levels"][name]["count"]
            cat_diff = new_count - old_count

            if cat_diff < 0:
                changes.append(f"   🎫 *{name}* ({date}): куплено `{abs(cat_diff)}` → осталось `{new_count:,}`")
            elif cat_diff > 0:
                changes.append(f"   ➕ *{name}* ({date}): добавлено `{cat_diff}` → стало `{new_count:,}`")

            # Категория заканчивается (меньше 50 мест)
            if old_count >= 50 and new_count < 50 and new_count > 0:
                changes.append(f"   ⚠️ *{name}* ({date}): осталось мало мест — `{new_count}`!")

            # Категория закончилась
            if old_count > 0 and new_count == 0:
                changes.append(f"   ❌ *{name}* ({date}): билеты закончились!")

    return changes


async def ticket_monitor_loop(bot):
    """Фоновый цикл мониторинга билетов"""
    global previous_tickets

    while True:
        await asyncio.sleep(TICKET_CHECK_INTERVAL)
        try:
            new_snapshot = get_ticket_snapshot()

            if not previous_tickets:
                previous_tickets = new_snapshot
                continue

            changes = compare_tickets(previous_tickets, new_snapshot)
            previous_tickets = new_snapshot

            if changes and ticket_subscribers:
                text = "🚨 *Изменения в билетах Comic Con Astana!*\n\n"
                text += "\n".join(changes[:30])
                text += f"\n\n🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"

                for chat_id in list(ticket_subscribers.keys()):
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

        except Exception:
            pass


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Получаю данные...")
    try:
        sessions = fetch_sessions()
        message = format_message(sessions)
        await msg.edit_text(message, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_track_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global previous_tickets, ticket_task

    chat_id = update.effective_chat.id
    ticket_subscribers[chat_id] = True

    msg = await update.message.reply_text("🔄 Запускаю трекинг...")

    try:
        snapshot = get_ticket_snapshot()
        previous_tickets = snapshot

        # Запускаем фоновый цикл если ещё не запущен
        if ticket_task is None or ticket_task.done():
            ticket_task = asyncio.create_task(
                ticket_monitor_loop(context.bot)
            )

        lines = ["✅ *Трекинг билетов запущен!*\n"]
        lines.append("Отслеживаю:")
        lines.append("• 📉 Покупки билетов")
        lines.append("• 📈 Добавление билетов")
        lines.append("• ❌ Окончание билетов")
        lines.append("• 🔴 Смена статуса на sold out")
        lines.append(f"\n🕐 Проверка каждую минуту")
        lines.append(f"\nТекущее состояние:")

        for date, data in snapshot.items():
            status_emoji = "🟢" if data["status"] == "available" else "🔴"
            lines.append(f"{status_emoji} *{date}* — `{data['total']:,}` мест")

        await msg.edit_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_track_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id in ticket_subscribers:
        del ticket_subscribers[chat_id]
        await update.message.reply_text("🛑 Трекинг билетов остановлен.")
    else:
        await update.message.reply_text("⚠️ Трекинг не был запущен.")


async def cmd_track_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔄 Получаю текущее состояние...")

    try:
        snapshot = get_ticket_snapshot()
        lines = ["📊 *Текущее состояние билетов*\n"]

        for date, data in snapshot.items():
            status_emoji = "🟢" if data["status"] == "available" else "🔴"
            lines.append(f"{status_emoji} *{date}* — всего: `{data['total']:,}`")
            for name, level in data["levels"].items():
                lines.append(f"   🎫 {name} — `{level['price']:,} ₸` | `{level['count']:,}` мест")
            lines.append("")

        tracking = "✅ активен" if chat_id in ticket_subscribers else "❌ не запущен"
        lines.append(f"🔍 Трекинг: {tracking}")
        lines.append(f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}")

        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


# Хранилище предыдущего состояния сайта
previous_state = {}


def fetch_site():
    r = httpx.get(SITE_URL, headers=HEADERS, follow_redirects=True)
    return r.text


def extract_state(html):
    soup = BeautifulSoup(html, "html.parser")

    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith("#"):
            links.add(href)

    images = set()
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if src:
            images.add(src)

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r'\s+', ' ', text).strip()
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
        return []

    new_links = new["links"] - old["links"]
    removed_links = old["links"] - new["links"]
    for link in new_links:
        changes.append(f"🔗 Новая ссылка: `{link}`")
    for link in removed_links:
        changes.append(f"❌ Удалена ссылка: `{link}`")

    new_images = new["images"] - old["images"]
    removed_images = old["images"] - new["images"]
    for img in new_images:
        changes.append(f"🖼 Новая картинка: `{img}`")
    for img in removed_images:
        changes.append(f"🗑 Удалена картинка: `{img}`")

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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Comic Con Astana — мониторинг*\n\n"
        "📊 *Билеты:*\n"
        "/check\\_tickets — статистика билетов\n"
        "/track\\_start — запустить трекинг билетов\n"
        "/track\\_stop — остановить трекинг\n"
        "/track\\_status — текущее состояние\n\n"
        "🌐 *Сайт:*\n"
        "/check\\_site — проверить сайт сейчас\n"
        "/monitor\\_site — запустить мониторинг сайта\n"
        "/stop\\_site — остановить мониторинг",
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
            except Exception:
                pass

    task = asyncio.create_task(monitor_loop())
    context.chat_data["site_task"] = task


async def cmd_stop_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.chat_data.get("site_task")
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 Мониторинг сайта остановлен.")
    else:
        await update.message.reply_text("⚠️ Мониторинг не был запущен.")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def run_web():
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


def main():
    Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check_site", cmd_check_site))
    app.add_handler(CommandHandler("monitor_site", cmd_monitor_site))
    app.add_handler(CommandHandler("stop_site", cmd_stop_site))
    app.add_handler(CommandHandler("check_tickets", cmd_check))
    app.add_handler(CommandHandler("track_start", cmd_track_start))
    app.add_handler(CommandHandler("track_stop", cmd_track_stop))
    app.add_handler(CommandHandler("track_status", cmd_track_status))
    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
