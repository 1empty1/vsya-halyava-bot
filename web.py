"""Render Free webhook with encrypted remote SQLite checkpoints."""
import asyncio
import hmac
import logging
import os
import sqlite3
import threading
import zlib
import uuid
from pathlib import Path
import aiohttp
import psycopg2
from aiohttp import web
from cryptography.fernet import Fernet
from bot import Bot, Store, APIError
log = logging.getLogger('halyava')

class Checkpoints:
    def __init__(self, database, name='bot'):
        self.database = database
        self.name = name
        self.cipher = Fernet(os.environ['STATE_ENCRYPTION_KEY'].encode())
        self.lock = asyncio.Lock()
        self.saved_changes = -1

    async def restore(self, path):
        encrypted = await asyncio.to_thread(self.database.load, self.name)
        if encrypted is None:
            raise RuntimeError('Required migrated state is missing: '+self.name)
        raw = self.cipher.decrypt(encrypted)
        if raw.startswith(b'Z'):
            raw = zlib.decompress(raw[1:])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    async def save(self, store):
        async with self.lock:
            changes = (store.db.total_changes, store.db.execute('PRAGMA data_version').fetchone()[0])
            if changes == self.saved_changes:
                return
            snapshot = sqlite3.connect(':memory:')
            try:
                store.db.backup(snapshot)
                snapshot.execute('PRAGMA journal_mode=DELETE')
                encrypted = self.cipher.encrypt(b'Z' + zlib.compress(snapshot.serialize(), 6))
            finally:
                snapshot.close()
            await asyncio.to_thread(self.database.write, self.name, encrypted)
            self.saved_changes = changes


class SnapshotDatabase:
    """Atomic encrypted snapshots in PostgreSQL, off the event loop."""
    def __init__(self, database_url):
        self.database_url = database_url
        self.lock = threading.RLock()

    def _connect(self):
        return psycopg2.connect(self.database_url, sslmode='require', connect_timeout=10,
                               options='-c statement_timeout=15000',
                               application_name='halyava-bot')

    def create_table(self):
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute("CREATE TABLE IF NOT EXISTS halyava_state (name TEXT PRIMARY KEY, payload BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                cursor.execute("ALTER TABLE halyava_state ENABLE ROW LEVEL SECURITY")
        finally:
            connection.close()

    def load(self, name):
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute('SELECT payload FROM halyava_state WHERE name=%s', (name,))
                row = cursor.fetchone()
                return bytes(row[0]) if row else None
        finally:
            connection.close()

    def write(self, name, encrypted):
        with self.lock:
            connection = self._connect()
            try:
                with connection, connection.cursor() as cursor:
                    cursor.execute('INSERT INTO halyava_state(name,payload) VALUES(%s,%s) ON CONFLICT(name) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()',
                                   (name, psycopg2.Binary(encrypted)))
            finally:
                connection.close()

    def close(self):
        pass

async def startup(app):
    app['session'] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35))
    app['snapshot_db'] = SnapshotDatabase(os.environ['DATABASE_URL'])
    await asyncio.to_thread(app['snapshot_db'].create_table)
    from migrate_state import migrate
    await asyncio.to_thread(migrate, app['snapshot_db'])
    checkpoint = Checkpoints(app['snapshot_db'], 'bot')
    app['checkpoint'] = checkpoint
    path = Path(os.getenv('DATA_PATH', '/tmp/halyava/bot.sqlite3'))
    for suffix in ('-wal', '-shm'):
        Path(str(path)+suffix).unlink(missing_ok=True)
    await checkpoint.restore(path)
    store = Store(path)
    app['store'] = store
    bot = Bot(os.environ['BOT_TOKEN'], store, int(os.environ['SOURCE_CHAT_ID']), [int(x) for x in os.environ['SCANNER_SENDER_IDS'].split(',')])
    bot.session = app['session']
    me = await bot.api('getMe')
    if me['username'].lower() != os.getenv('EXPECTED_BOT_USERNAME', 'vsyaHalyava_bot').lower():
        raise RuntimeError('Unexpected bot identity')
    app['bot'] = bot
    app['lock'] = asyncio.Lock()
    app['persist_event'] = asyncio.Event()
    app['checkpoint_healthy'] = True
    app['healthy'] = True
    checkpoint.saved_changes = (store.db.total_changes, store.db.execute('PRAGMA data_version').fetchone()[0])
    app['worker'] = asyncio.create_task(deliver(app))
    app['persist_worker'] = asyncio.create_task(persist_bot_state(app))
    if os.getenv('ENABLE_SCANNER', '0') == '1':
        from scanner_host import start
        await start(app)
    from admin import Admin
    bot.admin = Admin(bot, app, {int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()})
    app['admin_worker'] = asyncio.create_task(bot.admin.worker())
    log.info('Webhook bot ready: @%s', me['username'])

