import unittest,tempfile
from pathlib import Path
from bot import Store,classify,is_scanner_post
class BotTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'test.db')
 def tearDown(self): self.store.db.close();self.temp.cleanup()
 def test_wheels_ignore_original_cross_ad(self):
  self.assertEqual(classify('🎁 РОЗЫГРЫШ | TELEGRAM\n🎯 BetBoom\n🔗 https://betboom.ru/freestream/x\n📝 Оригинал сообщения:\ncaser magic-drop.win'),{'betboom'})
 def test_md(self):self.assertEqual(classify('✈️ TELEGRAM | канал\n🔑 MD-AAAA-BBBB-CCCC'),{'magicdrop'})
 def test_jabka(self):self.assertEqual(classify('✈️ TELEGRAM | Жабка\n🔑 ABCD123456\nhttps://jabka.to/case/x'),{'jabka'})
 def test_chat_not_forwarded(self):self.assertFalse(is_scanner_post('Мне нравится BetBoom'))
 def test_default_opt_out(self):
  self.store.ensure_user(1);self.assertEqual(self.store.preferences(1),set())
 def test_filter_and_dedup(self):
  self.store.toggle(1,'betboom');self.store.toggle(2,'caser');self.store.enqueue(-1,7,{'betboom'});self.store.enqueue(-1,7,{'betboom'})
  rows=self.store.pending();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['user_id'],1)
 def test_stop_cancels_queue(self):
  self.store.toggle(1,'betboom');self.store.enqueue(-1,7,{'betboom'});self.store.stop(1);self.assertEqual(self.store.pending(),[])
 def test_unsubscribe_cancels_category(self):
  self.store.toggle(1,'betboom');self.store.enqueue(-1,7,{'betboom'});self.store.toggle(1,'betboom');self.assertFalse(self.store.still_wanted(self.store.pending()[0]))
 def test_restart_persistence(self):
  self.store.toggle(1,'caser');other=Store(self.store.path);self.assertEqual(other.preferences(1),{'caser'});other.db.close()
 def test_reenable_only_new_messages(self):
  self.store.toggle(1,'betboom');self.store.enqueue(-1,7,{'betboom'});self.store.stop(1);self.store.toggle(1,'betboom');self.assertEqual(self.store.pending(),[])
if __name__=='__main__':unittest.main()
