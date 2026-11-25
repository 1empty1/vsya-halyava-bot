import asyncio
import os
import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

from bot import Store
from web import webhook
from web import Checkpoints, persist_bot_state
from cryptography.fernet import Fernet


class FakeRequest:
    def __init__(self, app, update):
        self.app = app
        self.headers = {'X-Telegram-Bot-Api-Secret-Token': 'test-secret'}
        self.update = update

    async def json(self):
        return self.update


class WebhookReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_save_does_not_hold_command_lock(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def save(store):
            entered.set()
            await release.wait()
        checkpoint = type('Checkpoint', (), {'save': staticmethod(save)})()
        app = {'lock': asyncio.Lock(), 'persist_event': asyncio.Event(),
               'checkpoint': checkpoint, 'store': None}
        app['persist_event'].set()
        task = asyncio.create_task(persist_bot_state(app))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(app['lock'].acquire(), .2)
            app['lock'].release()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_snapshot_restores_and_detects_other_sqlite_connections(self):
        class MemoryDatabase:
            def __init__(self): self.data = {}
            def write(self, name, encrypted): self.data[name] = encrypted
            def load(self, name): return self.data.get(name)
        old = os.environ.get('STATE_ENCRYPTION_KEY')
        os.environ['STATE_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source.db'
                store = Store(source)
                database = MemoryDatabase()
                checkpoint = Checkpoints(database, 'scanner')
                await checkpoint.save(store)
                other = sqlite3.connect(source)
                other.execute("INSERT INTO meta(key,value) VALUES('external','kept')")
                other.commit(); other.close()
                await checkpoint.save(store)
                target = Path(directory) / 'restored.db'
                await checkpoint.restore(target)
                restored = Store(target)
                self.assertEqual(restored.get('external'), 'kept')
                store.db.close(); restored.db.close()
        finally:
            if old is None: os.environ.pop('STATE_ENCRYPTION_KEY', None)
            else: os.environ['STATE_ENCRYPTION_KEY'] = old

    async def test_webhook_acknowledges_local_update_when_checkpoint_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'bot.sqlite3')
            bot = type('FakeBot', (), {'update': AsyncMock()})()
            checkpoint = type('FakeCheckpoint', (), {
                'save': AsyncMock(side_effect=RuntimeError('GitHub unavailable')),
            })()
            app = {
                'lock': asyncio.Lock(),
                'store': store,
                'bot': bot,
                'checkpoint': checkpoint,
                'healthy': True,
                'persist_event': asyncio.Event(),
            }
            old_secret = os.environ.get('WEBHOOK_SECRET')
            os.environ['WEBHOOK_SECRET'] = 'test-secret'
            try:
                result = await webhook(FakeRequest(app, {'update_id': 7, 'message': {}}))
                self.assertEqual(result.status, 200)
                self.assertEqual(store.get('offset'), '8')
                self.assertTrue(app['persist_event'].is_set())
                checkpoint.save.assert_not_awaited()
            finally:
                if old_secret is None:
                    os.environ.pop('WEBHOOK_SECRET', None)
                else:
                    os.environ['WEBHOOK_SECRET'] = old_secret
                store.db.close()


if __name__ == '__main__':
    unittest.main()
