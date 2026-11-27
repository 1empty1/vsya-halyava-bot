"""Connection supervision and timing for the desktop Telegram scanner."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, errors
from telethon.tl.functions.updates import GetStateRequest


def setup_timing_log(path):
    logger = logging.getLogger('scanner.telegram.timing')
    if not logger.handlers:
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def event_age(message, now=None):
    stamp = getattr(message, 'edit_date', None) or getattr(message, 'date', None)
    if stamp is None:
        return None
    return max(0, ((now or datetime.now(timezone.utc)) - stamp).total_seconds())


def telegram_status(message):
    for marker, state in [('ONLINE', 'online'), ('RECONNECT', 'reconnect'), ('UNAUTHORIZED', 'unauthorized')]:
        if f'[TG STATE] {marker}' in message:
            return state
    return None


class TimedTelegramClient(TelegramClient):
    """Measure actual sends without changing Telegram retry/deduplication semantics."""
    async def send_message(self, *args, **kwargs):
        started = time.monotonic()
        logger = logging.getLogger('scanner.telegram.timing')
        try:
            result = await super().send_message(*args, **kwargs)
        except BaseException as exc:
            logger.info('SEND failed=%s elapsed=%.3fs', type(exc).__name__, time.monotonic()-started)
            raise
        logger.info('SEND ok id=%s elapsed=%.3fs', result.id, time.monotonic()-started)
        return result


async def supervise_telegram(client, queue, *, interval=10, probe_timeout=8,
                             connect_timeout=20, retry_delay=2):
    """Check a real RPC response, not just whether the local socket is open.

    Idle channels are healthy: only a failed RPC triggers reconnection.
    Keep the same client/session/event handlers across reconnects.
    """
    logger = logging.getLogger('scanner.telegram.timing')
    async def connect():
        await client.connect()
        return await client.get_me()

    while True:
        try:
            me = await asyncio.wait_for(connect(), connect_timeout)
            if me is None:
                queue.put('[TG STATE] UNAUTHORIZED — НЕТ АВТОРИЗАЦИИ Telegram. Нужен вход в аккаунт.\n')
                return
            queue.put('[TG STATE] ONLINE — Telegram подключён, связь проверена.\n')
            logger.info('CONNECTED')
            while True:
                started = time.monotonic()
                try:
                    await asyncio.wait_for(client(GetStateRequest()), probe_timeout)
                except errors.FloodWaitError as exc:
                    logger.info('HEALTH flood_wait=%s', exc.seconds)
                    await asyncio.sleep(exc.seconds)
                    continue
                logger.info('HEALTH rpc=%.3fs', time.monotonic()-started)
                await asyncio.sleep(interval)
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError):
            queue.put('[TG STATE] UNAUTHORIZED — НЕТ АВТОРИЗАЦИИ Telegram. Нужен вход в аккаунт.\n')
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            queue.put(f'[TG STATE] RECONNECT — Нет ответа Telegram ({type(exc).__name__}); восстанавливаю связь.\n')
            logger.info('RECONNECT reason=%s', type(exc).__name__)
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except (Exception, asyncio.CancelledError):
                pass
        await asyncio.sleep(retry_delay)


class ChatResolver:
    """Reuse chat entities; retry lookup failures without discarding the event."""
    def __init__(self, retry_delay=10, timeout=12):
        self.cache = {}
        self.locks = {}
        self.slots = asyncio.Semaphore(2)
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.log = logging.getLogger('scanner.telegram.lookup')

    async def resolve(self, event):
        key = event.chat_id
        if key is None:
            return None
        chat = event.chat
        if chat is not None:
            self.cache[key] = chat
            return chat
        if key in self.cache:
            return self.cache[key]
        async with self.locks.setdefault(key, asyncio.Lock()):
            attempt = 0
            while True:
                if key in self.cache:
                    return self.cache[key]
                try:
                    async with self.slots:
                        # A background warm-up or another event may have resolved it.
                        if key in self.cache:
                            return self.cache[key]
                        async with asyncio.timeout(self.timeout):
                            chat = await event.get_chat()
                    if chat is not None:
                        self.cache[key] = chat
                        return chat
                    raise ValueError('Chat entity not available yet')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    attempt += 1
                    delay = max(self.retry_delay, getattr(exc, 'seconds', 0))
                    self.log.warning('Chat lookup deferred chat=%s attempt=%s reason=%s; event retained', key, attempt, type(exc).__name__)
                    await asyncio.sleep(delay)

    async def warm(self, client):
        """Populate in the background; a failing dialog request never blocks known posts."""
        while True:
            delay = 600
            try:
                if not client.is_connected():
                    await asyncio.sleep(2)
                    continue
                async for dialog in client.iter_dialogs():
                    if dialog.entity is not None:
                        self.cache[dialog.id] = dialog.entity
                self.log.info('Telegram chat cache ready: %s entities', len(self.cache))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = max(30, getattr(exc, 'seconds', 0))
                self.log.warning('Chat cache refresh will retry: %s', type(exc).__name__)
            await asyncio.sleep(delay)
