import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

# Execute only pure parsing helpers; importing the scanner starts configuration.
def helpers():
    source = ast.parse(Path(__file__).with_name('scanner_cloud.py').read_text(encoding='utf-8'))
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in ('rich_message_content', 'telegram_message_text', 'inline_entity_links')]
    scope = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<parsers>', 'exec'), scope)
    return scope

class RichMessageTests(unittest.TestCase):
    def test_rich_post_code_and_hidden_link(self):
        rich = SimpleNamespace(to_dict=lambda: {'_': 'RichMessage', 'blocks': [
            {'_': 'PageBlockParagraph', 'text': {'_': 'TextConcat', 'texts': [
                {'_': 'TextUrl', 'url': 'https://magic-drop.win/utm/TUTOR', 'text': {'_': 'TextPlain', 'text': 'MAGICDROP'}},
                {'_': 'TextBold', 'text': {'_': 'TextPlain', 'text': ' ПРОМОКОД: MD-V7V4-DJVJ-4H7U'}}]}},
            {'_': 'PageBlockPhoto', 'photo_id': 123, 'caption': {'_': 'PageCaption', 'text': {'_': 'TextPlain', 'text': 'Подпись'}, 'credit': {'_': 'TextEmpty'}}},
            {'_': 'PageBlockButtonRow', 'buttons': [{'_': 'PageButton', 'text': {'_': 'TextPlain', 'text': 'Колесо'}, 'type': {'_': 'InlineButtonTypeUrl', 'url': 'https://betboom.ru/freestream/tutor'}}]}
        ], 'photos': [{'secret_metadata': 'DO_NOT_SCAN'}]})
        message = SimpleNamespace(message='', entities=[], rich_message=rich)
        h = helpers()
        text, labels = h['inline_entity_links'](message)
        self.assertIn('MD-V7V4-DJVJ-4H7U', text)
        self.assertIn('https://betboom.ru/freestream/tutor', text)
        self.assertEqual(labels['https://magic-drop.win/utm/TUTOR'], 'MAGICDROP')
        self.assertIn('Подпись', text)
        self.assertNotIn('DO_NOT_SCAN', text)
        self.assertIn('MD-V7V4-DJVJ-4H7U', h['telegram_message_text'](message))

    def test_plain_post_unchanged(self):
        message = SimpleNamespace(message='MD-ABCD-EFGH-IJKL', entities=[])
        self.assertEqual(helpers()['inline_entity_links'](message), (message.message, {}))

    def test_upgrader_handler_has_no_undefined_pattern(self):
        tree = ast.parse(Path(__file__).with_name('scanner_cloud.py').read_text(encoding='utf-8'))
        defined = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id.startswith('ANY_UPGRADER_')}
        self.assertFalse(used - defined, used - defined)
