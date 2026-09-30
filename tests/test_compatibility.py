"""The current reader must recover tapes written by the original tape-backup application."""
from contextlib import redirect_stderr
import gzip
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb


class CompatibilityTests(unittest.TestCase):
    def test_keeper_and_legacy_source_entry_points_report_the_same_application(self):
        root = Path(tb.__file__).resolve().parent
        for filename in ('keeper.py', 'tape_backup.py'):
            with self.subTest(filename=filename):
                result = subprocess.run([sys.executable, str(root / filename), '--version'],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f'keeper {tb.PROGRAM_VERSION}')

    def test_keeper_respects_locks_held_under_the_previous_application_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'restored'
            lock_id = hashlib.sha256(os.fsencode(destination)).hexdigest()
            for filename, lock in (
                    ('tape-backup-drive-9-0.lock', lambda: tb.drive_lock(os.makedev(9, 128))),
                    (f'tape-backup-restore-{lock_id}.lock', lambda: tb.restore_lock(destination))):
                with self.subTest(filename=filename), \
                        patch.object(tb, 'DRIVE_LOCK_DIRECTORY', root), \
                        patch.object(tb, 'RESTORE_LOCK_DIRECTORY', root), \
                        tb.shared_lock(root / filename, 'legacy lock', 'busy'):
                    with self.assertRaisesRegex(tb.BackupError, 'Another operation'), lock():
                        pass

    def test_released_format3_fixture_verifies_and_restores(self):
        fixture = Path(__file__).parent / 'fixtures/legacy-format3-full.tape.gz'
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()), patch.object(tb.os, 'sync'):
            root = Path(temporary)
            media = tb.FileMedia(root / 'media')
            media.directory.mkdir()
            with gzip.open(fixture, 'rb') as stream:
                data = stream.read()
            backup_id = tb.decoded_header(data[:tb.BLOCK_SIZE])['backup']['id']
            (media.directory / f'{backup_id}.0001.tape').write_bytes(data)
            self.assertTrue(tb.scan(media, backup_id)['data_verified'])
            destination = root / 'restored'
            tb.restore([backup_id], destination, media, quiet=True)
            self.assertEqual((destination / 'book.txt').read_text(), 'format-3 compatibility fixture\n')
            self.assertEqual(list((destination / 'empty').iterdir()), [])
            self.assertEqual((destination / 'link').readlink(), Path('book.txt'))


if __name__ == '__main__':
    unittest.main()
