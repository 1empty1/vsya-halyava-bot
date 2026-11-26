"""Private owner-only scanner management with durable, idempotent jobs."""
import asyncio
import logging
import re
import time
from urllib.parse import urlparse
from telethon import errors
from telethon.tl.functions.channels import JoinChannelRequest

log = logging.getLogger('admin')
HELP = """🛠 Управление сканером

/status — состояние и очередь
/addtg @username — добавить публичный Telegram-канал
/addtwitch login — добавить Twitch-канал
/deltg @username — убрать Telegram-канал из сканирования
/deltwitch login — убрать Twitch-канал
/channels tg 1 — список Telegram, страница 1
/channels twitch 1 — список Twitch

Можно указывать ссылки t.me/username и twitch.tv/login.
Telegram: сканер подписывается своим аккаунтом. При удалении перестаёт читать источник, но остаётся подписанным.
Twitch: изменения применяются при следующем выборе чатов (обычно до 7 минут), с прежним лимитом 100.
/jobs — последние операции и результат
/admin — эта справка"""
COMMANDS = {'/admin', '/status', '/channels', '/jobs', '/addtg', '/addtwitch', '/deltg', '/deltwitch'}

def normalize_channel(value, platform):
    value = value.strip()
    if '/' in value or '://' in value:
        parsed = urlparse(value if '://' in value else 'https://' + value)
        hosts = {'t.me', 'telegram.me'} if platform == 'TELEGRAM' else {'twitch.tv', 'www.twitch.tv'}
        if parsed.scheme != 'https' or parsed.netloc.lower() not in hosts or parsed.query or parsed.fragment:
            raise ValueError('Нужна ссылка на канал: t.me/username или twitch.tv/login.')
        value = parsed.path.strip('/')
    else:
        value = value.lstrip('@' if platform == 'TELEGRAM' else '#@')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,32}' if platform == 'TELEGRAM' else r'[A-Za-z0-9_]{1,25}', value):
        raise ValueError('Укажи публичный username канала. Ссылки на посты и приглашения пока не поддерживаются.')
    return value.lower()

