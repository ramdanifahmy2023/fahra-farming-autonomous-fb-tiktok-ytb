import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageFont

from fahra_automation import hook
from fahra_automation.analysis import Analyzer


class HookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / 'hook.png'

    def assert_layout(self, text, width, height):
        layout = hook.create_hook(text, width, height, self.target)
        normalized = ' '.join(unicodedata.normalize('NFC', text).split())
        self.assertEqual(' '.join(layout['lines']), normalized)
        self.assertTrue(1 <= len(layout['lines']) <= 3)
        self.assertGreaterEqual(layout['font_size'], round(width * .061))
        self.assertLessEqual(layout['font_size'], round(width * .081))
        left, top, card_width, card_height = layout['card']
        pad_x, pad_y = layout['padding']
        self.assertAlmostEqual(left + card_width / 2, width * .47, delta=.5)
        self.assertEqual(top, round(height * .18))
        self.assertGreater(left, 0)
        self.assertLess(left + card_width, width)
        self.assertLess(top + card_height, height)
        self.assertLessEqual(card_width, round(width * .76))
        font = ImageFont.truetype(str(hook.FONT), layout['font_size'], layout_engine=ImageFont.Layout.BASIC)
        for line in layout['lines']:
            box = font.getbbox(line)
            self.assertLessEqual(max(box[2] - box[0], font.getlength(line)), card_width - 2 * pad_x)
        with Image.open(self.target) as image:
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.size, (width, height))
            self.assertEqual(image.getchannel('A').getbbox(), (left, top, left + card_width, top + card_height))
            text_pixels = image.getchannel('R').point(lambda value: 255 if value > 180 else 0)
            text_box = text_pixels.getbbox()
            self.assertIsNotNone(text_box)
            self.assertGreaterEqual(text_box[0], left + pad_x - 1)
            self.assertLessEqual(text_box[2], left + card_width - pad_x + 1)
            self.assertGreaterEqual(text_box[1], top + pad_y - 1)
            self.assertLessEqual(text_box[3], top + card_height - pad_y + 1)
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        return layout

    def test_measured_text_and_pixels_stay_inside_card_at_supported_sizes(self):
        longest = 'Kucing penasaran masuk dalam kardus besar milik tetangganya!'
        self.assertEqual(len(longest), 60)
        texts = ('Hai', longest, 'WWW MMM WWW MMM WWW MMM WWW MMM WWW MMM',
                 "Café: 100% seru, 'kok' bisa?", '%{pts}: ini cuma teks', 'Éléphant — déjà vu?')
        for width, height in ((540, 960), (720, 1280), (1080, 1920)):
            for text in texts:
                with self.subTest(size=(width, height), text=text):
                    layout = self.assert_layout(text, width, height)
                    if text == 'Hai':
                        self.assertEqual(len(layout['lines']), 1)
                        self.assertEqual(layout['font_size'], round(width * .081))
                    if text == longest:
                        self.assertGreater(len(layout['lines']), 1)

    def test_balanced_three_lines_handle_wide_words_without_splitting_them(self):
        text = 'WWW MMM WWW MMM WWW MMM WWW MMM WWW MMM'
        layout = self.assert_layout(text, 540, 960)
        self.assertEqual(len(layout['lines']), 3)
        self.assertEqual(' '.join(layout['lines']).split(), text.split())

    def test_accent_normalization_and_whitespace_preserve_text(self):
        self.assert_layout("  Cafe\u0301:\n déjà\tvu, ‘iya’?  ", 720, 1280)

    def test_limits_and_unfittable_token_fail_before_writing(self):
        for text in (None, 1, '', '   ', 'a' * 61, ' '.join(['a'] * 11), 'W' * 60):
            with self.subTest(text=text), self.assertRaises(ValueError):
                hook.create_hook(text, 540, 960, self.target)
            self.assertFalse(self.target.exists())

    def test_unsupported_glyphs_and_format_controls_never_render_as_missing_boxes(self):
        for text in ('Hai 😊', 'Hai 漢', 'Hai\u200d', 'Hai\u202e', 'Hai\x00', 'Hai\uffff'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                hook.create_hook(text, 720, 1280, self.target)
            self.assertFalse(self.target.exists())

    def test_missing_font_does_not_fallback_or_replace_existing_image(self):
        self.target.write_bytes(b'previous verified image')
        with patch.object(hook, 'FONT', self.target.parent / 'missing.ttf'), self.assertRaises(OSError):
            hook.create_hook('Hai', 720, 1280, self.target)
        self.assertEqual(self.target.read_bytes(), b'previous verified image')

    def test_generic_analyzer_validation_preserves_already_rendered_legacy_hook_and_caption(self):
        generated = {'hook': 'Kucing penasaran', 'caption': 'Kardusnya diperiksa dulu 😺',
                     'hashtags': ['#kucing'], 'confidence': .9}
        with patch('fahra_automation.analysis.layout_hook') as layout:
            for text in ('Kucing 😊', 'Kucing 漢', 'W' * 60):
                with self.subTest(text=text):
                    result = Analyzer.validate({**generated, 'hook': text})
                    self.assertEqual(result['hook'], text)
                    self.assertEqual(result['caption'], generated['caption'])
            layout.assert_not_called()
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_invalid_canvas_does_not_write_image(self):
        for width, height in ((539, 960), (540, 959), (960, 540), (1080, 1080)):
            with self.subTest(size=(width, height)), self.assertRaises(ValueError):
                hook.create_hook('Hai', width, height, self.target)
            self.assertFalse(self.target.exists())
