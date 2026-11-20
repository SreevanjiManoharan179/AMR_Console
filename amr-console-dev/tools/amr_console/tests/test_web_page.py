"""The console page: parses, stays offline, never stores the token, uses only real actions."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registry  # noqa: E402

PAGE = Path(__file__).resolve().parents[1] / 'web/index.html'
ROOT = Path(__file__).resolve().parents[3]


def scripts():
    return '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', PAGE.read_text(), re.S))


class WebPageTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_page_script_parses(self):
        with tempfile.NamedTemporaryFile('w', suffix='.js') as handle:
            handle.write(scripts())
            handle.flush()
            result = subprocess.run(['node', '--check', handle.name], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_no_external_resources(self):
        text = PAGE.read_text()
        urls = set(re.findall(r'https?://[^\s"\'<>)]+', text)) - {'http://www.w3.org/2000/svg'}
        self.assertEqual(set(), urls)
        self.assertNotRegex(text, r'<link[^>]+href=|<script[^>]+src=')

    def test_token_is_never_stored(self):
        self.assertNotRegex(scripts(), r'localStorage|sessionStorage|document\.cookie|indexedDB')

    def test_preview_actions_exist_in_the_backend(self):
        backend = {action['id'] for action in registry.actions(ROOT, {'ros_ready': True})}
        preview = set(re.findall(r"\{id:'([a-z]+\.[a-z]+)'", PAGE.read_text()))
        self.assertTrue(preview, 'no preview actions found')
        self.assertEqual(set(), preview - backend)

    def test_no_project_or_lab_wording(self):
        # speechSynthesis is the browser's read-aloud interface, not wording: leave it out first.
        text = PAGE.read_text().lower().replace('speechsynthesis', '')
        for word in ('thesis', 'universit', 'case study', 'objective 1', 'office_v1', 'objective1',
                     'trials.', 'review2', 'paired study'):
            self.assertNotIn(word, text)


if __name__ == '__main__':
    unittest.main()


class PageBehaviourTests(unittest.TestCase):
    """Fixes from the 27 Oct live run and the phone check (behaviour is also checked in a browser)."""

    def test_an_old_backend_check_is_dropped_when_no_fresh_one_can_be_made(self):
        # After arrival the request field is empty: the "still running" answer from mid-drive must go.
        body = scripts().split('function scheduleCardPreview', 1)[1].split('\nfunction ', 1)[0]
        self.assertIn('S.cardPreviews[action.id]=null', body)

    def test_the_page_follows_the_backend_operation_flag(self):
        self.assertIn('operation_running', scripts())

    def test_typed_requests_close_the_review_while_the_robot_drives(self):
        body = scripts().split('async function runReviewed', 1)[1].split('\nfunction ', 1)[0]
        self.assertLess(body.index('closeReview()'), body.index('await sent'))

    def test_phone_layout_never_widens_the_page(self):
        text = PAGE.read_text()
        block = text[text.rindex('@media(max-width:850px)'):text.index('</style>')]
        self.assertIn('minmax(0,1fr)', block)
        self.assertIn('flex-wrap:wrap', block)

    def test_speech_never_changes_the_chosen_language(self):
        # Found on 6 Nov: after one Tamil recording the box stayed on Tamil for typed English requests.
        body = scripts().split('async function officeMicStop', 1)[1].split('\nasync function ', 1)[0]
        self.assertNotIn('S.officeForm.language=', body)

