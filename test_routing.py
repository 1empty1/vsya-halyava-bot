import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from bot import Bot, Store


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'bot.db')
        self.bot = Bot('test-token', self.store, -100, [123])
        self.bot.api = AsyncMock(return_value={})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def post(self, chat=-100, sender=123, text=None):
        return {'message': {'message_id': 5, 'chat': {'id': chat, 'type': 'supergroup'},
                            'from': {'id': sender}, 'text': text or '🎁 РОЗЫГРЫШ | TELEGRAM\n🎯 BetBoom\nhttps://betboom.ru/freestream/abc'}}

    async def test_source_and_sender(self):
        self.store.toggle(99, 'betboom')
        await self.bot.update(self.post(sender=456))
        await self.bot.update(self.post(chat=-200))
        await self.bot.update(self.post(text='BetBoom купон от пользователя'))
        self.assertEqual(self.store.pending(), [])
        await self.bot.update(self.post())
        self.assertEqual(len(self.store.pending()), 1)
        self.bot.api.assert_not_called()  # Never publish anything back into source group.

    async def test_start_does_not_opt_in(self):
        await self.bot.update({'message': {'message_id': 1, 'chat': {'id': 99, 'type': 'private'}, 'text': '/start'}})
        self.assertEqual(self.store.preferences(99), set())
        self.assertEqual(self.bot.api.call_args.kwargs['chat_id'], 99)

    async def test_callback_replay(self):
        update = {'callback_query': {'id': 'abc', 'from': {'id': 99}, 'data': 'toggle:caser',
                                     'message': {'message_id': 1, 'chat': {'id': 99, 'type': 'private'}}}}
        await self.bot.update(update)
        await self.bot.update(update)
        self.assertEqual(self.store.preferences(99), {'caser'})

    async def test_stop(self):
        self.store.all_on(99)
        await self.bot.update({'message': {'message_id': 1, 'chat': {'id': 99, 'type': 'private'}, 'text': '/stop'}})
        self.assertEqual(self.store.preferences(99), set())

    async def test_edited_post_not_resent(self):
        self.store.all_on(99)
        first = self.post()
        await self.bot.update(first)
        await self.bot.update({'edited_message': first['message']})
        self.assertEqual(len(self.store.pending()), 1)

    async def test_unknown_category_ignored(self):
        self.store.all_on(99)
        await self.bot.update(self.post(text='✈️ TELEGRAM | тест\nНичего не найдено'))
        self.assertEqual(self.store.pending(), [])


if __name__ == '__main__':
    unittest.main()
