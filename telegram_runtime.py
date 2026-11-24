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
