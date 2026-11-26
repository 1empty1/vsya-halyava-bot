"""Headless version of the existing desktop scanner; credentials are environment-only."""
from telethon.sessions import StringSession
from types import SimpleNamespace
notification = SimpleNamespace(notify=lambda **kwargs: None)
import asyncio
import re
import time
import sqlite3
import aiohttp
from queue import Queue
from twitchio.ext import commands
from telethon import TelegramClient, events
from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate
import os
from telegram_runtime import TimedTelegramClient, supervise_telegram, setup_timing_log, event_age, telegram_status
APP_DIR = os.getenv('SCANNER_DATA_DIR', '/tmp/halyava')
DB_PATH = os.path.join(APP_DIR, 'scanner.sqlite3')
PROMO_PATTERN = re.compile('MD-[A-Z0-9\\*?]{4}-[A-Z0-9\\*?]{4}(?:-[A-Z0-9\\*?]{0,4})?', re.IGNORECASE)

def strip_markdown(text):
    """Убирает markdown-разметку, СОХРАНЯЯ одиночную '*' внутри кодов.

    Парная '*' - это жирный шрифт, её снимаем. Звёздочка-заглушка в коде всегда
    одиночная, поэтому после снятия парных она остаётся на месте.
    Ссылки [**Текст**](url) превращаем в "Текст (url)", чтобы название кейса и
    ссылка остались в одной строке с кодом.
    """
    if not text:
        return ''
    text = re.sub('\\[([^\\]\\[]+)\\]\\((https?://[^)\\s]+)\\)', '\\1 (\\2)', text)
    text = re.sub('\\*{2,}', '', text)
    text = text.replace('`', '')
    return text

def _markdown_link_tokens(text):
    """Токены-кандидаты, которые стояли ВНУТРИ markdown-ссылок в исходном тексте.

    Это латинские названия кейсов (KOLBASENKO, DETOX, MARKET), а не промокоды.
    Они попадают под шаблон CODE_CANDIDATE, но отсеиваются перед контекстом.
    """
    tokens = set()
    for m in re.finditer('\\[([^\\]\\[]+)\\]\\((https?://[^)\\s]+)\\)', text):
        link_text = m.group(1)
        for token in CODE_CANDIDATE_PATTERN.finditer(link_text):
            tokens.add(token.group(1).upper())
    return tokens
CODE_CANDIDATE_PATTERN = re.compile('(?<![\\w*?#%_])((?=[A-Z0-9?*#%_]{4,12}(?![\\w*?#%_]))[A-Z0-9?*#%_]*[A-Z0-9][A-Z0-9?*#%_]*)(?![\\w*?#%_])')
CHAT_CODE_MASK = re.compile('(?<![\\w*?#%_])([0-9A-F?*#%_]{10})(?![\\w*?#%_])', re.IGNORECASE)
_URL_SPAN = re.compile('(?:https?://|www\\.|t\\.me/)\\S+', re.IGNORECASE)

def extract_mask_codes(text):
    """Промокоды по маске чата. Возвращает список без дублей, в порядке текста.

    Требования к коду: ровно 10 символов, есть цифра И есть буква A-F.
    Одни цифры - это чей-то id или номер телефона, одни буквы - слово.
    Код внутри ссылки не берём: проверено, это единственный источник ложных
    срабатываний в живом чате.

    С октября 2026 админы жабки кидают в чат таблицы с кодами, включая коды
    с заглушками _ и ?, плюс подсказки в том же сообщении. Заглушки теперь ловим,
    расшифровываем по подсказкам из того же поста.
    """
    if not text:
        return []
    normalized = strip_markdown(text)
    url_spans = [m.span() for m in _URL_SPAN.finditer(normalized)]
    found = []
    for m in CHAT_CODE_MASK.finditer(normalized):
        start, end = m.span(1)
        if any((a <= start and end <= b for a, b in url_spans)):
            continue
        code = m.group(1).upper()
        if not re.search('\\d', code):
            continue
        if not re.search('[A-F]', code):
            continue
        if code not in found:
            found.append(code)
    return found
JUNK_CODES = {'https', 'http', 'www', 'jabka', 'case', 'cases', 'payment', 'profile', 'vip', 'cs2', 'wiki', 'weapon', 'utm', 'source', 'telegram', 'medium', 'smm', 'campaign', 'com', 'skin', 'promo', 'free', 'discount', 'bonus', 'time', 'gift', 'gifts', 'drop', 'skins', 'secretcode', 'route', 'sec', 'button', 'click', 'action'}

def is_real_code(code):
    """Отсеивает слова из ссылок и мусор, оставляя настоящие промокоды.

    Настоящие коды бывают трёх видов:
      - с цифрами и буквами: 2356C77FBA, F203A3*C63, 9305*E5A33
      - со спецсимволом:      5A981979?C, COM3B*CK
      - слова-капсом:         SPOKOYNOST, LAVKAM9, TORCASH, BA*KON
    Отсеиваем: только цифры (номера ID), только строчные слова из url, мусорные
    пути ссылок (case, detox, poor-t, utm_source).
    """
    if not code or len(code) < 4:
        return False
    low = code.lower()
    if low in JUNK_CODES:
        return False
    if low.startswith('utm') or low.startswith('https'):
        return False
    if re.search('\\d', code) or re.search('[*?#%_]', code):
        if code.isdigit() and len(code) > 8:
            return False
        return True
    if code.isalpha() and code.isupper() and (4 <= len(code) <= 12):
        return True
    return False

def extract_codes_from_text(text):
    """Все промокоды из текста поста, в порядке появления, без дублей.

    Нормализует markdown, поэтому ловит коды, завёрнутые в ** или `.
    """
    if not text:
        return []
    normalized = strip_markdown(text)
    found = []
    for m in CODE_CANDIDATE_PATTERN.finditer(normalized):
        code = m.group(1).upper()
        if is_real_code(code) and code not in found:
            found.append(code)
    return found
CONTEXT_MARKERS = re.compile('промо|от\\s*\\d|депа|депу|деп\\b|за\\s+неделю|за\\s+\\d+\\s+дн|[🔑🟢👉➡️🎁🎟️]|\\|\\s*\\**\\s*[*?_]\\s*=|в\\s+комментари')

def _line_has_code_context(normalized_text, code):
    """True, если строка с кодом содержит признак промо (см. CONTEXT_MARKERS).

    Требовать контекст - единственный надёжный способ не тащить в группу номера
    ID и названия скинов из обычных постов канала.
    """
    line = find_code_line(normalized_text, code)
    if line is None:
        return False
    return bool(CONTEXT_MARKERS.search(line))

def extract_promo_codes(text, trusted_codes=()):
    """Промокоды с проверкой контекста. Основная точка входа для сканеров.

    trusted_codes - коды, которым можно верить без контекста (например, найденные
    шаблоном MD-... или уже подтверждённые ранее). Это нужно, чтобы не отсеять
    код из строки, где он стоит один: посты вида "🔑 `2356C77FBA`" держатся на
    эмодзи-маркере, а он в контексте есть.
    """
    if not text:
        return []
    normalized = strip_markdown(text)
    trusted = {c.upper() for c in trusted_codes}
    out = []
    for code in extract_codes_from_text(text):
        if code in trusted or _line_has_code_context(normalized, code):
            out.append(code)
    return out
LINK_PATTERN = re.compile('(?:https?://)?(?:www\\.)?magic-drop\\.win/case/[-\\w]+', re.IGNORECASE)
DAST_LINK_PATTERN = re.compile('(?:https?://)?(?:www\\.)?(?:dast\\.skin|dastskin\\.cc)', re.IGNORECASE)
UPGRADER_PATTERN = re.compile('(?:https?://)?(?:[\\w\\-]+\\.)?upgrader\\.[a-z]{2,}/(\\w+)', re.IGNORECASE)
CASER_PATTERN = re.compile('(?:https?://)?(?:[\\w\\-]+\\.)?caser\\.[a-z]{2,}/(\\w+)', re.IGNORECASE)
RAFFLES_UPGRADER_PATTERN = re.compile('(?:https?://)?(?:[\\w\\-]+\\.)*upgrader\\.[a-z]{2,}/r/([A-Z0-9]+)', re.IGNORECASE)
RAFFLES_UPGRADER_USERNAME_PATTERN = re.compile('(?:https?://)?raffles\\.upgrader\\.[a-z]{2,}/(?!r/)([a-z0-9_]{3,})', re.IGNORECASE)
RAFFLES_CASER_PATTERN = re.compile('(?:https?://)?raffles\\.caser\\.[a-z]{2,}/([\\w\\-]+)', re.IGNORECASE)
ANY_UPGRADER_RAFFLE = re.compile('(?:https?://)?(?:[\\w\\-]+\\.)*upgrader\\.[a-z]{2,}/r/[A-Z0-9]+', re.IGNORECASE)
ANY_UPGRADER_RAFFLE_USERNAME = re.compile('(?:https?://)?raffles\\.upgrader\\.[a-z]{2,}/[a-z0-9_]+', re.IGNORECASE)
ANY_CASER_RAFFLE = re.compile('(?:https?://)?raffles\\.caser\\.[a-z]{2,}/[\\w\\-]+', re.IGNORECASE)
TWITCH_TOKEN = os.environ['TWITCH_TOKEN']
TG_API_ID = int(os.environ['TG_API_ID'])
TG_API_HASH = os.environ['TG_API_HASH']
TG_FORWARD_GROUP = int(os.environ['TG_FORWARD_GROUP'])
VK_TOKEN = os.environ['VK_TOKEN']
VK_API_VERSION = '5.131'
PORT = 0
COMMENTS_SESSION = None
COMMENTS_PROXY = None
JABKA_CHAT_ID = 2007494177
JABKA_CHAT_USERNAME = 'jabkaskinchat'
JABKA_CHANNEL_USERNAME = 'jabka_skin'
JABKA_CHANNEL_ID = 2082305863
JABKA_VK_DOMAIN = 'jabka.skin'
TEST_CHANNEL_USERNAME = 'givalox'

