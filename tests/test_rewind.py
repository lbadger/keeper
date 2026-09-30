"""Explicit rewind preserves recorded data and coordinates with active jobs."""
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_append import Cartridge, TapeVolume


class RewindCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = object.__new__(tb.TapeMedia)
        self.media.device = '/dev/nst1'
        self.media.device_number = os.makedev(9, 129)
        self.tape = Cartridge()
        self.tape.records = [b'a' * tb.BLOCK_SIZE, None, b'b' * tb.BLOCK_SIZE, None]
        self.tape.cursor = len(self.tape.records)
        self.volume = TapeVolume(self.tape)
        self.addCleanup(self.volume.close)
        self.commands = []
        for context in (patch.object(tb, 'DRIVE_LOCK_DIRECTORY', self.root),
                        patch.object(Path, 'read_text', return_value='0xa002'),
                        patch.object(tb, 'run_command', side_effect=self.command)):
            context.start()
            self.addCleanup(context.stop)

    def command(self, args):
        with self.assertRaises(tb.DriveBusy), self.media.lock():
            pass
        self.assertEqual(args[:3], ['mt', '-f', self.media.device])
        self.commands.append(args[3:])
        if args[3:] == ['rewind']:
            # Immediate rewind must be disabled before reporting completion.
            self.assertIn(['stclearoptions', '0xa002'], self.commands[:-1])
            self.volume.control(tb.MTREW)
        elif args[3:] not in (['stclearoptions', '0xa002'], ['stsetoptions', 'scsi2logical']):
            self.fail(f'Unexpected tape operation: {args}')
        return ''

    def rewind(self, *, default=False):
        output, errors = io.StringIO(), io.StringIO()
        device_args = [] if default else ['--device', self.media.device]
        with patch.object(tb, 'TapeMedia', return_value=self.media) as factory, \
                redirect_stdout(output), redirect_stderr(errors):
            code = tb.main(['rewind', *device_args, '--log-file', str(self.root / 'rewind.log')])
        factory.assert_called_once_with('/dev/nst0' if default else self.media.device)
        self.output, self.errors = output.getvalue(), errors.getvalue()
        return code

    def test_rewind_returns_to_beginning_with_records_and_cartridge_retained(self):
        before = list(self.tape.records)
        self.assertEqual(self.rewind(), 0, self.errors)
        self.assertEqual(self.tape.cursor, 0)
        self.assertEqual(self.tape.records, before)
        self.assertIn('Rewound tape in /dev/nst1; left loaded', self.output)
        with self.media.lock():
            pass

    def test_default_device(self):
        self.media.device = '/dev/nst0'
        self.media.device_number = os.makedev(9, 128)
        self.assertEqual(self.rewind(default=True), 0, self.errors)
        self.assertEqual(self.tape.cursor, 0)

    def test_active_job_blocks_rewind_before_any_drive_command(self):
        with self.media.lock():
            self.assertEqual(self.rewind(), 1)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.tape.cursor, len(self.tape.records))
        self.assertEqual(self.output, '')
        self.assertIn('Another operation', self.errors)

    def test_failed_or_interrupted_rewind_reports_no_success_and_releases_lock(self):
        for failure, code, message in ((OSError(errno.EIO, 'Rewind failed'), 1, 'Rewind failed'),
                                       (KeyboardInterrupt(), 130, 'Rewind interrupted')):
            with self.subTest(code=code), patch.object(self.media, 'mt', side_effect=failure):
                self.assertEqual(self.rewind(), code)
            self.assertEqual(self.output, '')
            self.assertIn(message, self.errors)
            self.assertNotIn('incomplete tail', self.errors)
            with self.media.lock():
                pass


if __name__ == '__main__':
    unittest.main()
