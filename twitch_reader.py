"""One authenticated IRC connection, at most 90 joined channels; live-first rotation."""
import asyncio
import logging
import time
from types import SimpleNamespace
import aiohttp

log = logging.getLogger('twitch-reader')
MAX_CHANNELS = 90

def choose_channels(channels, live, turn, limit=MAX_CHANNELS):
    live = sorted(set(channels) & set(live))
    offline = sorted(set(channels)-set(live))
    def rotate(items):
        if not items:
            return []
        offset = (turn*limit) % len(items)
        return items[offset:]+items[:offset]
    return (rotate(live)+rotate(offline))[:limit]

def parse_line(line):
    if line.startswith('@'):
        line = line.split(' ',1)[1]
    prefix = ''
    if line.startswith(':'):
        prefix, line = line[1:].split(' ',1)
    head, _, tail = line.partition(' :')
    parts = head.split()
    return prefix, parts[0] if parts else '', parts[1:], tail

class Reader:
    def __init__(self, scanner, token, status):
        self.scanner, self.token, self.status = scanner, token, status
        self.scanner.all_channels = sorted({n.strip().lower().lstrip('#') for n in scanner.all_channels})
        self.unavailable = set()
        self.joined, self.pending = set(), set()
        self.nick = None
        self.turn = 0
        self.ws = None
        self.ready = asyncio.Event()
        self.queue = asyncio.Queue(maxsize=10000)
        self.blocked_until = 0
        self.limit_failures = 0

    def report(self):
        self.status.update(twitch_channels=len(self.joined), twitch_pending=len(self.pending),
                           twitch_total=len(self.scanner.all_channels), twitch_limit=MAX_CHANNELS,
                           twitch_unmonitored=len(set(self.scanner.all_channels)-self.joined),
                           twitch_policy='live-first; remaining slots rotate every 5 minutes')

    def limit_reached(self, channel):
        self.pending.discard(channel)
        self.limit_failures += 1
        self.blocked_until = time.time() + min(300, 30 * 2 ** min(self.limit_failures - 1, 4))
        self.status['twitch_last_notice'] = 'concurrent_channel_limit_reached'
        self.status['twitch_retry_at'] = int(self.blocked_until)
        self.status['twitch'] = 'waiting_limit' if not self.joined else 'limited'
        self.report()

    def join_confirmed(self, channel):
        self.pending.discard(channel)
        self.joined.add(channel)
        self.limit_failures = 0
        self.blocked_until = 0
        self.status['twitch'] = 'connected'
        self.status.pop('twitch_last_notice', None)
        self.status.pop('twitch_retry_at', None)
        self.report()

    async def live_channels(self, session):
        live = set()
        query = 'query($login:String!){user(login:$login){stream{id}}}'
        for i in range(0,len(self.scanner.all_channels),30):
            batch = self.scanner.all_channels[i:i+30]
            async with session.post('https://gql.twitch.tv/gql',headers={'Client-Id':'kimne78kx3ncx6brgo4mv6wki5h1ko'},
                                    json=[{'query':query,'variables':{'login':n}} for n in batch]) as response:
                response.raise_for_status()
                data = await response.json()
            if not isinstance(data,list) or len(data)!=len(batch):
                raise ValueError('Invalid Twitch channel response')
            for name,item in zip(batch,data):
                if item.get('errors'):
                    raise ValueError('Twitch channel query failed')
                user = (item.get('data') or {}).get('user')
                if user is None:
                    self.unavailable.add(name)
                if (user or {}).get('stream'):
                    live.add(name)
        return live

    async def reconcile(self, session):
        await self.ready.wait()
        while True:
            try:
                if self.blocked_until > time.time():
                    await asyncio.sleep(self.blocked_until-time.time())
                if hasattr(self.scanner, 'refresh_sources'):
                    self.scanner.refresh_sources()
                live = await self.live_channels(session)
                self.status['twitch_live_total'] = len(live)
                target = set(choose_channels(set(self.scanner.all_channels)-self.unavailable,live,self.turn))
                self.status['twitch_unavailable'] = sorted(self.unavailable)
                self.turn += 1
                for channel in sorted(self.joined-target):
                    await self.ws.send_str('PART #'+channel+'\r\n')
                # Wait for acknowledged PART before requesting replacement joins.
                for _ in range(30):
                    if not self.joined-target:
                        break
                    await asyncio.sleep(1)
                for channel in sorted(target-self.joined, key=lambda name: (name not in live, name)):
                    if len(self.joined|self.pending)>=MAX_CHANNELS:
                        break
                    if time.time()<self.blocked_until:
                        break
                    self.pending.add(channel)
                    await self.ws.send_str('JOIN #'+channel+'\r\n')
                    self.report()
                    await asyncio.sleep(.65)  # Under 20 JOIN attempts / 10 seconds.
                await asyncio.sleep(20)
                if self.pending:
                    self.status['twitch_last_unconfirmed'] = sorted(self.pending)
                    self.pending.clear()
                self.report()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Twitch selection retry: %s',type(exc).__name__)
            delay = max(1, self.blocked_until-time.time()) if self.blocked_until else (30 if not self.joined else 300)
            await asyncio.sleep(delay)

    async def consume(self):
        while True:
            channel, text = await self.queue.get()
            try:
                await self.scanner.event_message(SimpleNamespace(echo=False,content=text,channel=SimpleNamespace(name=channel)))
            except Exception as exc:
                log.warning('Twitch message handler: %s',type(exc).__name__)
            finally:
                self.queue.task_done()

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get('https://id.twitch.tv/oauth2/validate',headers={'Authorization':'OAuth '+self.token.removeprefix('oauth:')}) as response:
                response.raise_for_status()
                self.nick = (await response.json())['login'].lower()
            consumer = asyncio.create_task(self.consume())
            try:
                while True:
                    selector = None
                    self.joined.clear(); self.pending.clear(); self.ready.clear()
                    self.status['twitch'] = 'connecting'; self.report()
                    try:
                        async with session.ws_connect('wss://irc-ws.chat.twitch.tv:443',heartbeat=30) as ws:
                            self.ws = ws
                            await ws.send_str('CAP REQ :twitch.tv/membership twitch.tv/tags twitch.tv/commands\r\n')
                            await ws.send_str('PASS '+self.token+'\r\n')
                            await ws.send_str('NICK '+self.nick+'\r\n')
                            selector = asyncio.create_task(self.reconcile(session))
                            async for message in ws:
                                if message.type!=aiohttp.WSMsgType.TEXT:
                                    continue
                                for line in message.data.splitlines():
                                    if not line.strip():continue
                                    prefix, command, args, tail = parse_line(line)
                                    if command=='PING':
                                        await ws.send_str('PONG :'+tail+'\r\n')
                                    elif command=='001':
                                        self.status['twitch']='joining';self.ready.set()
                                    elif command in ('JOIN','PART') and prefix.split('!')[0].lower()==self.nick:
                                        channel=(args[0] if args else tail).lstrip('#').lower()
                                        self.pending.discard(channel)
                                        if command=='JOIN':self.join_confirmed(channel)
                                        else:self.joined.discard(channel)
                                        self.report()
                                    elif command=='NOTICE':
                                        if 'permanently banned' in tail.lower() and args:
                                            channel=args[0].lstrip('#').lower()
                                            self.unavailable.add(channel)
                                            self.pending.discard(channel)
                                            self.status['twitch_unavailable']=sorted(self.unavailable)
                                        if 'msg_concurrent_channel_limit_reached' in line:
                                            self.limit_reached(args[0].lstrip('#').lower() if args else '')
                                        log.warning('Twitch notice: %s',tail[:200])
                                    elif command=='RECONNECT':
                                        await ws.close()
                                    elif command=='PRIVMSG' and args:
                                        channel=args[0].lstrip('#').lower()
                                        self.status['twitch_last_message_at']=int(time.time())
                                        self.status['twitch_messages_received']=self.status.get('twitch_messages_received',0)+1
                                        self.queue.put_nowait((channel,tail))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning('Twitch reconnect: %s',type(exc).__name__)
                    finally:
                        if selector:
                            selector.cancel();await asyncio.gather(selector,return_exceptions=True)
                        self.joined.clear();self.pending.clear();self.report()
                        self.status['twitch']='reconnecting'
                    await asyncio.sleep(10)
            finally:
                consumer.cancel();await asyncio.gather(consumer,return_exceptions=True)
