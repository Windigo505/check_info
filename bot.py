import os
import httpx
import hashlib
import re
import difflib
from functools import wraps
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin, urlparse
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import asyncio

# ⚠️ Токен лучше вынести в переменную окружения:
# BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_TOKEN = "8325253736:AAFl045ZAY-v_UK9X98m62qboolhsMJRr9Q"

CHECK_INTERVAL = 300
TICKET_CHECK_INTERVAL = 60
SITE_URL = "https://comicconastana.kz"
API_BASE = "https://widget.afisha.yandex.kz/api/tickets/v1"

CLIENT_KEY = "95ce097f-864a-49a6-b84b-847c07c2d8af"
WORKER_URL = "https://hidden-union-4445.daniil17032008f.workers.dev/"

HEADERS = {
    "Referer": "https://widget.afisha.yandex.kz/",
    "Origin": "https://widget.afisha.yandex.kz",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# ─── Доступ ────────────────────────────────────────────────────────────────
# ID пользователей, которым разрешено пользоваться ботом.
# Узнать свой id: написать @userinfobot в Telegram.
# Можно задать через переменную окружения ALLOWED_USERS="123,456,789"
# либо вписать напрямую в set() ниже.
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
} or {
    1762280778 
}


def restricted(func):
    """Декоратор: пропускает только пользователей из ALLOWED_USERS."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id not in ALLOWED_USERS:
            if update.message:
                await update.message.reply_text('⛔ У вас нет доступа к этому боту.')
            return
        return await func(update, context, *args, **kwargs)
    return wrapped


# ─── Шумовые паттерны — изменения, которые не несут смысла ───────────────────
NOISE_PATTERNS = [
    r'\b\d{10,}\b',           # длинные числа (токены, timestamps)
    r'[a-f0-9]{32,}',         # md5/sha хэши
    r'\?.*?=[\w%+]+',         # query-параметры
    r'\d+px',                 # размеры
    r'#[a-f0-9]{3,6}\b',      # цвета
]

# ─── Билеты ───────────────────────────────────────────────────────────────────
previous_tickets = {}
ticket_subscribers = {}
ticket_task = None

# ─── Сайт: { url -> state } ──────────────────────────────────────────────────
previous_site_states: dict[str, dict] = {}


# ═════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ САЙТА
# ═════════════════════════════════════════════════════════════════════════════

def is_noise(text: str) -> bool:
    """Возвращает True, если изменение скорее всего шумовое."""
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, '', text)
    return len(text.strip()) < 4


def normalize_url(url: str) -> str:
    """Убирает fragment и trailing slash для дедупликации."""
    p = urlparse(url)
    path = p.path.rstrip('/') or '/'
    return p._replace(fragment='', query='', path=path).geturl()


def collect_internal_links(html: str, base_url: str) -> dict[str, str]:
    """
    Возвращает словарь { normalized_url -> anchor_text } всех внутренних ссылок.
    """
    soup = BeautifulSoup(html, 'html.parser')
    base_domain = urlparse(base_url).netloc
    links: dict[str, str] = {}

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('mailto:', 'tel:', 'javascript:')):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc != base_domain:
            continue
        norm = normalize_url(full)
        anchor = a.get_text(strip=True) or parsed.path or norm
        anchor = anchor[:60]  # не длиннее 60 символов
        links[norm] = anchor

    return links


def extract_state(html: str) -> dict:
    """Извлекает структурированное состояние страницы."""
    soup = BeautifulSoup(html, 'html.parser')

    # Ссылки с анкорами
    links_with_anchors: dict[str, str] = {}
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href and not href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            anchor = a.get_text(strip=True) or href
            links_with_anchors[href] = anchor[:80]

    # Картинки с alt
    images_with_alt: dict[str, str] = {}
    for img in soup.find_all('img', src=True):
        src = img['src'].strip()
        if src:
            alt = img.get('alt', '').strip() or src
            images_with_alt[src] = alt[:80]

    # Чистый текст — разбитый на предложения для diff
    raw_text = soup.get_text(separator=' ', strip=True)
    raw_text = re.sub(r'\s+', ' ', raw_text).strip()
    # Разбиваем на «предложения» (по точке/восклицанию/вопросу + пробел)
    sentences = re.split(r'(?<=[.!?])\s+', raw_text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]

    page_hash = hashlib.md5(html.encode()).hexdigest()

    return {
        'hash': page_hash,
        'links': links_with_anchors,      # { href -> anchor }
        'images': images_with_alt,        # { src -> alt }
        'sentences': sentences,            # список предложений
    }


def smart_text_diff(old_sentences: list[str], new_sentences: list[str]) -> list[str]:
    """
    Возвращает человекочитаемый список изменений в тексте.
    Использует difflib для поиска добавленных/удалённых блоков.
    """
    changes = []
    matcher = difflib.SequenceMatcher(None, old_sentences, new_sentences, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue

        removed = old_sentences[i1:i2]
        added = new_sentences[j1:j2]

        if tag == 'insert':
            for s in added[:3]:
                if not is_noise(s):
                    short = s[:120] + ('…' if len(s) > 120 else '')
                    changes.append(f'📝 Добавлено: _{short}_')

        elif tag == 'delete':
            for s in removed[:3]:
                if not is_noise(s):
                    short = s[:120] + ('…' if len(s) > 120 else '')
                    changes.append(f'🗑 Удалено: _{short}_')

        elif tag == 'replace':
            # Показываем только если изменение не шумовое
            for old_s, new_s in zip(removed[:2], added[:2]):
                if not is_noise(old_s) and not is_noise(new_s):
                    old_short = old_s[:80] + ('…' if len(old_s) > 80 else '')
                    new_short = new_s[:80] + ('…' if len(new_s) > 80 else '')
                    changes.append(f'✏️ Изменено:\n  было: _{old_short}_\n  стало: _{new_short}_')

    return changes


def compare_states(old: dict, new: dict) -> list[str]:
    """Сравнивает два состояния страницы, возвращает список изменений."""
    if old['hash'] == new['hash']:
        return []

    changes = []

    # ── Ссылки ────────────────────────────────────────────────────────────────
    old_links = set(old['links'].keys())
    new_links_map = new['links']
    new_links = set(new_links_map.keys())

    for href in new_links - old_links:
        anchor = new_links_map[href]
        if not is_noise(href):
            changes.append(f'🔗 Новая ссылка: *{anchor}* → `{href}`')

    for href in old_links - new_links:
        anchor = old['links'][href]
        if not is_noise(href):
            changes.append(f'❌ Удалена ссылка: *{anchor}* → `{href}`')

    # ── Картинки ──────────────────────────────────────────────────────────────
    old_imgs = set(old['images'].keys())
    new_imgs_map = new['images']
    new_imgs = set(new_imgs_map.keys())

    for src in new_imgs - old_imgs:
        alt = new_imgs_map[src]
        if not is_noise(src):
            changes.append(f'🖼 Новая картинка: *{alt}*')

    for src in old_imgs - new_imgs:
        alt = old['images'][src]
        if not is_noise(src):
            changes.append(f'🗑 Удалена картинка: *{alt}*')

    # ── Текст ─────────────────────────────────────────────────────────────────
    text_changes = smart_text_diff(old['sentences'], new['sentences'])
    changes.extend(text_changes)

    return changes


def fetch_page(url: str) -> str:
    """Загружает одну страницу."""
    r = httpx.get(url, headers={**HEADERS, 'Accept-Encoding': 'identity'},
                  follow_redirects=True, timeout=15)
    return r.text


def fetch_all_pages() -> dict[str, str]:
    """
    Загружает главную страницу, собирает все внутренние ссылки,
    затем загружает каждую. Возвращает { url -> html }.
    """
    main_html = fetch_page(SITE_URL)
    pages: dict[str, str] = {normalize_url(SITE_URL): main_html}

    internal = collect_internal_links(main_html, SITE_URL)
    for url in internal:
        if url not in pages:
            try:
                pages[url] = fetch_page(url)
            except Exception:
                pass  # недоступная страница — пропускаем

    return pages


def snapshot_all_pages() -> dict[str, dict]:
    """Возвращает { url -> state } для всех страниц сайта."""
    pages = fetch_all_pages()
    return {url: extract_state(html) for url, html in pages.items()}


# ═════════════════════════════════════════════════════════════════════════════
#  БИЛЕТЫ (без изменений)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_sessions():
    r = httpx.get(
        WORKER_URL,
        params={
            '_target': f'{API_BASE}/events/832469/venues/sessions',
            'clientKey': CLIENT_KEY,
            'offset': '0', 'limit': '20',
            'dateFrom': '2026-08-06',
            'dateTo': '2026-08-09',
            'regionId': '163',
            'req_number': '2'
        },
        headers={**HEADERS, 'Accept-Encoding': 'identity'}
    )
    return r.json()['result']['venues']['items'][0]['sessions']


def fetch_levels(session_key):
    r = httpx.get(
        WORKER_URL,
        params={
            '_target': f'{API_BASE}/sessions/{session_key}/hallplan/async',
            'clientKey': CLIENT_KEY,
            'req_number': '1'
        },
        headers=HEADERS
    )
    return r.json()['result']['hallplan']['levels']


def format_message(sessions):
    lines = ['🎪 *Comic Con Astana 2026 — статистика билетов*\n']
    total_available = 0

    for s in sessions:
        date = s['presentationSessionDate']
        available = s['availableSeatCount']
        status = s['saleStatus']
        total_available += available

        status_emoji = '🟢' if status == 'available' else '🔴'
        lines.append(f'{status_emoji} *{date}* — всего доступно: `{available:,}`')

        try:
            levels = fetch_levels(s['key'])
            for level in levels:
                name = level['name']
                count = level['availableSeatCount']
                price = level['prices'][0]['value'] // 100
                lines.append(f'   🎫 {name} — `{price:,} ₸` | мест: `{count:,}`')
        except Exception:
            lines.append('   ⚠️ Не удалось загрузить категории')

        lines.append('')

    lines.append(f'📊 *Итого доступно: {total_available:,} мест*')
    lines.append(f'🕐 Обновлено: {datetime.now().strftime("%d.%m.%Y %H:%M")}')
    return '\n'.join(lines)


def get_ticket_snapshot():
    sessions = fetch_sessions()
    snapshot = {}
    for s in sessions:
        date = s['presentationSessionDate']
        status = s['saleStatus']
        total = s['availableSeatCount']

        levels_data = {}
        try:
            levels = fetch_levels(s['key'])
            for level in levels:
                levels_data[level['name']] = {
                    'count': level['availableSeatCount'],
                    'price': level['prices'][0]['value'] // 100
                }
        except Exception:
            pass

        snapshot[date] = {'status': status, 'total': total, 'levels': levels_data}
    return snapshot


def compare_tickets(old, new):
    changes = []
    for date in new:
        if date not in old:
            continue
        old_day = old[date]
        new_day = new[date]

        if old_day['status'] == 'available' and new_day['status'] != 'available':
            changes.append(f'🔴 *{date}* — билеты ЗАКОНЧИЛИСЬ (sold out)!')
        elif old_day['status'] != 'available' and new_day['status'] == 'available':
            changes.append(f'🟢 *{date}* — билеты снова в продаже!')

        diff = new_day['total'] - old_day['total']
        if diff < 0:
            changes.append(f'📉 *{date}* — куплено: `{abs(diff)}` (осталось: `{new_day["total"]:,}`)')
        elif diff > 0:
            changes.append(f'📈 *{date}* — добавлено: `{diff}` (стало: `{new_day["total"]:,}`)')

        for name in new_day['levels']:
            if name not in old_day['levels']:
                continue
            old_count = old_day['levels'][name]['count']
            new_count = new_day['levels'][name]['count']
            cat_diff = new_count - old_count
            if cat_diff < 0:
                changes.append(f'   🎫 *{name}* ({date}): куплено `{abs(cat_diff)}` → осталось `{new_count:,}`')
            elif cat_diff > 0:
                changes.append(f'   ➕ *{name}* ({date}): добавлено `{cat_diff}` → стало `{new_count:,}`')
            if old_count >= 50 and new_count < 50 and new_count > 0:
                changes.append(f'   ⚠️ *{name}* ({date}): осталось мало мест — `{new_count}`!')
            if old_count > 0 and new_count == 0:
                changes.append(f'   ❌ *{name}* ({date}): билеты закончились!')

    return changes


# ═════════════════════════════════════════════════════════════════════════════
#  МОНИТОРИНГ БИЛЕТОВ
# ═════════════════════════════════════════════════════════════════════════════

async def ticket_monitor_loop(bot):
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
                text = '🚨 *Изменения в билетах Comic Con Astana!*\n\n'
                text += '\n'.join(changes[:30])
                text += f'\n\n🕐 {datetime.now().strftime("%d.%m.%Y %H:%M")}'
                # Рассылаем только тем, кто в whitelist (на случай если id
                # оказался удалён из списка уже после подписки)
                for chat_id in list(ticket_subscribers.keys()):
                    if chat_id not in ALLOWED_USERS:
                        continue
                    try:
                        await bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
                    except Exception:
                        pass
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  ПУБЛИЧНАЯ КОМАНДА — УЗНАТЬ СВОЙ ID
# ═════════════════════════════════════════════════════════════════════════════

async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Доступна всем без ограничений — просто сообщает Telegram id пользователя."""
    user = update.effective_user
    await update.message.reply_text(
        f'🆔 Ваш Telegram ID: `{user.id}`',
        parse_mode='Markdown'
    )


