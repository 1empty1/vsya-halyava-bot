"""Render Free webhook with encrypted remote SQLite checkpoints."""
import asyncio
import base64
import hmac
import logging
import os
import sqlite3
from pathlib import Path
import aiohttp
from aiohttp import web
from cryptography.fernet import Fernet
from bot import Bot, Store, APIError
log = logging.getLogger('halyava')

class Checkpoints:
    def __init__(self, session, filename='state.enc'):
        self.session = session
        self.repo = os.environ['STATE_REPO']
        self.headers = {'Authorization': 'Bearer '+os.environ['GITHUB_TOKEN'], 'Accept': 'application/vnd.github+json'}
        self.cipher = Fernet(os.environ['STATE_ENCRYPTION_KEY'].encode())
        self.sha = None
        self.saved_changes = -1
        self.url = 'https://api.github.com/repos/'+self.repo+'/contents/'+filename

    async def restore(self, path):
        async with self.session.get(self.url, headers=self.headers) as response:
            if response.status == 404:
                async with self.session.get('https://api.github.com/repos/'+self.repo, headers=self.headers) as check:
                    if check.status != 200:
                        raise RuntimeError('State repository is inaccessible')
                return
            if response.status != 200:
                raise RuntimeError('Cannot load checkpoint: HTTP '+str(response.status))
            item = await response.json()
        self.sha = item['sha']
        if item.get('encoding') == 'base64':
            encrypted = base64.b64decode(item['content'])
        else:
            async with self.session.get('https://api.github.com/repos/'+self.repo+'/git/blobs/'+self.sha, headers=self.headers) as response:
                if response.status != 200:
                    raise RuntimeError('Cannot load checkpoint blob')
                blob = await response.json()
                encrypted = base64.b64decode(blob['content'])
        raw = self.cipher.decrypt(encrypted)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    async def save(self, store):
        changes = (store.db.total_changes, store.db.execute('PRAGMA data_version').fetchone()[0])
        if changes == self.saved_changes:
            return
        snapshot = sqlite3.connect(':memory:')
        try:
            store.db.backup(snapshot)
            snapshot.execute('PRAGMA journal_mode=DELETE')
            encrypted = self.cipher.encrypt(snapshot.serialize())
        finally:
            snapshot.close()
        body = {'message': 'Save encrypted bot state', 'content': base64.b64encode(encrypted).decode()}
        if self.sha:
            body['sha'] = self.sha
        async with self.session.put(self.url, headers=self.headers, json=body) as response:
            if response.status not in (200, 201):
                raise RuntimeError('Cannot persist checkpoint: HTTP '+str(response.status))
            result = await response.json()
        self.sha = result['content']['sha']
        self.saved_changes = changes

async def startup(app):
    app['session'] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35))
    checkpoint = Checkpoints(app['session'])
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
    app['healthy'] = True
    await checkpoint.save(store)
    app['worker'] = asyncio.create_task(deliver(app))
    if os.getenv('ENABLE_SCANNER', '0') == '1':
        from scanner_host import start
        await start(app)
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
            await app['checkpoint'].save(store)
            app['healthy'] = True
        return web.json_response({'ok': True})
    except web.HTTPException:
        raise
    except Exception as exc:
        log.warning('Webhook retry needed: %s', type(exc).__name__)
        request.app['healthy'] = False
        raise web.HTTPServiceUnavailable()

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
                await app['checkpoint'].save(store)
                app['healthy'] = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app['healthy'] = False
            log.warning('Delivery will retry: %s', type(exc).__name__)
        await asyncio.sleep(3)

async def health(request):
    healthy = request.app.get('healthy', False)
    return web.json_response({'status': 'ok' if healthy else 'retrying'}, status=200 if healthy else 503)

async def cleanup(app):
    if 'scanner_runner' in app:
        from scanner_host import stop
        await stop(app)
    worker = app.get('worker')
    if worker:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    if 'store' in app:
        try:
            await app['checkpoint'].save(app['store'])
        finally:
            app['store'].db.close()
    await app['session'].close()

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
