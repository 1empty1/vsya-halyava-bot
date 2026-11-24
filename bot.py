"""Opt-in delivery of verified scanner posts from the existing Telegram feed."""
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import aiohttp

CATEGORIES = {'betboom': 'BetBoom', 'upgrader': 'Upgrader', 'caser': 'Caser',
              'magicdrop': 'Magic Drop', 'jabka': 'Жабка'}
log = logging.getLogger('halyava')


def is_scanner_post(text):
    return bool(re.search(r'^(?:🎁\s*РОЗЫГРЫШ\s*\||[^\w\n]*\b(?:TELEGRAM|TWITCH|VK)\s*\|)', text))


def classify(text):
    # Original quoted text can advertise unrelated sites: classify the scanner header only.
    header = re.split(r'📝\s*Оригинал', text, maxsplit=1)[0]
    result = set()
    for key, pattern in {
        'betboom': r'\bbetboom\b', 'upgrader': r'\bupgrader\b',
        'caser': r'\bcaser\b', 'magicdrop': r'\bMD-[A-Z0-9*?]+|magic[\s.-]*drop',
        'jabka': r'jabka|жабк',
    }.items():
        if re.search(pattern, header, re.I):
            result.add(key)
    return result


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS preferences(user_id INTEGER,category TEXT,PRIMARY KEY(user_id,category));
        CREATE TABLE IF NOT EXISTS posts(chat_id INTEGER,message_id INTEGER,PRIMARY KEY(chat_id,message_id));
        CREATE TABLE IF NOT EXISTS deliveries(
          id INTEGER PRIMARY KEY,user_id INTEGER,chat_id INTEGER,message_id INTEGER,
          categories TEXT,state TEXT DEFAULT 'pending',due REAL DEFAULT 0,attempts INTEGER DEFAULT 0,
          UNIQUE(user_id,chat_id,message_id));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        ''')

    def ensure_user(self, user_id):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO users(id) VALUES(?)', (user_id,))

    def preferences(self, user_id):
        return {r[0] for r in self.db.execute('SELECT category FROM preferences WHERE user_id=?', (user_id,))}

    def toggle(self, user_id, category):
        if category not in CATEGORIES:
            return
        self.ensure_user(user_id)
        with self.db:
            self.db.execute('UPDATE users SET active=1 WHERE id=?', (user_id,))
            if category in self.preferences(user_id):
                self.db.execute('DELETE FROM preferences WHERE user_id=? AND category=?', (user_id, category))
            else:
                self.db.execute('INSERT INTO preferences VALUES(?,?)', (user_id, category))

    def all_on(self, user_id):
        self.ensure_user(user_id)
        with self.db:
            self.db.execute('UPDATE users SET active=1 WHERE id=?', (user_id,))
            self.db.executemany('INSERT OR IGNORE INTO preferences VALUES(?,?)', [(user_id, k) for k in CATEGORIES])

    def stop(self, user_id):
        with self.db:
            self.db.execute('UPDATE users SET active=0 WHERE id=?', (user_id,))
            self.db.execute('DELETE FROM preferences WHERE user_id=?', (user_id,))
            self.db.execute("UPDATE deliveries SET state='cancelled' WHERE user_id=? AND state='pending'", (user_id,))

    def enqueue(self, chat_id, message_id, categories):
        if not categories:
            return
        with self.db:
            new = self.db.execute('INSERT OR IGNORE INTO posts VALUES(?,?)', (chat_id, message_id)).rowcount
            if not new:
                return
            users = self.db.execute('SELECT id FROM users WHERE active=1').fetchall()
            for user in users:
                if self.preferences(user['id']) & categories:
                    self.db.execute('INSERT OR IGNORE INTO deliveries(user_id,chat_id,message_id,categories) VALUES(?,?,?,?)',
                                    (user['id'], chat_id, message_id, json.dumps(sorted(categories))))

    def pending(self):
        return self.db.execute("SELECT * FROM deliveries WHERE state='pending' AND due<=? ORDER BY id LIMIT 50", (time.time(),)).fetchall()

    def still_wanted(self, row):
        return bool(self.preferences(row['user_id']) & set(json.loads(row['categories'])))

    def finish(self, row_id, state):
        with self.db:
            self.db.execute('UPDATE deliveries SET state=? WHERE id=?', (state, row_id))

    def retry(self, row_id, seconds):
        with self.db:
            self.db.execute('UPDATE deliveries SET due=?,attempts=attempts+1 WHERE id=?', (time.time()+seconds, row_id))

    def get(self, key, default='0'):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))


class APIError(Exception):
    def __init__(self, data):
        self.code = data.get('error_code', 0)
        self.retry_after = data.get('parameters', {}).get('retry_after', 0)
        self.description = data.get('description', '')
        super().__init__('Telegram API error ' + str(self.code))


class Bot:
    def __init__(self, token, store, source_chat, senders):
        self.token, self.store, self.source_chat, self.senders = token, store, source_chat, set(senders)
        self.session = None

    async def api(self, method, **payload):
        async with self.session.post('https://api.telegram.org/bot'+self.token+'/'+method, json=payload) as response:
            data = await response.json()
        if not data.get('ok'):
            raise APIError(data)
        return data['result']

    def keyboard(self, user_id):
        chosen = self.store.preferences(user_id)
        rows = [[{'text': ('✅ ' if k in chosen else '▫️ ')+name, 'callback_data': 'toggle:'+k}] for k, name in CATEGORIES.items()]
        rows += [[{'text': '✅ Включить всё', 'callback_data': 'all'}, {'text': '🔕 Отключить всё', 'callback_data': 'stop'}]]
        return {'inline_keyboard': rows}

    async def menu(self, user_id, message_id=None):
        self.store.ensure_user(user_id)
        chosen = self.store.preferences(user_id)
        text = ('🎁 Вся халява\n\nВыбери категории для личной рассылки кнопками ниже.\n'
                'В общем канале по-прежнему публикуется всё.\n\n'
                'Включено: '+(', '.join(v for k, v in CATEGORIES.items() if k in chosen) or 'ничего')+
                '\n\nПриходят только новые публикации после включения категории.\n'
                '/menu — изменить выбор · /stop — отключить рассылку')
        payload = dict(chat_id=user_id, text=text, reply_markup=self.keyboard(user_id))
        if message_id:
            try:
                await self.api('editMessageText', message_id=message_id, **payload)
            except APIError as exc:
                if exc.code == 400 and 'not modified' in exc.description.lower():
                    return
                raise
        else:
            await self.api('sendMessage', **payload)

    async def update(self, update):
        cb = update.get('callback_query')
        if cb:
            user_id = cb['from']['id']
            message = cb.get('message', {})
            if message.get('chat', {}).get('id') != user_id or message.get('chat', {}).get('type') != 'private':
                return
            # Persist the callback ID alongside preference changes to make replays safe.
            if self.store.get('callback', '') != cb['id']:
                action = cb.get('data', '')
                if action.startswith('toggle:'):
                    self.store.toggle(user_id, action[7:])
                elif action == 'all':
                    self.store.all_on(user_id)
                elif action == 'stop':
                    self.store.stop(user_id)
                self.store.set('callback', cb['id'])
            try:
                await self.api('answerCallbackQuery', callback_query_id=cb['id'])
            except APIError:
                pass
            await self.menu(user_id, message.get('message_id'))
            return
        message = next((update[k] for k in ('message', 'channel_post', 'edited_message', 'edited_channel_post') if k in update), None)
        if not message:
            return
        chat = message['chat']
        text = message.get('text') or message.get('caption') or ''
        if chat['type'] == 'private':
            cmd = text.split(maxsplit=1)[0].split('@')[0] if text else ''
            if cmd == '/stop':
                self.store.stop(chat['id'])
                await self.api('sendMessage', chat_id=chat['id'], text='🔕 Личная рассылка отключена. /menu — выбрать категории заново.')
            elif cmd in ('/start', '/menu', '/help'):
                await self.menu(chat['id'])
            return
        if chat['id'] != self.source_chat:
            return
        # Source is a discussion group: never distribute ordinary participant messages.
        if message.get('from', {}).get('id') not in self.senders or not is_scanner_post(text):
            return
        self.store.enqueue(chat['id'], message['message_id'], classify(text))

    async def deliveries(self):
        while True:
            for row in self.store.pending():
                if not self.store.still_wanted(row):
                    self.store.finish(row['id'], 'cancelled')
                    continue
                try:
                    await self.api('copyMessage', chat_id=row['user_id'], from_chat_id=row['chat_id'], message_id=row['message_id'])
                    self.store.finish(row['id'], 'sent')
                except APIError as exc:
                    if exc.code == 403:
                        self.store.stop(row['user_id'])
                    elif exc.code == 429:
                        self.store.retry(row['id'], exc.retry_after or 30)
                        await asyncio.sleep(exc.retry_after or 30)
                    elif exc.code == 400:
                        self.store.finish(row['id'], 'failed')
                        log.warning('A source message cannot be copied; delivery marked failed')
                    else:
                        self.store.retry(row['id'], min(300, 2**min(row['attempts']+1, 8)))
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    self.store.retry(row['id'], 15)
                await asyncio.sleep(.1)
            await asyncio.sleep(1)

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            self.session = session
            me = await self.api('getMe')
            if me['username'].lower() != os.getenv('EXPECTED_BOT_USERNAME', 'vsyaHalyava_bot').lower():
                raise RuntimeError('Unexpected bot identity')
            webhook = await self.api('getWebhookInfo')
            if webhook.get('url'):
                raise RuntimeError('Existing webhook detected: refusing to replace it automatically')
            log.info('Bot @%s is running', me['username'])
            sender = asyncio.create_task(self.deliveries())
            try:
                while True:
                    if sender.done():
                        sender.result()
                    try:
                        updates = await self.api('getUpdates', offset=int(self.store.get('offset')), timeout=25,
                                                 allowed_updates=['message', 'channel_post', 'edited_message', 'edited_channel_post', 'callback_query'])
                        for update in updates:
                            await self.update(update)
                            self.store.set('offset', update['update_id']+1)
                    except APIError as exc:
                        if exc.code in (401, 409):
                            raise RuntimeError('Token rejected or another bot instance is polling') from None
                        log.warning('Telegram temporary error %s', exc.code)
                        await asyncio.sleep(exc.retry_after or 5)
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        log.warning('Telegram connection interrupted; retrying')
                        await asyncio.sleep(5)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ['BOT_TOKEN'].strip()
    source = int(os.environ['SOURCE_CHAT_ID'])
    senders = [int(x.strip()) for x in os.environ['SCANNER_SENDER_IDS'].split(',')]
    store = Store(os.getenv('DATA_PATH', './data/bot.sqlite3'))
    try:
        asyncio.run(Bot(token, store, source, senders).run())
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
