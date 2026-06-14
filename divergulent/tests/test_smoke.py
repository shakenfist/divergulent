import testtools

from divergulent import cli


class SmokeTestCase(testtools.TestCase):
    def test_main_is_callable(self):
        self.assertTrue(callable(cli.main))

    def test_parser_has_inventory_command(self):
        parser = cli._build_parser()
        # Parsing the inventory sub-command should not raise.
        args = parser.parse_args(['inventory'])
        self.assertEqual('inventory', args.command)
