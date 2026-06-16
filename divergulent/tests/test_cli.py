import contextlib
import io
import json
from unittest import mock

import testtools

from divergulent import cli
from divergulent import debversion
from divergulent.inventory import InstalledPackage


def _pkg(binary, binary_version, source, source_version, arch):
    return InstalledPackage(
        binary_name=binary,
        binary_version=debversion.parse(binary_version),
        source_name=source,
        source_version=debversion.parse(source_version),
        architecture=arch)


SAMPLE = [
    _pkg('libc6', '2.36-9', 'glibc', '2.36-9', 'amd64'),
    _pkg('bash', '5.2-1', 'bash', '5.2-1', 'amd64'),
]


class CliInventoryTestCase(testtools.TestCase):

    def _run(self, argv):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=list(SAMPLE)):
            with contextlib.redirect_stdout(out):
                rc = cli.main(argv)
        return rc, out.getvalue()

    def test_table_output(self):
        rc, output = self._run(['inventory'])
        self.assertEqual(0, rc)
        self.assertIn('bash', output)
        self.assertIn('glibc', output)
        # Sorted by source name, so the bash row precedes the glibc (libc6) row.
        self.assertLess(output.index('bash'), output.index('glibc'))

    def test_json_output(self):
        rc, output = self._run(['inventory', '--json'])
        self.assertEqual(0, rc)
        data = json.loads(output)
        self.assertEqual(2, len(data))
        self.assertEqual({'libc6', 'bash'}, {d['binary'] for d in data})
        libc6 = next(d for d in data if d['binary'] == 'libc6')
        self.assertEqual('glibc', libc6['source'])

    def test_no_command_shows_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main([])
        self.assertEqual(1, rc)
        self.assertIn('usage', out.getvalue().lower())


class ConcurrentMapTestCase(testtools.TestCase):

    def _items(self, n):
        return [('pkg-%02d' % i, debversion.parse('1.%d-1' % i)) for i in range(n)]

    def test_preserves_input_order_concurrently(self):
        items = self._items(20)
        progress = cli.Progress(len(items), enabled=False)
        # Return the index so we can assert order is preserved despite out-of-order
        # completion under several workers.
        results = cli._concurrent_map(
            items, lambda name, version: name, workers=8, progress=progress)
        self.assertEqual([name for name, _ in items], results)

    def test_workers_one_is_serial(self):
        items = self._items(5)
        progress = cli.Progress(len(items), enabled=False)
        seen = []
        results = cli._concurrent_map(
            items, lambda name, version: seen.append(name) or name, workers=1, progress=progress)
        self.assertEqual([name for name, _ in items], results)
        # Serial mode visits items strictly in input order.
        self.assertEqual([name for name, _ in items], seen)

    def test_progress_counts_every_item(self):
        items = self._items(7)
        steps = []

        class RecordingProgress:
            def step(self, label):
                steps.append(label)

            def finish(self):
                pass

        cli._concurrent_map(items, lambda name, version: name, workers=4, progress=RecordingProgress())
        self.assertEqual(sorted(name for name, _ in items), sorted(steps))
