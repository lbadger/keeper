"""Watch-style terminal refresh and per-run diagnostics, without tape hardware."""
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_cli_status import TerminalOutput


class LiveDisplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'backup.log'

    def test_prompt_suspends_refresh_and_exceptions_restore_the_terminal(self):
        output = TerminalOutput()
        with redirect_stderr(output), patch.dict(os.environ, TERM='xterm', NO_COLOR='1'), \
                tb.session_log('backup', self.path):
            with self.assertRaisesRegex(tb.BackupError, 'failed'):
                with tb.Progress('Backup', archives=1) as progress:
                    progress.track_archive(100)
                    progress.advance(20)
                    progress.report()
                    tb.log('Routine diagnostic stays in the file')
                    with tb.terminal_prompt():
                        self.assertFalse(tb.ACTIVE_SCREEN.active)
                        output.write('Load cartridge and press Enter\n')
                    self.assertTrue(tb.ACTIVE_SCREEN.active)
                    raise tb.BackupError('failed')
        text = output.getvalue()
        self.assertEqual(text.count('\x1b[?1049h'), 2)
        self.assertEqual(text.count('\x1b[?1049l'), 2)
        self.assertEqual(text.count('\x1b[?25l'), text.count('\x1b[?25h'))
        self.assertLess(text.index('\x1b[?1049l'), text.index('Load cartridge'))
        self.assertNotIn('Routine diagnostic', text)
        self.assertIn('Routine diagnostic', self.path.read_text())
        self.assertNotIn('\x1b', self.path.read_text())
        self.assertNotIn('100.0%', text)
        self.assertIsNone(tb.ACTIVE_SCREEN)
        self.assertIsNone(tb.LOG_FILE)

    def test_resized_screen_clears_stale_rows_without_scrolling(self):
        output = TerminalOutput()
        screen = tb.LiveScreen(output)
        screen.enter()
        with patch.object(tb.shutil, 'get_terminal_size', return_value=os.terminal_size((18, 5))):
            screen.draw('\n'.join(['\x1b[36m' + 'x' * 100 + '\x1b[0m'] * 20))
        first = output.getvalue().split('\x1b[H')[-1]
        self.assertEqual(first.count('\n'), 3)
        plain = re.sub(r'\x1b\[[0-9;]*[mKJ]', '', first)
        self.assertTrue(all(len(line) <= 17 for line in plain.splitlines()))
        screen.draw('short')
        self.assertTrue(output.getvalue().endswith('\x1b[Hshort\x1b[K\x1b[J'))
        screen.leave()

    def test_redirected_diagnostics_and_child_stderr_go_to_file_but_results_stay_stdout(self):
        errors, result = io.StringIO(), io.StringIO()
        with redirect_stderr(errors), redirect_stdout(result), tb.session_log('list', self.path):
            process = tb.logged_process([sys.executable, '-c',
                "import sys; print('file-name'); print('child diagnostic', file=sys.stderr)"],
                result_stdout=True)
            process.wait(timeout=3)
            tb.stop_process(process)
            with tb.Progress('Listing files') as progress:
                progress.phase = 'complete'
        self.assertEqual(result.getvalue(), 'file-name\n')
        self.assertEqual(errors.getvalue(), f'Log file: {self.path}\n')
        self.assertIn('child diagnostic', self.path.read_text())
        self.assertIn('complete', self.path.read_text())
        self.assertNotIn('file-name', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_log_location_override_append_and_fatal_errors(self):
        for args in (['--log-file', str(self.path), 'wipe', '--device', '/missing-tape', '--yes'],
                     ['wipe', '--log-file', str(self.path), '--device', '/missing-tape', '--yes']):
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(tb.main(args), 1)
            self.assertIn('Error: Cannot access tape device', errors.getvalue())
        self.assertEqual(self.path.read_text().count('Error: Cannot access tape device'), 2)
        with redirect_stderr(io.StringIO()), patch.dict(os.environ, XDG_STATE_HOME=str(self.root)):
            tb.main(['status', '--device', '/missing-tape'])
        self.assertEqual(len(list((self.root / 'keeper/logs').glob('*-status-*.log'))), 1)
        self.assertIsNone(tb.LOG_FILE)

    def test_log_open_failure_stops_before_accessing_drive(self):
        with redirect_stderr(io.StringIO()), patch.object(tb, 'TapeMedia') as drive:
            self.assertEqual(tb.main(['wipe', '--log-file', str(self.root), '--yes']), 1)
        drive.assert_not_called()

    def test_real_cli_refreshes_repeatedly_and_preserves_backup_and_restore_bytes(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 90, 0, 0))
        self.addCleanup(os.close, master)
        script = str(Path(tb.__file__).resolve().with_name('keeper.py'))
        media = str(self.root / 'media')
        producer = ("import sys,time; print('producer diagnostic', file=sys.stderr); "
                    "sys.stdout.buffer.write(b'a'*100000); sys.stdout.flush(); "
                    "time.sleep(2.2); sys.stdout.buffer.write(b'b'*100000)")
        process = subprocess.Popen([sys.executable, script, 'stream-backup', '--name', 'refresh',
            '--media-dir', media, '--buffer-size', '64KiB', '--log-file', str(self.path),
            '--command', sys.executable, '-c', producer], stdout=subprocess.PIPE, stderr=slave,
            env={**os.environ, 'TERM': 'xterm', 'NO_COLOR': '1'})
        os.close(slave)
        chunks, deadline = [], time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)
            identifier, _ = process.communicate(timeout=3)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        text = b''.join(chunks).decode()
        self.assertEqual(process.returncode, 0, text)
        self.assertGreaterEqual(text.count('\x1b[H'), 4)
        self.assertEqual(text.count('\x1b[?1049h'), 1)
        self.assertEqual(text.count('\x1b[?1049l'), 1)
        self.assertIn('100.0% (complete)', text)
        self.assertNotIn('producer diagnostic', text)
        self.assertIn('producer diagnostic', self.path.read_text())
        self.assertNotIn('\x1b', self.path.read_text())
        restored = subprocess.run([sys.executable, script, 'stream-restore', '--backup',
            identifier.decode().strip(), '--media-dir', media, '--log-file', str(self.path)],
            capture_output=True, timeout=10)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(restored.stdout, b'a' * 100000 + b'b' * 100000)
        self.assertNotIn(b'ETA', restored.stderr)


if __name__ == '__main__':
    unittest.main()