# ═════════════════════════════════════════════════════════════════════════════
#  КОМАНДЫ — БИЛЕТЫ
# ═════════════════════════════════════════════════════════════════════════════

@restricted
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text('🔄 Получаю данные...')
    try:
        sessions = fetch_sessions()
        await msg.edit_text(format_message(sessions), parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f'❌ Ошибка: {e}')


@restricted
async def cmd_track_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global previous_tickets, ticket_task
    chat_id = update.effective_chat.id
    ticket_subscribers[chat_id] = True
    msg = await update.message.reply_text('🔄 Запускаю трекинг...')
    try:
        snapshot = get_ticket_snapshot()
        previous_tickets = snapshot
        if ticket_task is None or ticket_task.done():
            ticket_task = asyncio.create_task(ticket_monitor_loop(context.bot))

        lines = ['✅ *Трекинг билетов запущен!*\n',
                 'Отслеживаю:', '• 📉 Покупки', '• 📈 Добавления',
                 '• ❌ Окончание', '• 🔴 Sold out',
                 f'\n🕐 Проверка каждую минуту\n\nТекущее состояние:']
        for date, data in snapshot.items():
            e = '🟢' if data['status'] == 'available' else '🔴'
            lines.append(f'{e} *{date}* — `{data["total"]:,}` мест')
        await msg.edit_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f'❌ Ошибка: {e}')