def is_jabka_source(chat_username, chat_id):
    """True для канала и чата жабки в Telegram (по username или по id).

    Также true для тестового канала givalox, чтобы можно было прогонять через него
    полноценные тесты case-time с подсказками в комментариях.
    """
    cu = (chat_username or '').lower()
    if cu in (JABKA_CHANNEL_USERNAME, JABKA_CHAT_USERNAME, TEST_CHANNEL_USERNAME):
        return True
    key = _chat_key(chat_id)
    return key in (_chat_key(JABKA_CHANNEL_ID), _chat_key(JABKA_CHAT_ID))
SEEN_PROMOS = set()
LAST_VK_POSTS = {}

def load_seen_promos_from_db():
    global SEEN_PROMOS
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT DISTINCT code FROM promos')
        SEEN_PROMOS = set((row[0] for row in c.fetchall()))
        conn.close()
    except:
        pass

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS promos\n                 (id INTEGER PRIMARY KEY, code TEXT, channel TEXT, platform TEXT, timestamp TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS channels\n                 (id INTEGER PRIMARY KEY, name TEXT, platform TEXT)')
    c.execute("CREATE TABLE IF NOT EXISTS raffles\n                 (id INTEGER PRIMARY KEY,\n                  channel TEXT,\n                  site TEXT,\n                  raffle_url TEXT,\n                  raffle_id TEXT,\n                  first_seen TEXT,\n                  last_seen TEXT,\n                  message_count INTEGER DEFAULT 1,\n                  status TEXT DEFAULT 'active')")
    c.execute('CREATE TABLE IF NOT EXISTS telegram_channels\n                 (id INTEGER PRIMARY KEY,\n                  channel_username TEXT UNIQUE,\n                  streamer_name TEXT,\n                  site TEXT)')
    c.execute('SELECT COUNT(*) FROM channels')
    if c.fetchone()[0] == 0:
        default_twitch = ['1centner1', 'autolafa', 'azrael_the_legend', 'bandurka', 'bib4ek', 'neclar1s1337']
        for ch in default_twitch:
            c.execute('INSERT INTO channels (name, platform) VALUES (?, ?)', (ch, 'TWITCH'))
        c.execute('INSERT INTO channels (name, platform) VALUES (?, ?)', ('jabka.skin', 'VK'))
    conn.commit()
    conn.close()

def get_channels(platform):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT name FROM channels WHERE platform=?', (platform,))
    res = [row[0] for row in c.fetchall()]
    conn.close()
    return res

def normalize_telegram_channel(value):
    """Приводит ввод канала к единому виду для хранения и сравнения.

    Принимает @username, t.me/username, https://t.me/username и числовые ID.
    Ссылки на конкретный пост тоже поддерживаются: идентификатор поста
    отбрасывается, потому что подписка всегда относится ко всему каналу.
    """
    value = str(value or '').strip()
    if not value:
        return ''
    value = re.sub('^https?://(?:www\\.)?(?:t\\.me|telegram\\.me)/', '', value, flags=re.IGNORECASE)
    value = re.sub('^(?:t\\.me|telegram\\.me)/', '', value, flags=re.IGNORECASE)
    value = value.split('?', 1)[0].split('#', 1)[0].strip().strip('/')
    if '/' in value and (not re.fullmatch('-?\\d+', value)):
        value = value.split('/', 1)[0]
    return value.lstrip('@').strip().lower()

def telegram_channel_keys(value):
    """Все допустимые представления одного Telegram-канала."""
    raw = str(value or '').strip().lower()
    key = normalize_telegram_channel(raw)
    keys = {raw, key, f'@{key}', f'https://t.me/{key}'} if key else set()
    if key.startswith('-100'):
        keys.add(key[4:])
    elif key.isdigit():
        keys.add(f'-100{key}')
    return {k for k in keys if k}

def add_channel_to_db(name, platform):
    if platform == 'TELEGRAM':
        name = normalize_telegram_channel(name)
        if not name:
            return False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT name FROM channels WHERE name=? AND platform=?', (name, platform))
    if not c.fetchone():
        c.execute('INSERT INTO channels (name, platform) VALUES (?, ?)', (name, platform))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def delete_channel_from_db(name, platform):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM channels WHERE name=? AND platform=?', (name, platform))
    conn.commit()
    conn.close()

def save_promo(code, channel, platform, timestamp):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO promos (code, channel, platform, timestamp) VALUES (?, ?, ?, ?)', (code, channel, platform, timestamp))
    conn.commit()
    conn.close()

def extract_betboom_raffles(text):
    """Ссылки на колеса BetBoom; query/fragment не меняют идентификатор."""
    pattern = '(?<![\\w./@-])(?:https?://)?(?:www\\.)?betboom\\.ru/freestream/([A-Za-z0-9_-]+)(?=$|[\\s/?#<>"\\x27\\])}.,!:;*])'
    found = {}
    for match in re.finditer(pattern, text or '', re.IGNORECASE):
        code = match.group(1)
        found.setdefault('betboom_' + code, 'https://betboom.ru/freestream/' + code)
    return list(found.items())

def save_betboom_raffle(channel, raffle_url, raffle_id):
    """КД 30 минут от первого уведомления, отдельно для канала и колеса.

    Повторы обновляют last_seen, но не first_seen, поэтому не продлевают КД.
    BEGIN IMMEDIATE защищает от одновременных повторов из разных обработчиков.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute('BEGIN IMMEDIATE')
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        row = conn.execute("SELECT id, first_seen, message_count FROM raffles WHERE channel=? AND site='betboom' AND raffle_id=? ORDER BY id DESC LIMIT 1", (channel, raffle_id)).fetchone()
        notify = row is None
        if row:
            try:
                first = time.mktime(time.strptime(row[1], '%Y-%m-%d %H:%M:%S'))
                now = time.mktime(time.strptime(timestamp, '%Y-%m-%d %H:%M:%S'))
                notify = now - first >= 30 * 60 or now < first
            except (ValueError, TypeError, OverflowError):
                notify = True
        if notify:
            conn.execute('INSERT INTO raffles (channel,site,raffle_url,raffle_id,first_seen,last_seen,message_count,status) VALUES (?,?,?,?,?,?,?,?)', (channel, 'betboom', raffle_url, raffle_id, timestamp, timestamp, 1, 'active'))
        else:
            conn.execute("UPDATE raffles SET last_seen=?, message_count=?, status='active' WHERE id=?", (timestamp, (row[2] or 0) + 1, row[0]))
        conn.commit()
        return notify
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def save_raffle(channel, site, raffle_url, raffle_id):
    """Сохраняет информацию о розыгрыше в БД.

    Возвращает True если это новое упоминание розыгрыша (нужно отправить уведомление).

    Логика:
    - Первое упоминание: сохраняем, возвращаем True
    - Повторное в течение 4 часов: увеличиваем счетчик, возвращаем False
    - Повторное после 4+ часов: новый розыгрыш, возвращаем True

    КД 4 часа работает отдельно для каждого сайта (channel + site + raffle_id).
    Пример: del1ght может постить Caser и Upgrader розыгрыши независимо.
    """
    if site == 'betboom':
        return save_betboom_raffle(channel, raffle_url, raffle_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    c.execute('SELECT id, last_seen, message_count FROM raffles WHERE channel=? AND site=? AND raffle_id=? ORDER BY id DESC LIMIT 1', (channel, site, raffle_id))
    existing = c.fetchone()
    should_notify = False
    if existing:
        last_seen_str = existing[1]
        message_count = existing[2]
        try:
            last_seen = time.mktime(time.strptime(last_seen_str, '%Y-%m-%d %H:%M:%S'))
            current_time = time.mktime(time.strptime(timestamp, '%Y-%m-%d %H:%M:%S'))
            hours_diff = (current_time - last_seen) / 3600
            if hours_diff >= 4:
                should_notify = True
                c.execute('INSERT INTO raffles (channel, site, raffle_url, raffle_id, first_seen, last_seen, message_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (channel, site, raffle_url, raffle_id, timestamp, timestamp, 1, 'active'))
            else:
                c.execute("UPDATE raffles SET last_seen=?, message_count=?, status='active' WHERE id=?", (timestamp, message_count + 1, existing[0]))
                should_notify = False
        except:
            c.execute("UPDATE raffles SET last_seen=?, status='active' WHERE id=?", (timestamp, existing[0]))
            should_notify = False
    else:
        should_notify = True
        c.execute('INSERT INTO raffles (channel, site, raffle_url, raffle_id, first_seen, last_seen, message_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (channel, site, raffle_url, raffle_id, timestamp, timestamp, 1, 'active'))
    conn.commit()
    conn.close()
    return should_notify
    conn.commit()
    conn.close()
    return should_notify

def get_active_raffles(channel):
    """Получает активные розыгрыши для канала"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT raffle_id, raffle_url, site FROM raffles WHERE channel=? AND status='active'", (channel,))
    res = c.fetchall()
    conn.close()
    return res

