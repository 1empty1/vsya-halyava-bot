"""Private owner-only scanner management with durable, idempotent jobs."""
import asyncio
import json
import secrets
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
Twitch: изменения применяются при следующем выборе чатов (обычно до 7 минут), с лимитом сканера 90.
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

    def ui_state(self, owner):
        return json.loads(self.bot.store.get(f'admin_ui:{owner}', '{}'))

    def set_ui(self, owner, value):
        self.bot.store.set(f'admin_ui:{owner}', json.dumps(value))
        self.signal()

    @staticmethod
    def button(text, data):
        return {'text': text, 'callback_data': 'adm:' + data}

    async def show(self, owner, text, rows, message_id=None):
        from bot import APIError
        payload = dict(chat_id=owner, text=text, reply_markup={'inline_keyboard':rows})
        if message_id:
            try:
                await self.bot.api('editMessageText', message_id=message_id, **payload)
                return
            except APIError as exc:
                if 'not modified' in exc.description.lower():
                    return
                if exc.code != 400:
                    raise
        await self.bot.api('sendMessage', **payload)

    async def home(self, owner, message_id=None):
        self.set_ui(owner, {})
        b=self.button
        await self.show(owner, '🛠 Управление сканером\n\nИсточники, состояние и последние операции.\nВыбери раздел ниже.', [
            [b('✈️ Telegram', 'list:tg:0'), b('🟣 Twitch', 'list:tw:0')],
            [b('📊 Состояние', 'status'), b('🕘 Операции', 'jobs')],
            [b('🎁 Мои подписки', 'subscriptions')]], message_id)

    async def listing(self, owner, kind, page, message_id=None):
        b=self.button
        scanner=self.app.get('scanner_store')
        platform={'tg':'TELEGRAM','tw':'TWITCH'}[kind]
        if not scanner:
            return await self.show(owner,'Сканер ещё запускается.',[[b('← Главное меню','home')]],message_id)
        rows=scanner.db.execute('SELECT min(id) AS id,name FROM channels WHERE platform=? GROUP BY name ORDER BY name COLLATE NOCASE',(platform,)).fetchall()
        pages=max(1,(len(rows)+9)//10);page=max(0,min(page,pages-1))
        buttons=[[b('➕ Добавить канал',f'add:{kind}')]]
        for row in rows[page*10:page*10+10]:
            buttons.append([b('📺 '+row['name'],f"pick:{kind}:{row['id']}:{page}")])
        nav=[]
        if page: nav.append(b('← Назад',f'list:{kind}:{page-1}'))
        nav.append(b(f'{page+1} / {pages}',f'list:{kind}:{page}'))
        if page+1<pages: nav.append(b('Вперёд →',f'list:{kind}:{page+1}'))
        buttons.extend([nav,[b('⌂ Главное меню','home')]])
        await self.show(owner,f"{'✈️ Telegram' if kind=='tg' else '🟣 Twitch'} · {len(rows)} источников\nСтраница {page+1} / {pages}\n\nНажми на канал, чтобы убрать его из сканирования.",buttons,message_id)

    async def callback(self, cb):
        data=cb.get('data','')
        if not data.startswith('adm:'):return False
        owner=cb.get('from',{}).get('id');message=cb.get('message',{});chat=message.get('chat',{})
        from bot import APIError
        allowed=owner in self.owners and chat.get('id')==owner and chat.get('type')=='private'
        try:
            await self.bot.api('answerCallbackQuery', callback_query_id=cb['id'], **({} if allowed else {'text':'Нет доступа','show_alert':True}))
        except APIError:pass
        if not allowed:return True
        mid=message.get('message_id');parts=data.split(':');action=parts[1];b=self.button
        if action=='confirm':
            state=self.ui_state(owner)
            if state.get('mode')!='delete' or state.get('token')!=parts[-1]:
                await self.show(owner,'Эта кнопка уже использована или устарела.',[[b('⌂ Главное меню','home')]],mid)
                return True
            with self.db:
                # Negative IDs reserve a separate idempotency namespace for button operations.
                self.db.execute('INSERT OR IGNORE INTO admin_jobs(chat_id,message_id,action,platform,name) VALUES(?,?,?,?,?)',
                    (owner,-int(state['token'],16),'delete',state['platform'],state['name']))
                self.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(f'admin_ui:{owner}','{}'))
            self.signal()
            await self.show(owner,'⏳ Удаление принято. Сообщу результат после сохранения.',[[b('📋 К списку',f"list:{state['kind']}:{state['page']}"),b('🕘 Операции','jobs')]],mid)
            return True
        self.set_ui(owner,{})
        if action=='home':await self.home(owner,mid)
        elif action=='subscriptions':await self.bot.menu(owner,mid)
        elif action=='list' and len(parts)==4 and parts[2] in ('tg','tw') and parts[3].isdigit():
            await self.listing(owner,parts[2],int(parts[3]),mid)
        elif action=='add' and len(parts)==3 and parts[2] in ('tg','tw'):
            kind=parts[2];self.set_ui(owner,{'mode':'add','kind':kind,'expires':time.time()+900})
            await self.show(owner,('✈️ Новый Telegram-канал' if kind=='tg' else '🟣 Новый Twitch-канал')+'\n\nПришли следующим сообщением ссылку или @username.\n'+('Публичный канал; сканер подпишется своим аккаунтом.' if kind=='tg' else 'Канал включится при следующем выборе чатов, обычно до 7 минут.'),[[b('Отмена','home')]],mid)
        elif action=='pick' and len(parts)==5 and parts[2] in ('tg','tw') and parts[3].isdigit() and parts[4].isdigit():
            kind=parts[2];platform={'tg':'TELEGRAM','tw':'TWITCH'}[kind];scanner=self.app.get('scanner_store')
            row=scanner.db.execute('SELECT name FROM channels WHERE id=? AND platform=?',(int(parts[3]),platform)).fetchone() if scanner else None
            if not row:
                await self.listing(owner,kind,int(parts[4]),mid)
                return True
            try:name=normalize_channel(row['name'],platform)
            except ValueError:
                await self.show(owner,'У этого старого источника нестандартный адрес. Удаление кнопкой пока недоступно.',[[b('← Назад',f'list:{kind}:{parts[4]}')]],mid)
                return True
            token=secrets.token_hex(6)
            self.set_ui(owner,{'mode':'delete','token':token,'name':name,'platform':platform,'kind':kind,'page':int(parts[4])})
            await self.show(owner,f'Убрать {name} из сканирования?\n\n'+('Аккаунт останется подписан на Telegram-канал.' if kind=='tg' else 'Изменение применится при следующем выборе чатов.'),[[b('🗑 Да, убрать','confirm:'+token)],[b('← Отмена',f'list:{kind}:{parts[4]}')]],mid)
        elif action in ('status','jobs'):
            await self.handle({'from':{'id':owner},'chat':chat,'message_id':mid,'text':'/'+action}, panel_id=mid)
        else:await self.home(owner,mid)
        return True

    async def handle(self, message, panel_id=None):
        text = message.get('text') or ''
        parts = text.split()
        cmd = parts[0].split('@')[0].lower() if parts else ''
        chat = message.get('chat', {})
        sender = message.get('from', {}).get('id')
        if chat.get('type') != 'private' or sender not in self.owners or chat.get('id') != sender:
            return cmd in COMMANDS
        state=self.ui_state(sender)
        if cmd == '/admin':
            await self.home(sender, panel_id)
            return True
        if cmd not in COMMANDS:
            if text.startswith('/'):
                self.set_ui(sender,{})
                return False
            if state.get('mode')!='add':return False
            if state.get('expires',0)<time.time():
                await self.home(sender)
                return True
            cmd='/addtg' if state['kind']=='tg' else '/addtwitch'
            parts=[cmd,text.strip()]
        reply = HELP
        if cmd == '/status':
            s = self.app.get('scanner_status', {})
            age = int(time.time() - s['checkpoint_at']) if s.get('checkpoint_at') else None
            pending = self.db.execute("SELECT count(*) FROM deliveries WHERE state='pending'").fetchone()[0]
            twitch_state = {'joining':'подключаю чаты', 'waiting_limit':'ожидание освобождения лимита', 'limited':'часть чатов недоступна из-за лимита', 'connected':'читает чаты', 'connecting':'подключается', 'reconnecting':'восстанавливает связь'}.get(s.get('twitch'), s.get('twitch', 'не запущен'))
            if s.get('twitch_retry_at'):
                twitch_state += f" (повтор через {max(0,int(s['twitch_retry_at']-time.time()))} сек.)"
            reply = (f"📊 Telegram: {s.get('telegram', 'не запущен')}\n"
                     f"Twitch: {twitch_state} — {s.get('twitch_channels', 0)}/{s.get('twitch_total', 0)} чатов\n"
                     f"VK: {s.get('vk', 'не запущен')}\n"
                     f"Снимок сканера: {str(age) + ' сек. назад' if age is not None else 'нет данных'}\n"
                     f"Сохранение бота: {'OK' if self.app.get('checkpoint_healthy') else 'нет подтверждения'}\n"
                     f"Очередь личной рассылки: {pending}\n/status не доказывает доставку каждого поста.")
        elif cmd == '/jobs':
            rows = self.db.execute('SELECT * FROM admin_jobs ORDER BY id DESC LIMIT 10').fetchall()
            states={'pending':'⏳ В очереди','notify':'📨 Отправляю результат','done':'✅ Завершено'}
            reply = '🕘 Последние операции\n\n' + ('\n\n'.join(f"#{r['id']} · {'Добавление' if r['action']=='add' else 'Удаление'} · {r['name']}\n{states.get(r['state'],r['state'])}\n{r['result']}" for r in rows) or 'Операций пока нет.')
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
                self.set_ui(sender,{})
                reply = '⏳ Операция принята. Сообщу результат после сохранения.'
            except ValueError as exc:
                reply = str(exc)
        b=self.button
        rows=[[b('⌂ Главное меню','home')]]
        if cmd in ('/status','/jobs'):
            rows.insert(0,[b('🔄 Обновить',cmd[1:])])
        elif state.get('mode')=='add':
            rows.insert(0,[b('➕ Ещё канал','add:'+state['kind']),b('📋 К списку','list:'+state['kind']+':0')])
        await self.show(sender,reply,rows,panel_id)
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
                    await self.show(row['chat_id'],result,[[self.button('📋 К списку','list:'+('tg' if row['platform']=='TELEGRAM' else 'tw')+':0'),self.button('⌂ Главное меню','home')]])
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