@restricted
async def cmd_track_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ticket_subscribers:
        del ticket_subscribers[chat_id]
        await update.message.reply_text('🛑 Трекинг билетов остановлен.')
    else:
        await update.message.reply_text('⚠️ Трекинг не был запущен.')


@restricted
async def cmd_track_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text('🔄 Получаю текущее состояние...')
    try:
        snapshot = get_ticket_snapshot()
        lines = ['📊 *Текущее состояние билетов*\n']
        for date, data in snapshot.items():
            e = '🟢' if data['status'] == 'available' else '🔴'
            lines.append(f'{e} *{date}* — всего: `{data["total"]:,}`')
            for name, level in data['levels'].items():
                lines.append(f'   🎫 {name} — `{level["price"]:,} ₸` | `{level["count"]:,}` мест')
            lines.append('')
        tracking = '✅ активен' if chat_id in ticket_subscribers else '❌ не запущен'
        lines.append(f'🔍 Трекинг: {tracking}')
        lines.append(f'🕐 {datetime.now().strftime("%d.%m.%Y %H:%M")}')
        await msg.edit_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f'❌ Ошибка: {e}')


# ═════════════════════════════════════════════════════════════════════════════
#  КОМАНДЫ — САЙТ
# ═════════════════════════════════════════════════════════════════════════════
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ALLOWED_USERS:
        await update.message.reply_text(
            '⛔ У вас нет доступа к этому боту.\n\n'
            'Используйте /getid, чтобы узнать свой Telegram ID, '
            'и отправьте его администратору для получения доступа.'
        )
        return
    await update.message.reply_text(
        '👋 *Comic Con Astana — мониторинг*\n\n'
        '📊 *Билеты:*\n'
        '/check\\_tickets — статистика билетов\n'
        '/track\\_start — запустить трекинг билетов\n'
        '/track\\_stop — остановить трекинг\n'
        '/track\\_status — текущее состояние\n\n'
        '🌐 *Сайт:*\n'
        '/check\\_site — проверить сайт сейчас\n'
        '/monitor\\_site — запустить мониторинг всех страниц\n'
        '/stop\\_site — остановить мониторинг\n'
        '/list\\_pages — список отслеживаемых страниц',
        parse_mode='Markdown'
    )