def mark_raffle_completed(channel, raffle_id):
    """Помечает розыгрыш как завершенный"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE raffles SET status='completed' WHERE channel=? AND raffle_id=?", (channel, raffle_id))
    conn.commit()
    conn.close()
    c = conn.cursor()
    c.execute('INSERT INTO promos (code, channel, platform, timestamp) VALUES (?, ?, ?, ?)', (code, channel, platform, timestamp))
    conn.commit()
    conn.close()

def get_all_promos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT timestamp, platform, channel, code FROM promos ORDER BY id DESC')
    res = c.fetchall()
    conn.close()
    return res
HINT_IN_LINE_PATTERN = re.compile('\\|\\s*\\**\\s*([*?_])\\s*=\\s*\\**\\s*([A-Z0-9]{1,2})\\b', re.IGNORECASE)
HINT_STANDALONE_PATTERN = re.compile('^[\\s🔽]*([*?_])\\s*=\\s*([A-Z0-9]{1,2})\\b', re.IGNORECASE)
HINT_IN_COMMENTS_PATTERN = re.compile('[*?_]\\s*=\\s*в\\s*комментари', re.IGNORECASE)

def find_code_line(text, code):
    """Возвращает строку поста, в которой стоит ИМЕННО ЭТОТ код (или None).

    Сравнение идёт по границам токена, а НЕ по подстроке. Это принципиально:
    код "XYZ*12" является подстрокой кода "WXYZ*12", и при поиске подстрокой
    короткий код находил чужую строку с чужой подсказкой - и расшифровывался
    НЕПРАВИЛЬНО, но со статусом "решён". Это хуже нерасшифрованного кода,
    потому что выглядит проверенным.
    """
    normalized = strip_markdown(text)
    for line in normalized.split('\n'):
        for token in _tokens_in_line(line):
            if token.upper() == code.upper():
                return line
    return None
_LINE_TOKEN_PATTERN = re.compile('(?<![\\w*?#%_])((?=[A-Z0-9?*#%_]{4,12}(?![\\w*?#%_]))[A-Z0-9?*#%_]*[A-Z0-9][A-Z0-9?*#%_]*)(?![\\w*?#%_])', re.IGNORECASE)

def _tokens_in_line(line):
    """Все токены-кандидаты в строке, по границам, с сохранением регистра."""
    return [m.group(1) for m in _LINE_TOKEN_PATTERN.finditer(line or '')]

def _hint_line_for(text, code):
    """Возвращает (строка_с_подсказкой, признак_комментариев) для кода.

    Подсказка стоит либо в ТОЙ ЖЕ строке, что и код, либо отдельной строкой
    рядом (в живых постах жабки встречаются оба варианта):
        "🟢 F203A3*C63 | * = B — Детокс от 30$"      (в строке кода)
        "🟢 2356C77FBA
? = в комментариях 🔽"        (отдельной строкой)
        "F29FCA?742    Тюльпан ...
