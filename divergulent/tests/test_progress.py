import io

import testtools

from divergulent.progress import Progress


class ProgressTestCase(testtools.TestCase):

    def test_tty_animates_in_place(self):
        out = io.StringIO()
        progress = Progress(3, stream=out, tty=True)
        progress.step('a')
        progress.step('b')
        progress.finish()
        rendered = out.getvalue()
        self.assertIn('\r', rendered)
        self.assertIn('[2/3] b', rendered)
        self.assertTrue(rendered.endswith('\n'))

    def test_non_tty_prints_periodic_and_final_lines(self):
        out = io.StringIO()
        progress = Progress(3, stream=out, tty=False, every=2)
        progress.step('a')   # n=1: not a multiple of 2, not the last -> nothing
        progress.step('b')   # n=2: multiple of 2 -> a line
        progress.step('c')   # n=3: the last -> a line
        progress.finish()    # off-TTY: no trailing newline
        lines = [line for line in out.getvalue().splitlines() if line]
        self.assertEqual(['[2/3] b', '[3/3] c'], lines)

    def test_disabled_is_silent(self):
        out = io.StringIO()
        progress = Progress(3, stream=out, tty=True, enabled=False)
        progress.step('a')
        progress.finish()
        self.assertEqual('', out.getvalue())
