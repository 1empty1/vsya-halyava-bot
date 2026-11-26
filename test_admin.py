import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from bot import Bot, Store
from admin import Admin, normalize_channel

class AdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'bot.db')
        self.bot = Bot('fake', self.store, -100, [1])
        self.bot.api = AsyncMock()
        self.admin = Admin(self.bot, {}, {875615377})
        self.bot.admin = self.admin
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
    async def test_non_admin_cannot_enqueue(self):
        await self.bot.update({'message': {'message_id': 1, 'from': {'id': 2}, 'chat': {'id': 2, 'type': 'private'}, 'text': '/addtg example'}})
        self.assertEqual(self.admin.jobs(), [])
    async def test_spoofed_sender_cannot_enqueue(self):
        await self.bot.update({'message': {'message_id': 1, 'from': {'id': 2}, 'chat': {'id': 875615377, 'type': 'private'}, 'text': '/addtg example'}})
        self.assertEqual(self.admin.jobs(), [])
    async def test_admin_queues_once_for_repeated_update(self):
        m={'message_id': 10, 'from': {'id': 875615377}, 'chat': {'id': 875615377, 'type': 'private'}, 'text': '/addtg https://t.me/Example'}
        await self.bot.update({'message':m}); await self.bot.update({'message':m})
        jobs=self.admin.jobs()
        self.assertEqual(len(jobs),1)
        self.assertEqual((jobs[0]['name'],jobs[0]['platform']),('example','TELEGRAM'))
    async def test_groups_cannot_admin(self):
        await self.bot.update({'message': {'message_id': 1, 'from': {'id': 875615377}, 'chat': {'id': -200, 'type': 'group'}, 'text': '/addtg example'}})
        self.assertEqual(self.admin.jobs(), [])
    async def test_invalid_input_not_queued(self):
        await self.bot.update({'message': {'message_id': 1, 'from': {'id': 875615377}, 'chat': {'id': 875615377, 'type': 'private'}, 'text': '/addtg https://evil.example/test'}})
        self.assertEqual(self.admin.jobs(), [])
    def test_normalization(self):
        self.assertEqual(normalize_channel('https://www.twitch.tv/Some_Name','TWITCH'),'some_name')
        self.assertEqual(normalize_channel('@Example','TELEGRAM'),'example')
        for value in ('https://t.me/+secret','https://t.me/example/99','https://t.me.evil/test','a b'):
            with self.assertRaises(ValueError): normalize_channel(value,'TELEGRAM')

    async def test_telegram_add_saved_and_idempotent(self):
        from types import SimpleNamespace
        scanner = Store(Path(self.tmp.name)/'scanner.db')
        scanner.db.execute('CREATE TABLE channels(id INTEGER PRIMARY KEY,name TEXT,platform TEXT)')
        client = AsyncMock()
        client.is_connected = lambda: True
        client.get_entity.return_value = SimpleNamespace(broadcast=True,username='Example')
        checkpoint = SimpleNamespace(save=AsyncMock())
        self.admin.app.update(scanner_store=scanner,scanner_telegram_client=client,scanner_checkpoint=checkpoint)
        row={'name':'example','platform':'TELEGRAM','action':'add'}
        await self.admin.apply(row)
        await self.admin.apply(row)
        self.assertEqual(scanner.db.execute('SELECT name FROM channels').fetchall()[0][0], 'example')
        self.assertEqual(scanner.db.execute('SELECT count(*) FROM channels').fetchone()[0],1)
        checkpoint.save.assert_awaited()
        scanner.db.close()

    async def test_delete_removes_legacy_forms_and_keeps_other_platform(self):
        scanner = Store(Path(self.tmp.name)/'scanner.db')
        scanner.db.execute('CREATE TABLE channels(id INTEGER PRIMARY KEY,name TEXT,platform TEXT)')
        scanner.db.executemany('INSERT INTO channels(name,platform) VALUES(?,?)', [('Example','TELEGRAM'),('https://t.me/example','TELEGRAM'),('example','TWITCH')])
        scanner.db.commit()
        from types import SimpleNamespace
        self.admin.app.update(scanner_store=scanner,scanner_checkpoint=SimpleNamespace(save=AsyncMock()))
        await self.admin.apply({'name':'example','platform':'TELEGRAM','action':'delete'})
        self.assertEqual([tuple(r) for r in scanner.db.execute('SELECT name,platform FROM channels')],[('example','TWITCH')])
        scanner.db.close()

    async def test_jobs_survive_restart(self):
        await self.bot.update({'message': {'message_id': 11, 'from': {'id': 875615377}, 'chat': {'id': 875615377, 'type': 'private'}, 'text': '/addtwitch example'}})
        reopened=Store(self.store.path)
        admin=Admin(Bot('fake',reopened,-100,[1]),{}, {875615377})
        self.assertEqual(admin.jobs()[0]['name'],'example')
        reopened.db.close()

    def callback(self, data, user=875615377):
        return {'id':'click1','from':{'id':user},'data':data,'message':{'message_id':50,'chat':{'id':user,'type':'private'}}}

    async def test_button_add_requires_owner_and_accepts_followup(self):
        await self.bot.update({'callback_query':self.callback('adm:add:tg',2)})
        self.assertEqual(self.admin.jobs(),[])
        await self.bot.update({'callback_query':self.callback('adm:add:tg')})
        await self.bot.update({'message':{'message_id':51,'from':{'id':875615377},'chat':{'id':875615377,'type':'private'},'text':'https://t.me/example'}})
        self.assertEqual(self.admin.jobs()[0]['name'],'example')

    async def test_cancel_stops_text_capture(self):
        await self.bot.update({'callback_query':self.callback('adm:add:tg')})
        await self.bot.update({'callback_query':self.callback('adm:home')})
        await self.bot.update({'message':{'message_id':51,'from':{'id':875615377},'chat':{'id':875615377,'type':'private'},'text':'example'}})
        self.assertEqual(self.admin.jobs(),[])

    async def test_pages_and_delete_confirmation(self):
        scanner=Store(Path(self.tmp.name)/'scanner.db')
        scanner.db.execute('CREATE TABLE channels(id INTEGER PRIMARY KEY,name TEXT,platform TEXT)')
        scanner.db.executemany('INSERT INTO channels(name,platform) VALUES(?,?)',[(f'channel{i:02}', 'TELEGRAM') for i in range(15)])
        scanner.db.commit();self.admin.app['scanner_store']=scanner
        await self.bot.update({'callback_query':self.callback('adm:list:tg:1')})
        payload=self.bot.api.call_args.kwargs
        self.assertIn('2 / 2',payload['text'])
        self.assertIn('channel10',str(payload['reply_markup']))
        await self.bot.update({'callback_query':self.callback('adm:pick:tg:11:1')})
        self.assertEqual(self.admin.jobs(),[])
        rows=self.bot.api.call_args.kwargs['reply_markup']['inline_keyboard']
        confirm=rows[0][0]['callback_data']
        await self.bot.update({'callback_query':self.callback(confirm)})
        await self.bot.update({'callback_query':self.callback(confirm)})
        self.assertEqual(len(self.admin.jobs()),1)
        self.assertEqual(self.admin.jobs()[0]['name'],'channel10')
        scanner.db.close()

    async def test_regular_user_still_gets_subscription_menu(self):
        await self.bot.update({'message':{'message_id':60,'from':{'id':2},'chat':{'id':2,'type':'private'},'text':'/start'}})
        self.assertIn('Выбери категории',self.bot.api.call_args.kwargs['text'])
        self.assertNotIn('adm:home',str(self.bot.api.call_args.kwargs['reply_markup']))