@restricted
async def cmd_check_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text('🔄 Сканирую все страницы сайта...')
    try:
        new_states = snapshot_all_pages()

        if not previous_site_states:
            previous_site_states.update(new_states)
            await msg.edit_text(
                f'✅ Начальное состояние сохранено\n'
                f'📄 Страниц найдено: `{len(new_states)}`\n'
                f'🕐 {datetime.now().strftime("%d.%m.%Y %H:%M")}',
                parse_mode='Markdown'
            )
            return

        all_changes = []
        for url, new_state in new_states.items():
            if url not in previous_site_states:
                all_changes.append(f'🆕 Новая страница: `{url}`')
                continue
            page_changes = compare_states(previous_site_states[url], new_state)
            if page_changes:
                path = urlparse(url).path or '/'
                all_changes.append(f'\n📄 *{path}*')
                all_changes.extend(page_changes[:5])

        for url in previous_site_states:
            if url not in new_states:
                all_changes.append(f'🗑 Страница исчезла: `{url}`')

        previous_site_states.update(new_states)

        if all_changes:
            text = '🚨 *Найдены изменения на сайте!*\n' + '\n'.join(all_changes[:25])
            text += f'\n\n🔗 {SITE_URL}\n🕐 {datetime.now().strftime("%d.%m.%Y %H:%M")}'
        else:
            text = f'✅ Изменений нет на всех {len(new_states)} страницах\n🕐 {datetime.now().strftime("%d.%m.%Y %H:%M")}'

        await msg.edit_text(text, parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f'❌ Ошибка: {e}')


