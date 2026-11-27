import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telegram_runtime import ChatResolver

class LookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedded_chat_needs_no_request(self):
        resolver=ChatResolver(retry_delay=0)
        chat=SimpleNamespace(id=123,username='source')
        event=SimpleNamespace(chat_id=-1000000000123,chat=chat,get_chat=AsyncMock())
        self.assertIs(await resolver.resolve(event),chat)
        event.get_chat.assert_not_awaited()
        missing=SimpleNamespace(chat_id=event.chat_id,chat=None,get_chat=AsyncMock(side_effect=ValueError('RPC failed')))
        self.assertIs(await resolver.resolve(missing),chat)
        missing.get_chat.assert_not_awaited()

    async def test_rpc_failure_retries_same_event(self):
        resolver=ChatResolver(retry_delay=0)
        chat=SimpleNamespace(id=123)
        event=SimpleNamespace(chat_id=123,chat=None,get_chat=AsyncMock(side_effect=[ValueError('Request unsuccessful'),chat]))
        self.assertIs(await resolver.resolve(event),chat)
        self.assertEqual(event.get_chat.await_count,2)

    async def test_one_lookup_for_simultaneous_same_channel(self):
        resolver=ChatResolver(retry_delay=0)
        chat=SimpleNamespace(id=123)
        async def fetch():
            await asyncio.sleep(.01)
            return chat
        event=SimpleNamespace(chat_id=123,chat=None,get_chat=AsyncMock(side_effect=fetch))
        values=await asyncio.gather(resolver.resolve(event),resolver.resolve(event))
        self.assertEqual(values,[chat,chat])
        self.assertEqual(event.get_chat.await_count,1)

    async def test_cancellation_stops_retry(self):
        resolver=ChatResolver(retry_delay=60)
        event=SimpleNamespace(chat_id=123,chat=None,get_chat=AsyncMock(side_effect=ValueError()))
        task=asyncio.create_task(resolver.resolve(event))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
