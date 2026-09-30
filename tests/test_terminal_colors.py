"""Readable terminal progress without coloring redirected data or trusting controls."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_cli_status import TerminalOutput, without_color


class TerminalColorTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, TERM='xterm-256color', NO_COLOR='')
        environment.start()
        self.addCleanup(environment.stop)
        self.progress = tb.Progress('a' * 32, buffer_size=1024**3, archives=1)
        self.progress.track_archive(1024**3)
        self.progress.advance(256 * 1024**2)
        self.progress.phase = 'writing volume 1, chunk 64'
        self.progress.read_bytes = 512 * 1024**2
        self.progress.written_bytes = self.progress.transferred = 256 * 1024**2
        self.progress.durable_bytes = 128 * 1024**2
        self.progress.reader_state = 'reading'
        volume = SimpleNamespace(record_bytes=300 * 1024**2)
        self.progress.start_tape(volume, 1, writing=True, capacity=2 * 1024**3)
        volume.record_bytes += 64 * 1024**2

    def render(self, stream=None, width=88):
        stream = TerminalOutput() if stream is None else stream
        with redirect_stderr(stream), patch.object(tb, 'terminal_width', return_value=width):
            self.progress.report()
        return stream.getvalue()

    def test_progress_prioritizes_total_and_etas_and_aligns_transfer_counters(self):
        text = self.render()
        self.assertIn('\x1b[1;36mBackup aaaaaaaaaaaa\x1b[0m', text)
        plain = without_color(text)
        self.assertIn('~25.0%', plain)
        self.assertLess(plain.index('Total ['), plain.index('Total job ETA'))
        self.assertLess(plain.index('Total job ETA'), plain.index('Tape ETA'))
        self.assertLess(plain.index('Tape ETA'), plain.index('Read'))
        for label, value in (('Read', '512.00 MiB'), ('Delivered', '256.00 MiB'),
                             ('I/O', ''), ('Source', ''), ('Buffer', '1.00 GiB each')):
            self.assertIn(f'  {label:<11}{value}', plain)
        self.assertIn('Committed 128.00 MiB', plain)
        self.assertIn('Wait totals:', plain)

    def test_colors_obey_terminal_capability_and_no_color(self):
        for env, stream in (({}, io.StringIO()), ({'NO_COLOR': '1'}, TerminalOutput()),
                            ({'NO_COLOR': '0'}, TerminalOutput()),
                            ({'TERM': 'dumb'}, TerminalOutput())):
            with self.subTest(env=env, tty=stream.isatty()), patch.dict(os.environ, env):
                text = self.render(stream)
            self.assertNotIn('\x1b', text)
            self.assertIn('25.0%', text)
            self.assertIn('ETA', text)
            if not stream.isatty():
                self.assertEqual(len(text.splitlines()), 1)
                self.assertNotIn('Total [', text)
        self.assertIn('\x1b[', self.render())

    def test_wait_and_finalize_are_yellow_and_only_complete_is_green(self):
        for phase in ('waiting for source', 'load volume 2',
                      'flushing volume 1 (backup completion)', 'finalizing archive extraction',
                      'writing metadata file'):
            with self.subTest(phase=phase):
                self.progress.phase = phase
                text = self.render()
                self.assertIn(f'\x1b[33m  Status     {phase}\x1b[0m', text)
                self.assertNotIn('\x1b[1;32m', text)
                self.assertNotIn('100.0%', text)
        self.progress.phase = 'complete'
        text = self.render()
        self.assertIn('\x1b[1;32m', text)
        self.assertIn('100.0% (complete)', text)

    def test_colored_lines_fit_and_reset_at_each_line_on_narrow_terminals(self):
        for width in (40, 60, 88, 120):
            with self.subTest(width=width):
                text = self.render(width=width)
                self.assertTrue(all(len(line) <= width for line in without_color(text).splitlines()))
                for line in text.splitlines():
                    if '\x1b[' in line:
                        self.assertTrue(line.endswith('\x1b[0m'))
                self.assertNotIn('\x1b', without_color(text))

    def test_control_characters_cannot_inject_colors_or_erase_the_terminal(self):
        self.progress.label = 'Restore \x1b[2J\nname'
        self.progress.phase = 'extracting \x1b[31mpath'
        text = self.render()
        plain = without_color(text)
        self.assertIn(r'Restore \x1b[2J\x0aname', plain)
        self.assertIn(r'extracting \x1b[31mpath', plain)
        self.assertNotIn('\x1b[2J', text)
        self.assertNotIn('\x1b[31m', text)

    def test_inventory_and_other_nontransfer_progress_respect_no_color(self):
        self.progress = tb.Progress('Source inventory', transfer=False)
        self.progress.phase = 'waiting for metadata'
        self.assertIn('\x1b[33m', self.render())
        with patch.dict(os.environ, NO_COLOR='1'):
            self.assertNotIn('\x1b', self.render())

    def test_help_colors_are_consistent_across_python_versions_and_subcommands(self):
        for args in ([], ['--help'], ['backup', '--help'], ['restore', '--help']):
            with self.subTest(args=args):
                terminal = TerminalOutput()
                with redirect_stdout(terminal):
                    if args:
                        with self.assertRaises(SystemExit) as stopped:
                            tb.main(args)
                        self.assertEqual(stopped.exception.code, 0)
                    else:
                        self.assertEqual(tb.main(args), 0)
                self.assertIn('\x1b[1;36musage:', terminal.getvalue())
                self.assertIn('\x1b[36m--help\x1b[0m', terminal.getvalue())
                if args in ([], ['--help']):
                    for command in ('backup', 'zfs-backup', 'inspect (info)', 'rewind', 'verify'):
                        self.assertIn(f'\x1b[1;36m{command}\x1b[0m', terminal.getvalue())

    def test_help_checks_its_own_stream_and_keeps_redirected_output_plain(self):
        parser = tb.make_parser()
        for env, output in (({}, io.StringIO()), ({'NO_COLOR': '1'}, TerminalOutput()),
                            ({'TERM': 'dumb'}, TerminalOutput())):
            with self.subTest(env=env, tty=output.isatty()), patch.dict(os.environ, env), \
                    redirect_stderr(TerminalOutput()):
                parser.print_help(file=output)
                self.assertNotIn('\x1b', output.getvalue())
                self.assertIn('--help', output.getvalue())
        with patch.dict(os.environ, FORCE_COLOR='1', PYTHON_COLORS='1'):
            output = io.StringIO()
            parser.print_help(file=output)
            self.assertNotIn('\x1b', output.getvalue())


if __name__ == '__main__':
    unittest.main()