class Admin:
    def __init__(self, bot, app, owners):
        self.bot, self.app, self.owners = bot, app, set(owners)
        self.db = bot.store.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS admin_jobs(
            id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, action TEXT,
            platform TEXT, name TEXT, state TEXT DEFAULT 'pending', result TEXT DEFAULT '',
            due REAL DEFAULT 0, UNIQUE(chat_id,message_id))""")
        self.db.commit()

    def jobs(self):
        return self.db.execute('SELECT * FROM admin_jobs ORDER BY id').fetchall()

    def signal(self):
        if 'persist_event' in self.app:
            self.app['persist_event'].set()

    async def handle(self, message):
        text = message.get('text') or ''
        parts = text.split()
        cmd = parts[0].split('@')[0].lower() if parts else ''
        if cmd not in COMMANDS:
            return False
        chat = message.get('chat', {})
        sender = message.get('from', {}).get('id')
        if chat.get('type') != 'private' or sender not in self.owners or chat.get('id') != sender:
            return True
        reply = HELP
        if cmd == '/status':
            s = self.app.get('scanner_status', {})
            age = int(time.time() - s['checkpoint_at']) if s.get('checkpoint_at') else None
            pending = self.db.execute("SELECT count(*) FROM deliveries WHERE state='pending'").fetchone()[0]
            reply = (f"📊 Telegram: {s.get('telegram', 'не запущен')}\n"
                     f"Twitch: {s.get('twitch', 'не запущен')} — {s.get('twitch_channels', 0)}/{s.get('twitch_total', 0)} чатов\n"
                     f"VK: {s.get('vk', 'не запущен')}\n"
                     f"Снимок сканера: {str(age) + ' сек. назад' if age is not None else 'нет данных'}\n"
                     f"Сохранение бота: {'OK' if self.app.get('checkpoint_healthy') else 'нет подтверждения'}\n"
                     f"Очередь личной рассылки: {pending}\n/status не доказывает доставку каждого поста.")
        elif cmd == '/jobs':
            rows = self.db.execute('SELECT * FROM admin_jobs ORDER BY id DESC LIMIT 10').fetchall()
            reply = '\n'.join(f"#{r['id']} {r['action']} {r['platform']} {r['name']}: {r['state']} {r['result']}" for r in rows) or 'Операций пока нет.'
        elif cmd == '/channels':
            scanner = self.app.get('scanner_store')
            platform = {'tg': 'TELEGRAM', 'telegram': 'TELEGRAM', 'twitch': 'TWITCH'}.get(parts[1].lower() if len(parts)>1 else 'tg')
            try:
                page = max(1, int(parts[2])) if len(parts)>2 else 1
            except ValueError:
                page = 1
            if not scanner:
                reply = 'Сканер ещё не запущен.'
            elif not platform:
                reply = 'Пример: /channels tg 1 или /channels twitch 1'
            else:
                names = [r[0] for r in scanner.db.execute('SELECT DISTINCT name FROM channels WHERE platform=? ORDER BY name', (platform,))]
                pages = max(1, (len(names)+39)//40); page = min(page, pages)
                reply = f'{platform}: {len(names)} источников · страница {page}/{pages}\n' + '\n'.join(names[(page-1)*40:page*40])
        elif cmd.startswith(('/add', '/del')):
            platform = 'TELEGRAM' if cmd.endswith('tg') else 'TWITCH'
            try:
                if len(parts) != 2:
                    raise ValueError(f'Пример: {cmd} username')
                name = normalize_channel(parts[1], platform)
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO admin_jobs(chat_id,message_id,action,platform,name) VALUES(?,?,?,?,?)',
                                    (sender,message['message_id'],'add' if cmd.startswith('/add') else 'delete',platform,name))
                self.signal()
                reply = 'Операция принята. Результат появится здесь; /jobs — история.'
            except ValueError as exc:
                reply = str(exc)
        await self.bot.api('sendMessage', chat_id=sender, text=reply)
        return True

    async def apply(self, row):
        scanner = self.app.get('scanner_store')
        if scanner is None:
            raise RuntimeError('Scanner not started')
        name, platform = row['name'], row['platform']
        db = scanner.db
        if row['action'] == 'add' and platform == 'TELEGRAM':
            client = self.app.get('scanner_telegram_client')
            if client is None or not client.is_connected():
                raise RuntimeError('Telegram reconnecting')
            entity = await client.get_entity(name)
            if not getattr(entity, 'broadcast', False):
                raise ValueError('Это не публичный Telegram-канал.')
            await client(JoinChannelRequest(entity))
            name = (entity.username or name).lower()
        elif row['action'] == 'add' and platform == 'TWITCH':
            query = 'query($login:String!){user(login:$login){id}}'
            async with self.app['session'].post('https://gql.twitch.tv/gql', headers={'Client-Id':'kimne78kx3ncx6brgo4mv6wki5h1ko'}, json={'query':query,'variables':{'login':name}}) as response:
                response.raise_for_status()
                data = await response.json()
            if data.get('errors'):
                raise RuntimeError('Twitch validation failed')
            if not (data.get('data') or {}).get('user'):
                raise ValueError('Twitch-канал не найден.')
        with db:
            if row['action'] == 'add':
                if not db.execute('SELECT 1 FROM channels WHERE lower(name)=? AND platform=?', (name, platform)).fetchone():
                    db.execute('INSERT INTO channels(name,platform) VALUES(?,?)', (name,platform))
            else:
                # Compare normalized forms to support legacy @names and URL entries.
                ids = []
                for record in db.execute('SELECT id,name FROM channels WHERE platform=?', (platform,)):
                    try:
                        if normalize_channel(record['name'], platform) == name:
                            ids.append((record['id'],))
                    except ValueError:
                        pass
                db.executemany('DELETE FROM channels WHERE id=?', ids)
        s = self.app.get('scanner_status', {})
        s['telegram_total'] = db.execute("SELECT count(*) FROM channels WHERE platform='TELEGRAM'").fetchone()[0]
        # Save before reporting successful completion; transient failures retry idempotently.
        await self.app['scanner_checkpoint'].save(scanner)
        s['checkpoint_at'] = int(time.time())
        return ('Добавлен' if row['action']=='add' else 'Убран из сканирования') + f': {platform} {name}. Сохранено в Supabase.'

    async def worker(self):
        while True:
            row = None
            try:
                row = self.db.execute("SELECT * FROM admin_jobs WHERE state IN ('pending','notify') AND due<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
                if row:
                    if row['state'] == 'pending':
                        # Persist intent before an external subscription or changing sources.
                        await self.app['checkpoint'].save(self.bot.store)
                        try:
                            result = await asyncio.wait_for(self.apply(row), 60)
                        except errors.FloodWaitError as exc:
                            with self.db:
                                self.db.execute('UPDATE admin_jobs SET due=?,result=? WHERE id=?', (time.time()+max(5,exc.seconds), 'Telegram ограничил частоту; повтор запланирован.', row['id']))
                            self.signal()
                            continue
                        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError, errors.ChannelPrivateError, errors.ChannelsTooMuchError):
                            result = 'Не добавлен: проверь публичный адрес и доступ аккаунта; возможно достигнут лимит подписок.'
                        with self.db:
                            self.db.execute("UPDATE admin_jobs SET state='notify',result=? WHERE id=?", (result, row['id']))
                        await self.app['checkpoint'].save(self.bot.store)
                    result = self.db.execute('SELECT result FROM admin_jobs WHERE id=?', (row['id'],)).fetchone()[0]
                    await self.bot.api('sendMessage', chat_id=row['chat_id'], text=result)
                    with self.db:
                        self.db.execute("UPDATE admin_jobs SET state='done' WHERE id=?", (row['id'],))
                    self.signal()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Admin job will retry: %s', type(exc).__name__)
                if row:
                    with self.db:
                        self.db.execute("UPDATE admin_jobs SET due=?,result=CASE WHEN state='pending' THEN ? ELSE result END WHERE id=?", (time.time()+60, 'Временная ошибка; повтор через минуту.', row['id']))
                    self.signal()
            await asyncio.sleep(2)