async def webhook(request):
    if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), os.environ['WEBHOOK_SECRET']):
        raise web.HTTPUnauthorized()
    try:
        update = await request.json()
        if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
            raise web.HTTPBadRequest()
        app = request.app
        async with app['lock']:
            store = app['store']
            if update['update_id'] >= int(store.get('offset')):
                await app['bot'].update(update)
                store.set('offset', update['update_id']+1)
                app['persist_event'].set()
            app['healthy'] = True
        return web.json_response({'ok': True})
    except web.HTTPException:
        raise
    except Exception as exc:
        log.warning('Webhook retry needed: %s', type(exc).__name__)
        request.app['healthy'] = False
        raise web.HTTPServiceUnavailable()

async def persist_bot_state(app):
    while True:
        await app['persist_event'].wait()
        app['persist_event'].clear()
        try:
            await app['checkpoint'].save(app['store'])
            app['checkpoint_healthy'] = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app['checkpoint_healthy'] = False
            log.warning('Bot state save will retry: %s', type(exc).__name__)
            await asyncio.sleep(5)
            app['persist_event'].set()

async def deliver(app):
    while True:
        try:
            async with app['lock']:
                store = app['store']
                for row in store.pending()[:10]:
                    if not store.still_wanted(row):
                        store.finish(row['id'], 'cancelled')
                        continue
                    try:
                        await app['bot'].api('copyMessage', chat_id=row['user_id'], from_chat_id=row['chat_id'], message_id=row['message_id'])
                        store.finish(row['id'], 'sent')
                    except APIError as exc:
                        if exc.code == 403:
                            store.stop(row['user_id'])
                        elif exc.code == 400:
                            store.finish(row['id'], 'failed')
                            log.warning('Source post unavailable for copying')
                        else:
                            store.retry(row['id'], exc.retry_after or 30)
                            if exc.code == 429:
                                break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        store.retry(row['id'], 30)
                    await asyncio.sleep(.1)
                if store.db.total_changes:
                    app['persist_event'].set()
                app['healthy'] = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app['healthy'] = False
            log.warning('Delivery will retry: %s', type(exc).__name__)
        await asyncio.sleep(3)

async def health(request):
    healthy = request.app.get('healthy', False) and request.app.get('checkpoint_healthy', True)
    return web.json_response({'status': 'ok' if healthy else 'retrying'}, status=200 if healthy else 503)

async def cleanup(app):
    if app.get('admin_worker'):
        app['admin_worker'].cancel()
        await asyncio.gather(app['admin_worker'], return_exceptions=True)
    if 'scanner_runner' in app:
        from scanner_host import stop
        await stop(app)
    worker = app.get('worker')
    if worker:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    persist_worker = app.get('persist_worker')
    if persist_worker:
        persist_worker.cancel()
        await asyncio.gather(persist_worker, return_exceptions=True)
    if 'store' in app:
        try:
            await app['checkpoint'].save(app['store'])
        finally:
            app['store'].db.close()
    await app['session'].close()
    if 'snapshot_db' in app:
        await asyncio.to_thread(app['snapshot_db'].close)

def make_app():
    app = web.Application(client_max_size=1024*1024)
    app.router.add_get('/healthz', health)
    app.router.add_post('/telegram', webhook)
    async def scanner_status(request):
        if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),os.environ['WEBHOOK_SECRET']):
            raise web.HTTPUnauthorized()
        return web.json_response(request.app.get('scanner_status', {'enabled': False}))
    app.router.add_get('/scanner-status', scanner_status)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    web.run_app(make_app(), host='0.0.0.0', port=int(os.getenv('PORT', '10000')), access_log=None)