Подсказка для активации промо с "?"
? = 1"
    С октября 2026 жабка стала давать подсказки в свёрнутых цитатах по всему
    посту ("Подсказка для активации промо с "_"" → "_ = 0"). Если в соседних
    2 строках ничего нет, ищем по всему посту — но только если в промо-коде
    есть заглушка, иначе поиск вхолостую.
    """
    normalized = strip_markdown(text)
    lines = normalized.split('\n')
    own_line = None
    for line in lines:
        if any((t.upper() == code.upper() for t in _tokens_in_line(line))):
            own_line = line
            break
    if own_line is None:
        return (None, False)
    if HINT_IN_LINE_PATTERN.search(own_line):
        return (own_line, False)
    if HINT_IN_COMMENTS_PATTERN.search(own_line):
        return (own_line, True)
    idx = lines.index(own_line)
    for cand in lines[idx + 1:idx + 3] + lines[max(0, idx - 2):idx][::-1]:
        if not cand.strip():
            continue
        if _tokens_in_line(cand):
            break
        if HINT_IN_LINE_PATTERN.search(cand):
            return (cand, False)
        if HINT_IN_COMMENTS_PATTERN.search(cand):
            return (cand, True)
    if any((ch in code for ch in '?*_')):
        for i, line in enumerate(lines):
            if 'подсказка' in line.lower() and 'промо' in line.lower():
                for next_line in lines[i + 1:i + 3]:
                    m = HINT_IN_LINE_PATTERN.search(next_line) or HINT_STANDALONE_PATTERN.search(next_line)
                    if m and m.group(1) in code:
                        return (next_line, False)
    return (own_line, False)

def decrypt_jabka_code(text, code):
    """Расшифровывает промокод с заглушками '*'/'?'/'_' по подсказке из того же поста.

    Возвращает расшифрованный код. Если подсказки нет или заглушки в коде не
    совпадают с подсказкой - возвращает код БЕЗ изменений. Молча выдумывать
    букву нельзя: неверный код хуже, чем нерасшифрованный.
    """
    if '*' not in code and '?' not in code and ('_' not in code):
        return code
    line, in_comments = _hint_line_for(text, code)
    if line is None or in_comments:
        return code
    match = HINT_IN_LINE_PATTERN.search(line) or HINT_STANDALONE_PATTERN.search(line)
    if not match:
        return code
    placeholder = match.group(1)
    value = match.group(2).upper()
    if placeholder not in code:
        return code
    return code.replace(placeholder, value)

def has_unresolved_hint(text, code):
    """True, если код с заглушкой расшифровать по посту нельзя.

    Это либо явное "? = в комментариях", либо подсказки в посте просто нет.
    Такие коды показываем как есть и НЕ запоминаем - когда подсказку опубликуют,
    алерт должен сработать ещё раз.
    """
    if '?' not in code and '*' not in code and ('_' not in code):
        return False
    if decrypt_jabka_code(text, code) != code:
        return False
    return True

def is_hint_promised_in_comments(text, code):
    """True, если в посте прямо написано, что подсказка будет в комментариях."""
    line, in_comments = _hint_line_for(text, code)
    if in_comments:
        return True
    normalized = strip_markdown(text)
    return bool(HINT_IN_COMMENTS_PATTERN.search(normalized))

def parse_hint_from_comment(comment_text):
    """Ищет в тексте комментария подсказку вида "? = 3" / "?=B" / "* = 4" / "_ = 0".

    Возвращает список пар (символ_заглушки, символ_подстановки), например
    [('?', '3')] или [('*', 'B')]. Пустой список - подсказки нет.

    В комментарии подсказку повторяют много раз ("? = 3" от разных людей),
    поэтому возвращаем ВСЕ совпадения: вызывающий код сам решает, какое брать.
    """
    if not comment_text:
        return []
    pattern = re.compile('([?*_])\\s*[=-]\\s*([A-Za-z0-9])(?![A-Za-z0-9])')
    found = []
    for m in pattern.finditer(comment_text):
        placeholder, value = (m.group(1), m.group(2).upper())
        found.append((placeholder, value))
    return found

def apply_hint_to_code(code, placeholder, value):
    """Подставляет символ подсказки вместо ВСЕХ заглушек указанного типа.

    Длина кода не меняется: заглушка заменяется ровно одним символом.
    Если заглушки такого типа в коде нет - возвращает код без изменений.
    """
    if placeholder not in code:
        return code
    return code.replace(placeholder, value)
HINT_MIN_VOTES = 2
COMMENT_POLL_INTERVAL = 3
COMMENT_POLL_TIMEOUT = 10 * 60
COMMENT_WATCHERS = set()
COMMENT_WAKEUPS = {}

def _chat_key(chat_id):
    """Одинаковый ключ для '2007494177', '-1002007494177' и '-2007494177'."""
    s = str(chat_id).lstrip('-')
    if s.startswith('100'):
        s = s[3:]
    return s

def _extract_case_info(text, code):
    """Извлекает название кейса и ссылку из строки с промокодом.

    Возвращает (case_name, case_link). Ищет строку, где встречается код,
    и парсит название (текст после кода до 'от'/'—'/' - '/перед '(')
    и ссылку jabka.to/case/...
    """
    case_name = ''
    case_link = ''
    for line in text.split('\n'):
        if code not in line:
            continue
        parts = line.split(code)
        if len(parts) > 1:
            tail = parts[1].strip()
            tail = tail.replace('**', '').replace('__', '').replace('`', '')
            if '(https://' in tail:
                tail = tail.split('(https://')[0].strip()
            for sep in [' от ', '—', ' - ']:
                if sep in tail:
                    case_name = tail.split(sep)[0].strip()
                    break
            if not case_name:
                case_name = tail.strip()
        if 'https://jabka.to/case/' in line:
            link_start = line.find('https://jabka.to/case/')
            link_end = len(line)
            for end_char in [')', ' ', '?utm']:
                pos = line.find(end_char, link_start)
                if pos != -1:
                    link_end = min(link_end, pos)
            case_link = line[link_start:link_end].strip()
        break
    return (case_name, case_link)
CASE_URL_RE = re.compile('https?://(?:www\\.)?(?:jabka\\.to|magic-drop\\.win)/case/[^\\s)]+', re.IGNORECASE)

def rich_message_content(message):
    """Read visible rich-message blocks, captions and buttons, excluding media metadata."""
    rich = getattr(message, 'rich_message', None)
    if rich is None:
        return '', {}
    data = rich.to_dict()
    labels = {}

    def walk(node):
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return '\n'.join(filter(None, (walk(item) for item in node)))
        if not isinstance(node, dict):
            return ''
        kind = node.get('_', '')
        if kind == 'TextConcat':
            return ''.join(walk(item) for item in node.get('texts', []))
        if kind == 'TextCustomEmoji':
            return node.get('alt', '')
        text = walk(node.get('text'))
        url = node.get('url')
        if kind == 'PageButton':
            url = (node.get('type') or {}).get('url')
        if url and isinstance(url, str):
            if text:
                labels.setdefault(url, text)
            text = text + ' (' + url + ')' if text else url
        parts = [text]
        for key in ('caption', 'credit', 'blocks', 'items', 'buttons', 'title', 'author', 'rows', 'cells', 'list', 'details'):
            if key in node:
                parts.append(walk(node[key]))
        return '\n'.join(part for part in parts if part)

    return walk(data.get('blocks', [])), labels


def telegram_message_text(message):
    ordinary = getattr(message, 'message', None) or ''
    rich, _ = rich_message_content(message)
    return '\n'.join(part for part in (ordinary, rich) if part)


def inline_entity_links(message):
    """Текст сообщения, где после каждой вшитой ссылки стоит её адрес в скобках.

    Смещения entities считаются в UTF-16 (эмодзи занимают 2 единицы), поэтому
    работаем через add_surrogate/del_surrogate, иначе адрес встанет не туда.
    Возвращает (текст, {адрес: подпись_ссылки}).
    """
    from telethon import helpers
    from telethon.tl.types import MessageEntityTextUrl
    text = getattr(message, 'message', None) or ''
    ents = [e for e in getattr(message, 'entities', None) or [] if isinstance(e, MessageEntityTextUrl) and e.url]
    rich, rich_labels = rich_message_content(message)
    if not ents:
        return ('\n'.join(part for part in (text, rich) if part), rich_labels)
    s = helpers.add_surrogate(text)
    labels = {}
    for e in sorted(ents, key=lambda e: e.offset + e.length, reverse=True):
        end = e.offset + e.length
        label = helpers.del_surrogate(s[e.offset:end]).strip()
        if label == e.url.strip():
            continue
        if label and (not labels.get(e.url)):
            labels[e.url] = label
        s = s[:end] + f' ({e.url})' + s[end:]
    labels.update(rich_labels)
    return ('\n'.join(part for part in (helpers.del_surrogate(s), rich) if part), labels)

def find_case_link_for_code(text, code):
    """Ссылка на кейс для кода: (адрес, позиция) или None.

    Ищем в строке кода ближайшую ссылку на кейс того же сайта: сначала ту, что
    стоит ДО кода ("Ёлку (url) от 25$ по коду ELKA26"), иначе первую после
    ("FA37851CFB — Доходяга (url)"). Если в строке ссылок нет, но во всём посте
    ссылка на кейс одна - берём её ("Вводи код на странице Telegram Case").
    """
    site = 'magic-drop' if code.upper().startswith('MD-') else 'jabka'
    for line in text.split('\n'):
        idx = line.upper().find(code.upper())
        if idx < 0:
            continue
        links = [(m.start(), m.group()) for m in CASE_URL_RE.finditer(line) if site in m.group().lower()]
        if links:
            before = [l for l in links if l[0] < idx]
            return before[-1][1] if before else links[0][1]
        break
    unique = {m.group().split('?')[0] for m in CASE_URL_RE.finditer(text) if site in m.group().lower()}
    if len(unique) == 1:
        return next((m.group() for m in CASE_URL_RE.finditer(text) if site in m.group().lower()))
    return None

def case_label_for_link(link, labels):
    """Название кейса для ссылки: подпись вшитой ссылки, иначе по слагу."""
    label = (labels or {}).get(link, '')
    label = re.sub('[^\\w\\s.\\-«»"]', '', label).strip(' «»"-')
    if len(label) >= 2:
        return label[:1].upper() + label[1:]
    m = re.search('/case/([^?&\\s)/]+)', link)
    if not m:
        return None
    slug = m.group(1)
    for name, s in NAME_TO_SLUG.items():
        if s == slug:
            return name.capitalize()
    return slug.replace('_', ' ').replace('-', ' ').capitalize()

def register_comment_wakeup(chat_id, event):
    COMMENT_WAKEUPS.setdefault(_chat_key(chat_id), set()).add(event)

def unregister_comment_wakeup(chat_id, event):
    key = _chat_key(chat_id)
    bucket = COMMENT_WAKEUPS.get(key)
    if not bucket:
        return
    bucket.discard(event)
    if not bucket:
        COMMENT_WAKEUPS.pop(key, None)

def wake_comment_watchers(chat_id):
    """Будит слежения, ждущие комментарии в этом чате. Возвращает их число."""
    bucket = COMMENT_WAKEUPS.get(_chat_key(chat_id))
    if not bucket:
        return 0
    for ev in list(bucket):
        ev.set()
    return len(bucket)

async def find_comments_anchor(client, channel_entity, post_id):
    """Находит чат обсуждения и id сообщения-обсуждения для поста.

    Возвращает (discussion_chat, discussion_msg_id) либо (None, None), если
    комментариев под постом нет или аккаунт их не видит.

    Чат берём ПРЯМО ИЗ ОТВЕТА запроса, а не через get_entity(linked_chat_id):
    у аккаунта, не состоящего в чате, второй способ падает с ChannelPrivateError,
    хотя первый работает - сервер отдаёт чат вместе с id обсуждения.
    """
    from telethon.tl.functions.messages import GetDiscussionMessageRequest
    try:
        disc = await client(GetDiscussionMessageRequest(peer=channel_entity, msg_id=post_id))
    except Exception:
        return (None, None)
    if not disc.chats or not disc.messages:
        return (None, None)
    return (disc.chats[0], disc.messages[0].id)

def _is_channel_authored(message):
    """True, если комментарий написан ОТ ИМЕНИ КАНАЛА, а не обычным человеком.

    Проверено на живых подсказках (посты 2633 и 2618 @jabka_skin): подсказку
    публикует сам канал - get_sender() отдаёт None, а в post_author стоит
    подпись ("Хикуля"). У обычных людей автор-пользователь заполнен.
    """
    if getattr(message, 'post_author', None):
        return True
    from telethon.tl.types import PeerChannel
    from_id = getattr(message, 'from_id', None)
    if from_id is None:
        return True
    return isinstance(from_id, PeerChannel)

async def collect_hints_from_comments(client, disc_chat, disc_msg_id, limit=100, only_channel=True):
    """Читает комментарии и считает голоса за каждую подсказку.

    Возвращает словарь {(заглушка, символ): количество_повторов}.

    only_channel НЕ РАБОТАЕТ: _is_channel_authored возвращает True для ВСЕХ
    с post_author, а не только для канала. Все админы пишут с подписью.
    Поэтому фильтр отключён (only_channel игнорируется) - берём подсказки
    от всех, а защита от шутников - через HINT_MIN_VOTES >= 2.
    """
    votes = {}
    try:
        async for comment in client.iter_messages(disc_chat, reply_to=disc_msg_id, limit=limit):
            for placeholder, value in parse_hint_from_comment(comment.text or ''):
                key = (placeholder, value)
                votes[key] = votes.get(key, 0) + 1
    except Exception as e:
        return (votes, f'{type(e).__name__}: {e}')
    return (votes, None)

def pick_trusted_hint(votes):
    """Выбирает подсказку, набравшую достаточно голосов.

    Возвращает (заглушка, символ) или None. Если лидируют две разные подсказки
    с одинаковым числом голосов - не верим ни одной (лучше оставить код
    нерасшифрованным, чем опубликовать выдуманный).
    """
    trusted = [(k, v) for k, v in votes.items() if v >= HINT_MIN_VOTES]
    if not trusted:
        return None
    trusted.sort(key=lambda kv: kv[1], reverse=True)
    if len(trusted) > 1:
        (ph1, val1), n1 = trusted[0]
        (ph2, val2), n2 = trusted[1]
        if ph1 == ph2 and n1 == n2:
            return None
    return trusted[0][0]

def trusted_hints(votes):
    """Все подсказки, которым можно верить - ПО ОДНОЙ на каждую заглушку.

    pick_trusted_hint отдаёт только лидера, и этого мало: в одном посте бывают
    коды с РАЗНЫМИ заглушками ('*' и '?'), а подсказки к ним канал публикует
    по отдельности. Здесь для каждой заглушки свой победитель. Если за одну
    заглушку одинаково голосуют два разных символа - заглушка пропускается:
    лучше оставить код нерасшифрованным, чем выдумать букву.
    """
    by_ph = {}
    for (ph, val), n in votes.items():
        by_ph.setdefault(ph, []).append((val, n))
    out = []
    for ph, items in by_ph.items():
        items.sort(key=lambda kv: kv[1], reverse=True)
        if items[0][1] < HINT_MIN_VOTES:
            continue
        if len(items) > 1 and items[0][1] == items[1][1]:
            continue
        out.append((ph, items[0][0]))
    return out

async def watch_comments_for_hints(comments_client, channel_entity, post_id, unresolved_codes, msg_queue, forward_func):
    """Следит за комментариями поста и, когда появится подсказка, добивает коды.

    unresolved_codes - коды из этого поста, которые не удалось расшифровать.
    forward_func     - корутина, отправляющая досыл с расшифрованными кодами.
    """
    started = time.time()
    anchor_logged = False
    first_pass = True
    wakeup = asyncio.Event()
    wakeup_chat = None
    pending = list(unresolved_codes)
    used_hints = set()
    try:
        while time.time() - started < COMMENT_POLL_TIMEOUT and pending:
            if not first_pass:
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=COMMENT_POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                wakeup.clear()
            first_pass = False
            disc_chat, disc_msg_id = await find_comments_anchor(comments_client, channel_entity, post_id)
            if disc_chat is None:
                continue
            if not anchor_logged:
                msg_queue.put(f'[TELEGRAM] Слежу за комментариями поста {post_id}\n')
                anchor_logged = True
            if wakeup_chat is None:
                wakeup_chat = getattr(disc_chat, 'id', None)
                if wakeup_chat is not None:
                    register_comment_wakeup(wakeup_chat, wakeup)
            votes, err = await collect_hints_from_comments(comments_client, disc_chat, disc_msg_id)
            if err:
                msg_queue.put(f'[TELEGRAM] Ошибка чтения комментариев: {err}\n')
                continue
            if not votes:
                continue
            for hint in trusted_hints(votes):
                if hint in used_hints:
                    continue
                placeholder, value = hint
                resolved = []
                for code in list(pending):
                    new_code = apply_hint_to_code(code, placeholder, value)
                    if new_code != code:
                        resolved.append((code, new_code))
                        pending.remove(code)
                if not resolved:
                    continue
                used_hints.add(hint)
                msg_queue.put(f'[TELEGRAM] Найдена подсказка в комментариях: {placeholder} = {value} (голосов: {votes[hint]})\n')
                try:
                    await forward_func(resolved, votes[hint])
                except Exception as e:
                    msg_queue.put(f'[ОШИБКА ДОСЫЛА]: {e}\n')
                    for old_code, _ in resolved:
                        if old_code not in pending:
                            pending.append(old_code)
                    used_hints.discard(hint)
            if not pending:
                return
        if pending:
            msg_queue.put(f"[TELEGRAM] Подсказку под постом {post_id} так и не дали (ждал {COMMENT_POLL_TIMEOUT // 3600} ч), нерасшифровано: {', '.join(pending)}\n")
    finally:
        if wakeup_chat is not None:
            unregister_comment_wakeup(wakeup_chat, wakeup)

def start_comment_watcher(comments_client, channel_entity, post_id, unresolved_codes, msg_queue, forward_func):
    """Запускает слежение фоновой задачей. Возвращает задачу или None."""
    if comments_client is None or not unresolved_codes:
        return None
    task = asyncio.create_task(watch_comments_for_hints(comments_client, channel_entity, post_id, unresolved_codes, msg_queue, forward_func))
    COMMENT_WATCHERS.add(task)
    task.add_done_callback(COMMENT_WATCHERS.discard)
    return task
NAME_TO_SLUG = {'доходяга': 'poor-t', 'лавочка': 'bench', 'детокс': 'detox', 'рыбалка': 'fishing', 'тюльпан': 'tulip', 'магия': 'magic', 'лужайка': 'lawn', 'домашний': 'comfort', 'колбасенко': 'kolbasenko', 'рынок': 'market', 'волна': 'wave', 'лимонад': 'lemonade', 'уточка': 'duckling', 'гнездо': 'nest', 'жара': 'heat', 'кокос': 'coconut', 'гаражный': 'garage', 'народная': 'stash', 'сто': 'sto', 'подработка': 'part-time', 'магнит': 'magnet'}

def resolve_case_for_code(text, code, code_to_link, short_codes, jabka_links):
    """Возвращает (название кейса, чистая ссылка) для конкретного промокода.

    Порядок источников:
      1. строка, где код стоит рядом со ссылкой;
      2. короткие коды из "SPOKOYNOST — промо на «Детокс»";
      3. строка вида "2356C77FBA — Доходяга от 1$" (в посте нет ссылок);
      4. словарь NAME_TO_SLUG по названию кейса.
    Всё сравнение идёт по НОРМАЛИЗОВАННОМУ тексту: иначе код, завёрнутый в
    ** или ` в оригинале, не найдётся в строке.
    """
    normalized = strip_markdown(text)
    case_name = None
    clean_link = None
    if code in code_to_link:
        link = code_to_link[code]
        case_name = extract_jabka_case_name(normalized, link)
        if case_name:
            clean_link = link.split('?')[0]
    elif code in short_codes:
        case_name = short_codes[code]
        for link in jabka_links:
            if case_name and case_name.replace(' ', '-').lower() in link.lower():
                clean_link = link.split('?')[0]
                break
    if not case_name:
        case_name = extract_case_from_code_line(text, code)
    if case_name and (not clean_link):
        slug = NAME_TO_SLUG.get(case_name.lower())
        if slug:
            clean_link = f'https://jabka.to/case/{slug}'
    return (case_name, clean_link)

def _case_name_from_line(text, code):
    """Название кейса из строки с кодом, когда прямой ссылки рядом нет.

    Понимает оба живых варианта:
        "➡️BA*KON | * = L  — промо на «Домашний» от 50$"
        "➡️SPOKOYNOST — промо на «Детокс» от 15$"
        "2356C77FBA — Доходяга от 1$ за неделю"
    Возвращает имя или None.
    """
    line = find_code_line(text, code)
    if not line:
        return None
    m = re.search('промо\\s+на\\s*[«"\\\']?([А-Яа-яЁёA-Za-z][^»"\\\'\\n(]*)', line, re.IGNORECASE)
    if m:
        name = m.group(1).strip().rstrip('«»"\'').strip()
        name = re.sub('\\s+(от|за)\\s+\\d.*$', '', name, flags=re.IGNORECASE).strip()
        if name and name.lower() not in ('промо', 'на', 'кейс'):
            return name
    m = re.search('[—\\-–]\\s*([А-ЯЁA-Z][А-Яа-яёЁA-Za-z]{2,20})(?=\\s*\\(|\\s+от\\s|\\s*$|\\s*[—\\-–])', line)
    if m:
        name = m.group(1).strip()
        if name.lower() not in ('промо', 'на', 'от', 'за'):
            return name
    return None

def extract_case_from_code_line(text, code):
    """Название кейса из строки "2356C77FBA — Доходяга от 1$ за неделю" (ссылки в посте нет)"""
    return _case_name_from_line(text, code)

def extract_jabka_case_name(text, link):
    """Извлекает название кейса Jabka из текста рядом со ссылкой"""
    import re
    patterns = ['промо на\\s*[«"\\\']([^»"\\\'\\n]+)[»"\\\']', '[—\\-]\\s*([А-Яа-яA-Za-z][А-Яа-яA-Za-z\\s]*?)\\s*\\(']
    lines = text.split('\n')
    for line in lines:
        if link in line or link.split('?')[0] in line:
            for pattern in patterns:
                match = re.search(pattern, line)
                if match:
                    case_name = match.group(1).strip()
                    if case_name and case_name.lower() not in ('промо', 'промо на', 'на'):
                        return case_name
    match = re.search('/case/([^?&\\s\\)]+)', link)
    if match:
        case_slug = match.group(1)
        slug_names = {'detox': 'Детокс', 'fishing': 'Рыбалка', 'poor-t': 'Доходяга', 'bench': 'Лавочка', 'tulip': 'Тюльпан', 'magic': 'Магия', 'lawn': 'Лужайка', 'comfort': 'Домашний'}
        return slug_names.get(case_slug, case_slug.capitalize())
    return None

def get_target_site(code):
    """Определяет сайт по формату промокода"""
    code_upper = code.upper()
    if code_upper.startswith('MD-'):
        return 'MAGIC DROP'
    elif len(code_upper) == 10:
        return 'JABKA'
    elif len(code_upper) == 9:
        has_digits = any((c.isdigit() for c in code_upper))
        has_letters = any((c.isalpha() for c in code_upper))
        if has_digits and has_letters:
            return 'DAST.SKIN'
        else:
            return 'JABKA'
    else:
        return 'JABKA'

def notify_promo(platform, channel, codes_list):
    if not codes_list:
        return
    try:
        target_site = get_target_site(codes_list[0])
        msg_text = f'Сайт: {target_site}\nКод: {codes_list[0]}' if len(codes_list) == 1 else f'Сайт: {target_site}\nНайдено кодов: {len(codes_list)}'
        notification.notify(title=f'🎯 ПРОМОКОД ({platform})!', message=f'Канал: {channel.upper()}\n{msg_text}', app_name='Promo Scanner', timeout=5)
    except Exception:
        pass

class TwitchScanner(commands.Bot):

    def __init__(self, msg_queue):
        channels = get_channels('TWITCH')
        super().__init__(token=TWITCH_TOKEN, prefix='!', initial_channels=[])
        self.msg_queue = msg_queue
        self.tg_client = None
        self.all_channels = channels
        self.total_channels = len(channels)
        self.connected_count = 0
        self.msg_queue.put(f'[TWITCH] Инициализация... Всего каналов: {len(channels)}\n')

    def refresh_sources(self):
        self.all_channels = sorted({name.strip().lower().lstrip('#') for name in get_channels('TWITCH')})

    async def event_ready(self):
        self.msg_queue.put(f'[TWITCH] IRC подключен. Начинаю подключение к каналам...\n')
        await self._connect_channels_gradually()

    async def _connect_channels_gradually(self):
        """Постепенное подключение к каналам с соблюдением rate limit"""
        batch_size = 18
        delay = 11
        total = len(self.all_channels)
        for i in range(0, total, batch_size):
            batch = self.all_channels[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total + batch_size - 1) // batch_size
            try:
                await self.join_channels(batch)
                self.connected_count += len(batch)
                self.msg_queue.put(f'[TWITCH] Подключено {self.connected_count}/{total} каналов (партия {batch_num}/{total_batches})\n')
            except Exception as e:
                self.msg_queue.put(f'[TWITCH ОШИБКА] Партия {batch_num}: {e}\n')
            if i + batch_size < total:
                await asyncio.sleep(delay)

    async def event_message(self, message):
        if message.echo:
            return
        text = message.content
        channel = message.channel.name
        md_matches = [m.group().upper() for m in PROMO_PATTERN.finditer(text)]
        other_codes = []
        matches = md_matches + other_codes
        raffles_upgrader_matches = RAFFLES_UPGRADER_PATTERN.findall(text)
        raffles_upgrader_username = RAFFLES_UPGRADER_USERNAME_PATTERN.findall(text)
        raffles_caser_matches = RAFFLES_CASER_PATTERN.findall(text)
        all_upgrader_raffles = list(raffles_upgrader_matches) + list(raffles_upgrader_username)
        if not all_upgrader_raffles:
            any_upgrader_r = ANY_UPGRADER_RAFFLE.findall(text)
            any_upgrader_u = ANY_UPGRADER_RAFFLE_USERNAME.findall(text)
            for link in any_upgrader_r:
                if '/r/' in link:
                    parts = link.split('/r/')
                    if len(parts) > 1:
                        code = parts[1].split('?')[0].split('&')[0].split('/')[0]
                        if code and code.replace('_', '').replace('-', '').isalnum():
                            all_upgrader_raffles.append(code)
            for link in any_upgrader_u:
                parts = link.split('/')
                if len(parts) >= 4:
                    username = parts[-1].split('?')[0].split('&')[0]
                    if username and username.replace('_', '').replace('-', '').isalnum() and (len(username) >= 3):
                        all_upgrader_raffles.append(username)
        if not raffles_caser_matches:
            any_caser = ANY_CASER_RAFFLE.findall(text)
            for link in any_caser:
                parts = link.split('/')
                if len(parts) >= 4:
                    code = parts[-1].split('?')[0].split('&')[0]
                    if code and code.replace('_', '').replace('-', '').isalnum():
                        raffles_caser_matches.append(code)
        new_raffles = []
        for raffle_code in all_upgrader_raffles:
            raffle_url = None
            for link in ANY_UPGRADER_RAFFLE.findall(text) + ANY_UPGRADER_RAFFLE_USERNAME.findall(text):
                if raffle_code in link:
                    raffle_url = link
                    break
            if not raffle_url:
                if raffle_code.isupper() and len(raffle_code) <= 10:
                    raffle_url = f'https://raffles.upgrader.best/r/{raffle_code}'
                else:
                    raffle_url = f'https://raffles.upgrader.best/{raffle_code}'
            raffle_id = f'upgrader_{raffle_code}'
            should_notify = save_raffle(channel, 'upgrader', raffle_url, raffle_id)
            if should_notify:
                new_raffles.append({'site': 'Upgrader', 'url': raffle_url})
        for raffle_username in raffles_caser_matches:
            raffle_url = None
            for link in ANY_CASER_RAFFLE.findall(text):
                if raffle_username in link:
                    raffle_url = link
                    break
            if not raffle_url:
                raffle_url = f'https://raffles.caser.one/{raffle_username}'
            raffle_id = f'caser_{raffle_username}'
            should_notify = save_raffle(channel, 'caser', raffle_url, raffle_id)
            if should_notify:
                new_raffles.append({'site': 'Caser', 'url': raffle_url})
        for raffle_id, raffle_url in extract_betboom_raffles(text):
            if save_raffle(channel, 'betboom', raffle_url, raffle_id):
                new_raffles.append({'site': 'BetBoom', 'url': raffle_url})
        if new_raffles:
            timestamp = time.strftime('%H:%M:%S')
            for raffle in new_raffles:
                self.msg_queue.put(f"🎁 [TWITCH | {timestamp}] РОЗЫГРЫШ: {channel.upper()}\n🎯 Сайт: {raffle['site']}\n🔗 {raffle['url']}\n{'-' * 60}\n")
                try:
                    notification.notify(title=f"🎁 РОЗЫГРЫШ ({raffle['site']})!", message=f"Канал: {channel.upper()}\n{raffle['url']}", app_name='Promo Scanner', timeout=5)
                except Exception:
                    pass
                if self.tg_client and TG_FORWARD_GROUP:
                    try:
                        tg_msg = f"🎁 **РОЗЫГРЫШ | {channel.upper()}**\n🎯 **{raffle['site']}**\n📺 https://twitch.tv/{channel}\n\n🔗 {raffle['url']}\n\n📝 Оригинал сообщения:\n```\n{text}\n```"
                        await self.tg_client.send_message(TG_FORWARD_GROUP, tg_msg, link_preview=False)
                    except Exception as e:
                        self.msg_queue.put(f'[ОШИБКА ПЕРЕСЫЛКИ РОЗЫГРЫША]: {e}\n')
        if matches:
            decrypted_codes = {}
            for code in matches:
                if '*' in code or '?' in code:
                    decrypted = decrypt_jabka_code(text, code)
                    decrypted_codes[code] = decrypted
            new_codes = []
            for code in matches:
                if ('?' in code or '*' in code) and code == decrypted_codes.get(code, code):
                    continue
                decrypted = decrypted_codes.get(code, code)
                if code not in SEEN_PROMOS and decrypted not in SEEN_PROMOS:
                    new_codes.append(code)
            if not new_codes:
                return
            for code in new_codes:
                decrypted = decrypted_codes.get(code, code)
                SEEN_PROMOS.add(decrypted)
                if decrypted != code:
                    SEEN_PROMOS.add(code)
            case_links = [m.group() for m in LINK_PATTERN.finditer(text)]
            dast_link = DAST_LINK_PATTERN.search(text)
            link_text = ''
            if case_links:
                links_formatted = '\n'.join([f'📦 {link}' for link in case_links])
                link_text = f'\n{links_formatted}'
            elif dast_link:
                link_text = f'\n🔗 https://dast.skin'
            timestamp = time.strftime('%H:%M:%S')
            channel = message.channel.name
            for code in new_codes:
                target_site = get_target_site(code)
                code_display = code
                if code in decrypted_codes and decrypted_codes[code] != code:
                    code_display = f'{code} → {decrypted_codes[code]}'
                save_code = decrypted_codes.get(code, code)
                save_promo(save_code, channel, 'TWITCH', timestamp)
                site_info = f'🎯 Сайт: {target_site}'
                if target_site == 'DAST.SKIN':
                    site_info += ' (https://dast.skin)'
                self.msg_queue.put(f"🔥 [TWITCH | {timestamp}] АЛЕРТ: {channel.upper()}\n{site_info}\n💬 Код: {code_display}{link_text}\n{'-' * 60}\n")
            notify_promo('TWITCH', channel, new_codes)
            if self.tg_client and TG_FORWARD_GROUP:
                try:
                    codes_formatted = '\n'.join([f'🔑 `{c}`' for c in new_codes])
                    current_month = time.strftime('%Y-%m')
                    author = message.author.name
                    site_header = ''
                    has_dast = any((len(c) == 9 and (not c.startswith('MD-')) for c in new_codes))
                    if has_dast:
                        site_header = '🎯 **DAST.SKIN** (https://dast.skin)\n\n'
                    tg_msg = f'🎮 **TWITCH | {channel.upper()}**\n📺 https://twitch.tv/{channel}\n\n{site_header}{codes_formatted}{link_text}\n\n📝 Оригинал:\n```\n{text}\n```\n🔗 Логи: https://tv.supa.sh/logs?c={channel}&u={author}&d={current_month}'
                    await self.tg_client.send_message(TG_FORWARD_GROUP, tg_msg, link_preview=False)
                except Exception as e:
                    self.msg_queue.put(f'[ОШИБКА ПЕРЕСЫЛКИ TWITCH]: {e}\n')

async def vk_scanner(msg_queue, tg_client):
    msg_queue.put('[VK] Инициализация сканера...\n')
    async with aiohttp.ClientSession() as session:
        while True:
            vk_channels = get_channels('VK')
            if not vk_channels:
                await asyncio.sleep(5)
                continue
            for channel in vk_channels:
                try:
                    params = {'access_token': VK_TOKEN, 'v': VK_API_VERSION, 'count': 3}
                    if channel.startswith('-') or channel.isdigit():
                        params['owner_id'] = channel
                    else:
                        params['domain'] = channel
                    async with session.get('https://api.vk.com/method/wall.get', params=params) as resp:
                        data = await resp.json()
                        if 'response' in data:
                            msg_queue.put('[VK HEALTH] OK')
                            items = data['response']['items']
                            for item in items:
                                post_id = f"{item['owner_id']}_{item['id']}"
                                text = item.get('text', '')
                                if channel in LAST_VK_POSTS and post_id in LAST_VK_POSTS[channel]:
                                    continue
                                if channel not in LAST_VK_POSTS:
                                    LAST_VK_POSTS[channel] = set([f"{i['owner_id']}_{i['id']}" for i in items])
                                    break
                                LAST_VK_POSTS[channel].add(post_id)
                                if str(channel).strip().lower() == JABKA_VK_DOMAIN:
                                    vk_matches = re.findall('([A-Z0-9]+)\\s*—\\s*промо', text, re.IGNORECASE)
                                    matches = [m.upper() for m in vk_matches]
                                else:
                                    matches = [m.group().upper() for m in PROMO_PATTERN.finditer(text)]
                                if matches:
                                    new_codes = [code for code in matches if code not in SEEN_PROMOS]
                                    if new_codes:
                                        SEEN_PROMOS.update(new_codes)
                                        timestamp = time.strftime('%H:%M:%S')
                                        post_link = f'https://vk.com/wall{post_id}'
                                        photos = []
                                        if 'attachments' in item:
                                            for att in item['attachments']:
                                                if att.get('type') == 'photo':
                                                    photo_sizes = att['photo'].get('sizes', [])
                                                    if photo_sizes:
                                                        largest = max(photo_sizes, key=lambda x: x.get('width', 0) * x.get('height', 0))
                                                        photos.append(largest['url'])
                                        for code in new_codes:
                                            target_site = get_target_site(code)
                                            save_promo(code, channel, 'VK', timestamp)
                                            msg_queue.put(f"🔥 [VK | {timestamp}] АЛЕРТ: {channel.upper()}\n🎯 Сайт: {target_site}\n💬 Код: {code}\n🔗 {post_link}\n{'-' * 60}\n")
                                        notify_promo('VK', channel, new_codes)
                                        if tg_client and TG_FORWARD_GROUP:
                                            try:
                                                codes_formatted = '\n'.join([f'🔑 `{c}`' for c in new_codes])
                                                tg_msg = f'🔥 **VK | {channel.upper()}**\n🎯 Сайт: {target_site}\n\n{codes_formatted}\n\n🔗 Пост: {post_link}\n\n📝 Оригинал:\n```\n{text}\n```'
                                                if photos:
                                                    try:
                                                        import io
                                                        from telethon.tl.types import DocumentAttributeFilename
                                                        async with session.get(photos[0]) as photo_resp:
                                                            if photo_resp.status == 200:
                                                                photo_data = await photo_resp.read()
                                                                photo_bytes = io.BytesIO(photo_data)
                                                                photo_bytes.name = 'photo.jpg'
                                                                await tg_client.send_file(TG_FORWARD_GROUP, file=photo_bytes, caption=tg_msg, force_document=False, attributes=[])
                                                            else:
                                                                await tg_client.send_message(TG_FORWARD_GROUP, tg_msg, link_preview=False)
                                                    except Exception as e:
                                                        msg_queue.put(f'[VK ФОТО ОШИБКА]: {e}\n')
                                                        await tg_client.send_message(TG_FORWARD_GROUP, tg_msg, link_preview=False)
                                                else:
                                                    await tg_client.send_message(TG_FORWARD_GROUP, tg_msg, link_preview=False)
                                            except Exception as e:
                                                msg_queue.put(f'[ОШИБКА ПЕРЕСЫЛКИ VK]: {e}\n')
                except Exception as e:
                    pass
            await asyncio.sleep(3)

async def start_scanners(msg_queue, runtime=None):
    timing_log = setup_timing_log(os.path.join(APP_DIR, 'telegram_timing.log'))
    twitch_bot = TwitchScanner(msg_queue)
    tg_client = TimedTelegramClient(StringSession(os.environ['TG_STRING_SESSION']), TG_API_ID, TG_API_HASH, timeout=8, request_retries=3, connection_retries=2, retry_delay=1, catch_up=True)
    if runtime is not None:
        runtime['scanner_telegram_client'] = tg_client
    twitch_bot.tg_client = tg_client
    comments_client = tg_client

    @tg_client.on(events.NewMessage(incoming=True, outgoing=True))
    async def tg_handler(event):
        received_at = time.monotonic()
        age = event_age(event.message)
        chat = await event.get_chat()
        if not chat:
            return
        chat_id = str(event.chat_id)
        if wake_comment_watchers(event.chat_id):
            msg_queue.put('[TELEGRAM] Новый комментарий - проверяю подсказку\n')
        chat_username = getattr(chat, 'username', None)
        chat_title = getattr(chat, 'title', None)
        configured_keys = set()
        for configured in get_channels('TELEGRAM'):
            configured_keys.update(telegram_channel_keys(configured))
        event_keys = set(telegram_channel_keys(chat_id))
        event_keys.update(telegram_channel_keys(chat_username))
        event_keys.update(telegram_channel_keys(chat_title))
        is_target = bool(configured_keys.intersection(event_keys))
        if _chat_key(event.chat_id) == _chat_key(JABKA_CHAT_ID) or (chat_username or '').lower() == JABKA_CHAT_USERNAME:
            chat_text = telegram_message_text(event.message)
            chat_links_src, _ = inline_entity_links(event.message)
            msg_id = event.message.id
            mask_in_chat = extract_mask_codes(chat_text)
            chat_codes = []
            for code in mask_in_chat:
                if code not in chat_codes and code not in SEEN_PROMOS and (decrypt_jabka_code(chat_text, code) not in SEEN_PROMOS):
                    chat_codes.append(code)
            if chat_codes:
                decrypted = []
                for code in chat_codes:
                    dec = decrypt_jabka_code(chat_text, code)
                    decrypted.append(dec)
                    SEEN_PROMOS.add(dec)
                    SEEN_PROMOS.add(code)
                chat_ts = time.strftime('%H:%M:%S')
                chat_name = chat_title or chat_username or str(event.chat_id)
                author = getattr(event.message, 'post_author', None) or ''
                for code in decrypted:
                    save_promo(code, chat_name, 'TELEGRAM', chat_ts)
                    msg_queue.put(f'🔥 [TELEGRAM | {chat_ts}] ЧАТ: {chat_name.upper()}\n💬 Код: {code}\n' + '-' * 60 + '\n')
                notify_promo('TELEGRAM', chat_name, decrypted)
                if TG_FORWARD_GROUP:
                    try:
                        codes_lines = []
                        for orig, dec in zip(chat_codes, decrypted):
                            case_name, case_link = _extract_case_info(chat_links_src, orig)
                            if orig != dec:
                                line_text = f'🔑 {orig} → {dec}'
                            else:
                                line_text = f'🔑 {dec}'
                            if case_name:
                                line_text += f' - {case_name}'
                            if case_link:
                                line_text += f' ({case_link})'
                            codes_lines.append(line_text)
                        codes_block = '\n'.join(codes_lines)
                        if chat_username:
                            link_line = f'📢 https://t.me/{chat_username}/{msg_id}\n\n'
                        else:
                            link_line = '\n'
                        author_line = f'👤 {author}\n\n' if author else ''
                        chat_msg = f'✈️ **TELEGRAM | {chat_name.upper()}**\n{link_line}{author_line}{codes_block}\n\n📝 Оригинал:\n```\n{chat_text}\n```'
                        await tg_client.send_message(TG_FORWARD_GROUP, chat_msg, link_preview=False)
                    except Exception as e:
                        msg_queue.put(f'[ОШИБКА ПЕРЕСЫЛКИ ЧАТА]: {e}\n')
            return
        if is_target:
            timing_log.info('RECEIVE chat=%s post=%s kind=%s age=%s lookup=%.3fs', event.chat_id, event.message.id, 'edit' if getattr(event.message, 'edit_date', None) else 'new', round(age, 3) if age is not None else 'unknown', time.monotonic() - received_at)
            text = telegram_message_text(event.message)
            post_id = event.message.id
            link_src, link_labels = inline_entity_links(event.message)
            raffles_upgrader_matches = RAFFLES_UPGRADER_PATTERN.findall(text)
            raffles_upgrader_username = RAFFLES_UPGRADER_USERNAME_PATTERN.findall(text)
            raffles_caser_matches = RAFFLES_CASER_PATTERN.findall(text)
            raffles_upgrader_matches_hidden = RAFFLES_UPGRADER_PATTERN.findall(link_src)
            raffles_upgrader_username_hidden = RAFFLES_UPGRADER_USERNAME_PATTERN.findall(link_src)
            raffles_caser_matches_hidden = RAFFLES_CASER_PATTERN.findall(link_src)
            all_upgrader_raffles_tg = list(set(raffles_upgrader_matches + raffles_upgrader_username + raffles_upgrader_matches_hidden + raffles_upgrader_username_hidden))
            if not all_upgrader_raffles_tg:
                any_upgrader_r = ANY_UPGRADER_RAFFLE.findall(text + ' ' + link_src)
                any_upgrader_u = ANY_UPGRADER_RAFFLE_USERNAME.findall(text + ' ' + link_src)
                for link in any_upgrader_r:
                    if '/r/' in link:
                        parts = link.split('/r/')
                        if len(parts) > 1:
                            code = parts[1].split('?')[0].split('&')[0].split('/')[0]
                            if code and code.replace('_', '').replace('-', '').isalnum():
                                all_upgrader_raffles_tg.append(code)
                for link in any_upgrader_u:
                    parts = link.split('/')
                    if len(parts) >= 4:
                        username = parts[-1].split('?')[0].split('&')[0]
                        if username and username.replace('_', '').replace('-', '').isalnum() and (len(username) >= 3):
                            all_upgrader_raffles_tg.append(username)
            all_caser_raffles_tg = list(set(raffles_caser_matches + raffles_caser_matches_hidden))
            if not all_caser_raffles_tg:
                any_caser = ANY_CASER_RAFFLE.findall(text + ' ' + link_src)
                for link in any_caser:
                    parts = link.split('/')
                    if len(parts) >= 4:
                        code = parts[-1].split('?')[0].split('&')[0]
                        if code and code.replace('_', '').replace('-', '').isalnum():
                            all_caser_raffles_tg.append(code)
            new_raffles_tg = []
            channel_name = chat_username or chat_title or str(chat_id)
            for raffle_code in all_upgrader_raffles_tg:
                raffle_url = None
                for link in ANY_UPGRADER_RAFFLE.findall(text + ' ' + link_src) + ANY_UPGRADER_RAFFLE_USERNAME.findall(text + ' ' + link_src):
                    if raffle_code in link:
                        raffle_url = link
                        break
                if not raffle_url:
                    if raffle_code.isupper() and len(raffle_code) <= 10:
                        raffle_url = f'https://raffles.upgrader.best/r/{raffle_code}'
                    else:
                        raffle_url = f'https://raffles.upgrader.best/{raffle_code}'
                raffle_id = f'upgrader_{raffle_code}'
                should_notify = save_raffle(channel_name, 'upgrader', raffle_url, raffle_id)
                if should_notify:
                    new_raffles_tg.append({'site': 'Upgrader', 'url': raffle_url})
            for raffle_username in all_caser_raffles_tg:
                raffle_url = None
                for link in ANY_CASER_RAFFLE.findall(text + ' ' + link_src):
                    if raffle_username in link:
                        raffle_url = link
                        break
                if not raffle_url:
                    raffle_url = f'https://raffles.caser.one/{raffle_username}'
                raffle_id = f'caser_{raffle_username}'
                should_notify = save_raffle(channel_name, 'caser', raffle_url, raffle_id)
                if should_notify:
                    new_raffles_tg.append({'site': 'Caser', 'url': raffle_url})
            for raffle_id, raffle_url in extract_betboom_raffles(text + ' ' + link_src):
                if save_raffle(channel_name, 'betboom', raffle_url, raffle_id):
                    new_raffles_tg.append({'site': 'BetBoom', 'url': raffle_url})
            if new_raffles_tg:
                timing_log.info('DETECT raffle chat=%s post=%s elapsed=%.3fs', event.chat_id, post_id, time.monotonic() - received_at)
                timestamp = time.strftime('%H:%M:%S')
                for raffle in new_raffles_tg:
                    msg_queue.put(f"🎁 [TELEGRAM | {timestamp}] РОЗЫГРЫШ: {channel_name.upper()}\n🎯 Сайт: {raffle['site']}\n🔗 {raffle['url']}\n{'-' * 60}\n")
                    try:
                        notification.notify(title=f"🎁 РОЗЫГРЫШ ({raffle['site']})!", message=f"TG: {channel_name.upper()}\n{raffle['url']}", app_name='Promo Scanner', timeout=5)
                    except Exception:
                        pass
                    if TG_FORWARD_GROUP:
                        try:
                            tg_msg = f"🎁 **РОЗЫГРЫШ | TELEGRAM**\n📱 Канал: {channel_name}\n🎯 **{raffle['site']}**\n\n🔗 {raffle['url']}\n\n📝 Оригинал сообщения:\n```\n{text}\n```"
                            await tg_client.send_message(TG_FORWARD_GROUP, tg_msg, link_preview=False)
                        except Exception as e:
                            msg_queue.put(f'[ОШИБКА ПЕРЕСЫЛКИ РОЗЫГРЫША TG]: {e}\n')
            md_matches = [m.group().upper() for m in PROMO_PATTERN.finditer(text)]
            if is_jabka_source(chat_username, event.chat_id):
                other_codes = [c for c in extract_promo_codes(text) if not c.startswith('MD-')]
            else:
                other_codes = []
            short_codes = {c: _case_name_from_line(text, c) for c in other_codes if _case_name_from_line(text, c)}
            seen_in_msg = set()
            ordered_matches = []
            for code in md_matches + other_codes:
                if code not in seen_in_msg:
                    seen_in_msg.add(code)
                    ordered_matches.append(code)
            matches = ordered_matches
            if matches:
                decrypted_codes = {}
                for code in matches:
                    if '*' in code or '?' in code:
                        decrypted_codes[code] = decrypt_jabka_code(text, code)
                unresolved = [c for c in matches if has_unresolved_hint(text, c)]
                new_codes = []
                for code in matches:
                    decrypted = decrypted_codes.get(code, code)
                    if decrypted == code and code in decrypted_codes:
                        continue
                    if code not in SEEN_PROMOS and decrypted not in SEEN_PROMOS:
                        new_codes.append(code)
                unresolved = [c for c in unresolved if c not in SEEN_PROMOS]
                if not new_codes and (not unresolved):
                    return
                for code in new_codes:
                    decrypted = decrypted_codes.get(code, code)
                    SEEN_PROMOS.add(decrypted)
                    if decrypted != code:
                        SEEN_PROMOS.add(code)
                for code in unresolved:
                    SEEN_PROMOS.add(code)
                jabka_links = [m.group() for m in CASE_URL_RE.finditer(link_src) if 'jabka' in m.group().lower()]
                dast_link = DAST_LINK_PATTERN.search(text)
                code_to_link = {}
                for code in new_codes + unresolved:
                    link = find_case_link_for_code(link_src, code)
                    if link:
                        code_to_link[code] = link

                def _case_for(c):
                    """(название, чистая ссылка) кейса для кода - жабки или MD."""
                    raw_link = code_to_link.get(c)
                    if raw_link and link_labels.get(raw_link):
                        return (case_label_for_link(raw_link, link_labels), raw_link.split('?')[0])
                    name, clean = (None, None)
                    if get_target_site(c) == 'JABKA':
                        name, clean = resolve_case_for_code(link_src, c, code_to_link, short_codes, jabka_links)
                    if raw_link:
                        clean = clean or raw_link.split('?')[0]
                        name = name or case_label_for_link(raw_link, link_labels)
                    return (name, clean)
                link_text = ''
                timestamp = time.strftime('%H:%M:%S')
                display_name = chat_title if chat_title else chat_username if chat_username else chat_id
                for code in new_codes + unresolved:
                    target_site = get_target_site(code)
                    if code not in unresolved:
                        save_promo(code, display_name, 'TELEGRAM', timestamp)
                    code_display = code
                    if code in decrypted_codes and decrypted_codes[code] != code:
                        code_display = f'{code} → {decrypted_codes[code]}'
                    elif code in unresolved:
                        code_display = f'{code} (подсказка в комментариях)'
                    case_name, clean_link = _case_for(code)
                    if case_name:
                        code_display = f'{code_display} - {case_name}'
                        if clean_link:
                            code_display = f'{code_display} ({clean_link})'
                    link_text = ''
                    site_info = f'🎯 Сайт: {target_site}'
                    if target_site == 'DAST.SKIN':
                        site_info += ' (https://dast.skin)'
                    msg_queue.put(f"🔥 [TELEGRAM | {timestamp}] АЛЕРТ: {display_name.upper()}\n{site_info}\n💬 Код: {code_display}{(link_text if target_site != 'JABKA' or code not in code_to_link else '')}\n{'-' * 60}\n")
                timing_log.info('DETECT promo chat=%s post=%s elapsed=%.3fs', event.chat_id, post_id, time.monotonic() - received_at)
                notify_promo('TELEGRAM', display_name, new_codes)
                if TG_FORWARD_GROUP:
                    try:
                        forward_target = str(TG_FORWARD_GROUP).replace('@', '').lower()
                        if str(chat_id) != forward_target and str(chat_id).replace('-100', '') != forward_target:
                            codes_formatted_list = []
                            for c in new_codes + unresolved:
                                code_line = f'🔑 `{c}`'
                                if c in decrypted_codes and decrypted_codes[c] != c:
                                    code_line += f' → `{decrypted_codes[c]}`'
                                elif c in unresolved:
                                    code_line += ' (подсказка в комментариях)'
                                case_name, clean_link = _case_for(c)
                                if case_name:
                                    code_line += f' - [{case_name}]({clean_link})' if clean_link else f' - {case_name}'
                                codes_formatted_list.append(code_line)
                            codes_formatted = '\n'.join(codes_formatted_list)
                            tg_link_str = f'📢 https://t.me/{chat_username}\n\n' if chat_username else '\n'
                            show_link_text = link_text if not code_to_link else ''
                            tg_msg = f'✈️ **TELEGRAM | {display_name.upper()}**\n{tg_link_str}{codes_formatted}{show_link_text}\n\n📝 Оригинал:\n```\n{text}\n```'
                            await tg_client.send_message(TG_FORWARD_GROUP, tg_msg, link_preview=False)
                            if unresolved and comments_client is not None:

                                async def _send_resolved(resolved, votes_count):
                                    """Досыл расшифрованных кодов после подсказки из комментариев."""
                                    lines = ['🔓 **Расшифровка из комментариев**', '']
                                    for old_code, new_code in resolved:
                                        line = f'🔑 `{old_code}` → `{new_code}`'
                                        name, link = _case_for(old_code)
                                        if name:
                                            line += f' - [{name}]({link})' if link else f' - {name}'
                                        lines.append(line)
                                    lines.append('')
                                    lines.append(f'📢 Пост: https://t.me/{chat_username}/{post_id}' if chat_username else '')
                                    await tg_client.send_message(TG_FORWARD_GROUP, '\n'.join(lines), link_preview=False)
                                    done_ts = time.strftime('%H:%M:%S')
                                    for old_code, new_code in resolved:
                                        SEEN_PROMOS.add(old_code)
                                        SEEN_PROMOS.add(new_code)
                                        save_promo(new_code, display_name, 'TELEGRAM', done_ts)
                                    msg_queue.put(f"🔥 [TELEGRAM | {time.strftime('%H:%M:%S')}] ДОСЫЛ: расшифровано из комментариев {len(resolved)} к. (подсказку повторили {votes_count} раз)\n")
                                start_comment_watcher(comments_client, chat, post_id, unresolved, msg_queue, _send_resolved)
                    except Exception as e:
                        msg_queue.put(f'[ОШИБКА ПЕРЕСЫЛКИ]: {e}\n')
    tg_client.add_event_handler(tg_handler, events.MessageEdited())

    async def run_platform(name, coroutine):
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            msg_queue.put(f'[ОШИБКА {name}] {type(exc).__name__}: {exc}\n')
    tasks = [asyncio.create_task(run_platform('TWITCH', run_twitch_reader(twitch_bot))), asyncio.create_task(supervise_telegram(tg_client, msg_queue)), asyncio.create_task(run_platform('VK', vk_scanner(msg_queue, tg_client)))]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

async def run_twitch_reader(scanner):
    from twitch_reader import Reader
    await Reader(scanner, TWITCH_TOKEN, scanner.msg_queue.status).run()
