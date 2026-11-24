"""Lifecycle and encrypted database checkpoints for the headless scanner."""
import asyncio
import logging
import os
import time
from pathlib import Path
from bot import Store

log = logging.getLogger('scanner')

class StatusQueue:
    def __init__(self, status):
        self.status = status

    def put(self, text):
        if '[TG STATE]' in text:
            self.status['telegram'] = 'online' if 'ONLINE' in text else 'reconnecting' if 'RECONNECT' in text else 'unauthorized'
            log.info('Scanner Telegram: %s', self.status['telegram'])
        elif '[TWITCH] IRC' in text:
            self.status['twitch'] = 'connected'
            log.info('Scanner Twitch connected')
        elif '[TWITCH] Подключено' in text:
            import re
            counts = re.search(r'(\d+)/(\d+)', text)
            if counts:
                self.status['twitch_channels'] = int(counts[1])
                self.status['twitch_total'] = int(counts[2])
        elif '[VK HEALTH]' in text:
            self.status['vk'] = 'online'
            self.status['vk_checked_at'] = int(time.time())
        elif '[VK] Инициализация' in text:
            self.status['vk'] = 'starting'
        elif '[ОШИБКА' in text or 'ОШИБКА]' in text:
            self.status['last_error_at'] = int(time.time())
            log.warning('Scanner reported an error; check source connectivity')

async def start(app):
    from web import Checkpoints
    directory = Path(os.getenv('SCANNER_DATA_DIR', '/tmp/halyava'))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/'scanner.sqlite3'
    checkpoint = Checkpoints(app['session'], 'scanner.enc')
    for suffix in ('-wal','-shm'):
        Path(str(path)+suffix).unlink(missing_ok=True)
    await checkpoint.restore(path)
    if not path.exists():
        raise RuntimeError('Migrated scanner database is required')
    import scanner_cloud as scanner
    scanner.init_db()
    scanner.load_seen_promos_from_db()
    store = Store(path)
    app['scanner_store'] = store
    app['scanner_checkpoint'] = checkpoint
    status = {'enabled': True, 'telegram': 'starting', 'twitch': 'starting', 'vk': 'starting',
              'twitch_total': len(scanner.get_channels('TWITCH')),
              'telegram_total': len(scanner.get_channels('TELEGRAM')),
              'vk_total': len(scanner.get_channels('VK'))}
    app['scanner_status'] = status
    queue = StatusQueue(status)

    async def run():
        while True:
            try:
                await scanner.start_scanners(queue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status['last_error_at'] = int(time.time())
                log.warning('Scanner restarting after %s', type(exc).__name__)
            await asyncio.sleep(10)

    async def persist():
        while True:
            try:
                await checkpoint.save(store)
                status['checkpoint_at'] = int(time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Scanner checkpoint retry: %s', type(exc).__name__)
            await asyncio.sleep(30)

    app['scanner_runner'] = asyncio.create_task(run())
    app['scanner_saver'] = asyncio.create_task(persist())

async def stop(app):
    for name in ('scanner_runner','scanner_saver'):
        app[name].cancel()
    await asyncio.gather(app['scanner_runner'],app['scanner_saver'],return_exceptions=True)
    try:
        await app['scanner_checkpoint'].save(app['scanner_store'])
    finally:
        app['scanner_store'].db.close()