@restricted
async def cmd_list_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not previous_site_states:
        await update.message.reply_text('⚠️ Сначала запустите /check\\_site или /monitor\\_site', parse_mode='Markdown')
        return
    lines = [f'📄 *Отслеживаемые страницы ({len(previous_site_states)}):*\n']
    for url in sorted(previous_site_states.keys()):
        path = urlparse(url).path or '/'
        lines.append(f'• `{path}`')
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')


@restricted
async def cmd_monitor_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if 'site_task' in context.chat_data and not context.chat_data['site_task'].done():
        await update.message.reply_text('⚠️ Мониторинг уже запущен.')
        return

    msg = await update.message.reply_text('🔄 Сканирую сайт для начального снимка...')
    try:
        new_states = snapshot_all_pages()
        previous_site_states.update(new_states)
        await msg.edit_text(
            f'✅ Мониторинг запущен!\n'
            f'📄 Страниц: `{len(new_states)}`\n'
            f'🕐 Проверка каждые {CHECK_INTERVAL // 60} минут',
            parse_mode='Markdown'
        )
    except Exception as e:
        await msg.edit_text(f'❌ Ошибка при запуске: {e}')
        return

    async def monitor_loop():
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            try:
                new_states = snapshot_all_pages()
                all_changes = []

                for url, new_state in new_states.items():
                    if url not in previous_site_states:
                        all_changes.append(f'🆕 Новая страница: `{url}`')
                        continue
                    page_changes = compare_states(previous_site_states[url], new_state)
                    if page_changes:
                        path = urlparse(url).path or '/'
                        all_changes.append(f'\n📄 *{path}*')
                        all_changes.extend(page_changes[:5])

                for url in previous_site_states:
                    if url not in new_states:
                        all_changes.append(f'🗑 Страница исчезла: `{url}`')

                previous_site_states.update(new_states)

                if all_changes:
                    text = '🚨 *Изменения на comicconastana.kz!*\n'
                    text += '\n'.join(all_changes[:25])
                    text += f'\n\n🔗 {SITE_URL}\n🕐 {datetime.now().strftime("%d.%m.%Y %H:%M")}'
                    # chat_id зафиксирован на момент запуска — он и так
                    # принадлежит разрешённому пользователю (проверено при /monitor_site)
                    if chat_id in ALLOWED_USERS:
                        await context.bot.send_message(
                            chat_id=chat_id, text=text, parse_mode='Markdown'
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    task = asyncio.create_task(monitor_loop())
    context.chat_data['site_task'] = task


@restricted
async def cmd_stop_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.chat_data.get('site_task')
    if task and not task.done():
        task.cancel()
        await update.message.reply_text('🛑 Мониторинг сайта остановлен.')
    else:
        await update.message.reply_text('⚠️ Мониторинг не был запущен.')


# ═════════════════════════════════════════════════════════════════════════════
#  WEB-СЕРВЕР (keepalive)
# ═════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_web():
    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    if not ALLOWED_USERS:
        print('⚠️  ВНИМАНИЕ: ALLOWED_USERS пуст — никто не сможет пользоваться ботом.')
        print('    Задай переменную окружения ALLOWED_USERS="id1,id2" или впиши id в код.')

    Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('getid', cmd_getid))
    app.add_handler(CommandHandler('check_site', cmd_check_site))
    app.add_handler(CommandHandler('monitor_site', cmd_monitor_site))
    app.add_handler(CommandHandler('stop_site', cmd_stop_site))
    app.add_handler(CommandHandler('list_pages', cmd_list_pages))
    app.add_handler(CommandHandler('check_tickets', cmd_check))
    app.add_handler(CommandHandler('track_start', cmd_track_start))
    app.add_handler(CommandHandler('track_stop', cmd_track_stop))
    app.add_handler(CommandHandler('track_status', cmd_track_status))
    print('Бот запущен...')
    app.run_polling()


if __name__ == '__main__':
    main()
