import unittest
from twitch_reader import choose_channels,parse_line
class ReaderTests(unittest.TestCase):
 def test_limit_live_first(self):
  names=['ch'+str(i) for i in range(273)];live=set(names[150:180]);chosen=choose_channels(names,live,0)
  self.assertEqual(len(chosen),90);self.assertTrue(live.issubset(chosen))
 def test_rotation(self):
  names=['ch'+str(i) for i in range(273)];self.assertNotEqual(choose_channels(names,[],0),choose_channels(names,[],1))
 def test_live_overflow(self):
  names=['ch'+str(i) for i in range(273)];self.assertEqual(len(choose_channels(names,names,1)),90)
 def test_join_ack(self):
  self.assertEqual(parse_line(':me!me@me.tmi.twitch.tv JOIN #offline'),('me!me@me.tmi.twitch.tv','JOIN',['#offline'],''))
 def test_message(self):
  self.assertEqual(parse_line('@id=1 :u!u@u PRIVMSG #abc :hello :world')[1:],('PRIVMSG',['#abc'],'hello :world'))
if __name__=='__main__':unittest.main()


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
 def reader(self):
  from twitch_reader import Reader
  from types import SimpleNamespace
  return Reader(SimpleNamespace(all_channels=['a','b']), 'test', {})
 def test_first_limit_retry_is_short_and_status_honest(self):
  from unittest.mock import patch
  r=self.reader()
  with patch('twitch_reader.time.time',return_value=1000):
   r.limit_reached('a')
   self.assertEqual(r.blocked_until,1030)
   self.assertEqual(r.status['twitch'],'waiting_limit')
   self.assertEqual(r.status['twitch_retry_at'],1030)
 def test_repeated_limit_backoff_is_bounded(self):
  from unittest.mock import patch
  r=self.reader()
  with patch('twitch_reader.time.time',return_value=1000):
   for _ in range(8):r.limit_reached('a')
   self.assertEqual(r.blocked_until,1300)
 def test_join_clears_stale_limit_notice(self):
  r=self.reader();r.limit_reached('a');r.join_confirmed('a')
  self.assertEqual(r.status['twitch'],'connected')
  self.assertEqual(r.status['twitch_channels'],1)
  self.assertNotIn('twitch_last_notice',r.status)
  self.assertNotIn('twitch_retry_at',r.status)
