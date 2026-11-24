import unittest
from twitch_reader import choose_channels,parse_line
class ReaderTests(unittest.TestCase):
 def test_limit_live_first(self):
  names=['ch'+str(i) for i in range(273)];live=set(names[150:180]);chosen=choose_channels(names,live,0)
  self.assertEqual(len(chosen),100);self.assertTrue(live.issubset(chosen))
 def test_rotation(self):
  names=['ch'+str(i) for i in range(273)];self.assertNotEqual(choose_channels(names,[],0),choose_channels(names,[],1))
 def test_live_overflow(self):
  names=['ch'+str(i) for i in range(273)];self.assertEqual(len(choose_channels(names,names,1)),100)
 def test_join_ack(self):
  self.assertEqual(parse_line(':me!me@me.tmi.twitch.tv JOIN #offline'),('me!me@me.tmi.twitch.tv','JOIN',['#offline'],''))
 def test_message(self):
  self.assertEqual(parse_line('@id=1 :u!u@u PRIVMSG #abc :hello :world')[1:],('PRIVMSG',['#abc'],'hello :world'))
if __name__=='__main__':unittest.main()
