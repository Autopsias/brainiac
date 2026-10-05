"""Blank-image fallback is bounded and never modifies original image data."""
import unittest
from unittest.mock import Mock, patch
from PIL import Image

from brain.ingest.handlers.ocr_retry import read_image


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new('RGB', (400, 200), 'white')

    def test_existing_text_is_unchanged(self):
        engine = Mock()
        engine.image_to_string.return_value = ' Existing text '
        self.assertEqual(read_image(engine, self.image, 'eng', 20), ('Existing text', []))
        self.assertEqual(engine.image_to_string.call_count, 1)

    def test_blank_retry_keeps_original_and_flags_review(self):
        before = self.image.tobytes()
        engine = Mock()
        engine.image_to_string.side_effect = ['', 'The garden contains many flowers and trees beside the gate.']
        text, warnings = read_image(engine, self.image, 'eng', 20)
        self.assertIn('garden', text)
        self.assertIn('verify against original', warnings[0])
        self.assertEqual(self.image.tobytes(), before)
        self.assertEqual(engine.image_to_string.call_args.kwargs['config'], '--psm 11')
        self.assertLessEqual(engine.image_to_string.call_args.kwargs['timeout'], 20)

    def test_tiny_or_large_images_do_not_retry(self):
        for size in ((20, 20), (2000, 2000)):
            engine = Mock()
            engine.image_to_string.return_value = ''
            read_image(engine, Image.new('L', size), 'eng', 20)
            self.assertEqual(engine.image_to_string.call_count, 1)

    def test_exhausted_time_budget_does_not_retry(self):
        engine = Mock()
        engine.image_to_string.return_value = ''
        with patch('brain.ingest.handlers.ocr_retry.time.monotonic', side_effect=[0, 21]):
            self.assertIn('exhausted', read_image(engine, self.image, 'eng', 20)[1][0])
        self.assertEqual(engine.image_to_string.call_count, 1)

    def test_sparse_recognition_noise_is_not_a_success(self):
        engine = Mock()
        engine.image_to_string.side_effect = ['', 'abc xyz']
        self.assertEqual(read_image(engine, self.image, 'eng', 20)[0], '')


if __name__ == '__main__':
    unittest.main()
