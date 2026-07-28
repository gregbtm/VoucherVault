from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from myapp.models import ScanFieldCorrection
from myapp.scan_learning import MAX_PROMPT_HINTS, build_ocr_correction_hints
from myapp.test_utils import set_site_config
from ocr.backends.claude_backend import ClaudeOCRBackend


class BuildOcrCorrectionHintsTestCase(TestCase):
    """
    build_ocr_correction_hints is the proactive counterpart to
    apply_learned_corrections: instead of only fixing a known-bad value
    after OCR returns it, it tells the vision model about this user's
    recurring correction patterns up front, in the prompt itself.
    """

    def setUp(self):
        self.user = User.objects.create_user('hintuser', 'h@example.com', 'password123')

    def test_empty_for_anonymous_user(self):
        self.assertEqual(build_ocr_correction_hints(None), '')

    def test_empty_when_user_has_no_corrections(self):
        self.assertEqual(build_ocr_correction_hints(self.user), '')

    def test_mentions_a_recorded_correction(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer',
            ai_value='Amzn', corrected_value='Amazon',
        )
        hint = build_ocr_correction_hints(self.user)
        self.assertIn('issuer', hint)
        self.assertIn('Amzn', hint)
        self.assertIn('Amazon', hint)

    def test_blank_ai_value_corrections_are_excluded(self):
        """Blank-fill corrections ('the scan left it empty, user filled
        it in') have no source misreading to warn the model about."""
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer',
            ai_value='', corrected_value='Amazon', times_seen=5,
        )
        self.assertEqual(build_ocr_correction_hints(self.user), '')

    def test_only_learnable_fields_considered(self):
        """Direct ORM write bypassing the field validation callers use -
        confirms the query itself filters, not just upstream callers."""
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='name',
            ai_value='Wrong Name', corrected_value='Right Name',
        )
        self.assertEqual(build_ocr_correction_hints(self.user), '')

    def test_capped_at_max_prompt_hints_ordered_by_times_seen(self):
        for i in range(MAX_PROMPT_HINTS + 3):
            ScanFieldCorrection.objects.create(
                user=self.user, item_type='giftcard', field='issuer',
                ai_value=f'Wrong{i}', corrected_value='Right',
                times_seen=i,  # higher i = more times seen = should rank first
            )
        hint = build_ocr_correction_hints(self.user)
        self.assertEqual(hint.count(' is usually actually '), MAX_PROMPT_HINTS)
        # The most-seen ones (highest i) must be the ones included.
        self.assertIn(f'Wrong{MAX_PROMPT_HINTS + 2}', hint)
        self.assertNotIn('Wrong0"', hint)

    def test_never_raises_on_lookup_failure(self):
        with patch('myapp.scan_learning.ScanFieldCorrection.objects.filter', side_effect=Exception('boom')):
            self.assertEqual(build_ocr_correction_hints(self.user), '')


class ClaudeBackendUsesCorrectionHintsTestCase(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('claudehintuser', 'ch@example.com', 'password123')
        set_site_config(anthropic_api_key='test-key')

    def _mock_client_returning(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_block = MagicMock(type='text', text='{"code": "SAVE20", "confidence": 0.9}')
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client
        return mock_client

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_prompt_includes_hint_when_user_has_corrections(self, mock_anthropic_cls):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer',
            ai_value='Amzn', corrected_value='Amazon',
        )
        mock_client = self._mock_client_returning(mock_anthropic_cls)

        ClaudeOCRBackend().extract(b'fake-bytes', 'image/jpeg', user=self.user)

        sent_text = mock_client.messages.create.call_args.kwargs['messages'][0]['content'][1]['text']
        self.assertIn('Amzn', sent_text)
        self.assertIn('Amazon', sent_text)

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_prompt_unchanged_when_no_user_given(self, mock_anthropic_cls):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer',
            ai_value='Amzn', corrected_value='Amazon',
        )
        mock_client = self._mock_client_returning(mock_anthropic_cls)

        ClaudeOCRBackend().extract(b'fake-bytes', 'image/jpeg')

        sent_text = mock_client.messages.create.call_args.kwargs['messages'][0]['content'][1]['text']
        self.assertNotIn('Amzn', sent_text)

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_prompt_unchanged_when_user_has_no_corrections(self, mock_anthropic_cls):
        mock_client = self._mock_client_returning(mock_anthropic_cls)

        ClaudeOCRBackend().extract(b'fake-bytes', 'image/jpeg', user=self.user)

        sent_text = mock_client.messages.create.call_args.kwargs['messages'][0]['content'][1]['text']
        from ocr.backends.claude_backend import _PROMPT
        self.assertEqual(sent_text, _PROMPT)
