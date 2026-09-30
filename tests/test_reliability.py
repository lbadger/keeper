"""Read retries, efficient inspection, and device-wide exclusion of competing jobs."""
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_append import TapeMedia, TapeVolume
from test_tape_backup import tree_contents


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book').write_bytes(os.urandom(700_000))
        context = redirect_stderr(io.StringIO())
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        for context in (patch.object(tb.os, 'sync'), patch.object(tb, 'DRIVE_LOCK_DIRECTORY', self.root)):
            context.start()
            self.addCleanup(context.stop)

    def create(self, media, base=None, cap=None):
        return tb.backup(self.source, media, level='incremental' if base else 'full',
                         base=base, volume_size=cap, buffer_size=tb.BLOCK_SIZE, quiet=True)

    def test_backup_readback_rewinds_after_eio_at_recorded_end(self):
        for failure in ('open', 'position', 'read'):
            with self.subTest(failure=failure):
                media = TapeMedia()
                original_open = media.raw_open
                failures, recorded = [], []

                def raw_open(writing):
                    tape = media.active
                    if not writing and tape.records and tape.cursor == len(tape.records):
                        failures.append(failure)
                        recorded[:] = tape.records
                        error = OSError(errno.EIO, 'Injected error at recorded end')
                        if failure == 'open':
                            raise error
                        volume = original_open(writing)
                        method = 'position' if failure == 'position' else 'next_record'
                        def fail():
                            raise error
                        setattr(volume, method, fail)
                        return volume
                    return original_open(writing)

                with patch.object(media, 'raw_open', raw_open):
                    full = tb.backup(self.source, media, quiet=True,
                                     buffer_size=tb.BLOCK_SIZE, verify=True)
                self.assertEqual(failures, [failure])
                self.assertEqual(media.requests, [(full, 1, 'blank')])
                self.assertTrue(media.last_result['archive_complete'])
                self.assertTrue(media.last_result['data_verified'])
                self.assertEqual(media.active.records, recorded)
                destination = self.root / f'restored-{failure}'
                tb.restore([full], destination, media, quiet=True)
                self.assertEqual(tree_contents(destination), tree_contents(self.source))

    def test_readback_failure_after_rewind_is_fatal_and_identifies_volume(self):
        media = TapeMedia()
        original_open = media.raw_open
        opened, cursors = [], []

        def raw_open(writing):
            volume = original_open(writing)
            if not writing:
                opened.append(volume)
                cursors.append(media.active.cursor)
                def fail():
                    raise OSError(errno.EIO, 'Persistent read failure')
                volume.next_record = fail
            return volume

        with patch.object(media, 'raw_open', raw_open), self.assertRaisesRegex(
                tb.BackupError, 'was committed.*volume 1.*after rewind.*Persistent read failure'):
            tb.backup(self.source, media, quiet=True, buffer_size=tb.BLOCK_SIZE, verify=True)
        self.assertEqual(len(cursors), 2)
        self.assertGreater(cursors[0], 0)
        self.assertEqual(cursors[1], 0)
        self.assertTrue(all(volume.stream.closed for volume in opened))
        self.assertEqual(len(media.requests), 1)
        self.assertTrue(media.last_result['archive_complete'])
        self.assertFalse(media.last_result['data_verified'])

    def test_loaded_lookup_does_not_retry_non_eio_errors(self):
        media = TapeMedia()
        full = self.create(media)
        with patch.object(media, 'raw_open', side_effect=OSError(errno.EACCES, 'Permission denied')), \
                patch.object(media, 'mt') as mt, self.assertRaises(OSError) as error:
            tb.scan(media, full)
        self.assertEqual(error.exception.errno, errno.EACCES)
        mt.assert_not_called()

    def test_readback_payload_eio_is_not_retried_or_marked_verified(self):
        media = TapeMedia()
        original_read = TapeVolume.read
        failed = []

        def read(volume, **kwargs):
            if not kwargs.get('boundary'):
                failed.append(volume.stream.tape.cursor)
                raise OSError(errno.EIO, 'Archive payload read failure')
            return original_read(volume, **kwargs)

        with patch.object(TapeVolume, 'read', read), self.assertRaisesRegex(
                tb.BackupError, 'was committed.*Archive payload read failure'):
            tb.backup(self.source, media, quiet=True, buffer_size=tb.BLOCK_SIZE, verify=True)
        self.assertEqual(len(failed), 1)
        self.assertTrue(media.last_result['archive_complete'])
        self.assertFalse(media.last_result['data_verified'])

    def test_wrong_read_cartridge_twice_then_correct_continues_same_restore(self):
        class RetryMedia(TapeMedia):
            attempts = 0
            def request(self, backup_id, number, action):
                super().request(backup_id, number, action)
                if action == 'read' and number == 2:
                    self.attempts += 1
                    if self.attempts < 3:
                        self.active = self.tapes[0]
        media = RetryMedia()
        full = self.create(media, cap=8 * tb.BLOCK_SIZE)
        before = [list(tape.records) for tape in media.tapes]
        media.loaded = False
        destination = self.root / 'restored'
        tb.restore([full], destination, media, quiet=True)
        self.assertEqual(media.attempts, 3)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))
        self.assertEqual(before, [tape.records for tape in media.tapes])

    def test_cancel_wrong_read_does_not_loop_or_keep_partial_restore(self):
        class CancelMedia(TapeMedia):
            attempts = 0
            def request(self, backup_id, number, action):
                super().request(backup_id, number, action)
                if action == 'read' and number == 2:
                    self.attempts += 1
                    if self.attempts == 2:
                        raise tb.BackupError('Media change cancelled')
                    self.active = self.tapes[0]
        media = CancelMedia()
        full = self.create(media, cap=8 * tb.BLOCK_SIZE)
        media.loaded = False
        with self.assertRaisesRegex(tb.BackupError, 'cancelled'):
            tb.restore([full], self.root / 'restored', media, quiet=True)
        self.assertEqual(media.attempts, 2)
        self.assertFalse(list(self.root.glob('.restored.restoring-*')))

    def test_continuation_requests_next_cartridge_without_rewinding_or_reading_previous(self):
        media = TapeMedia()
        full = self.create(media, cap=8 * tb.BLOCK_SIZE)
        media.loaded = False
        original_next = tb.StreamReader.next_volume
        original_request = media.request
        previous = []
        transitions = []
        def next_volume(reader):
            if reader.number:
                previous[:] = [(media.active, media.active.cursor, media.active.reads)]
            return original_next(reader)
        def request(backup_id, number, action):
            if number > 1 and action == 'read':
                tape, cursor, reads = previous[0]
                self.assertIs(media.active, tape)
                self.assertEqual(tape.cursor, cursor, 'Repositioned the exhausted cartridge')
                self.assertEqual(tape.reads, reads, 'Reread the exhausted cartridge')
                transitions.append(number)
            return original_request(backup_id, number, action)
        with patch.object(tb.StreamReader, 'next_volume', next_volume), patch.object(media, 'request', request):
            destination = self.root / 'restored'
            tb.restore([full], destination, media, quiet=True)
        self.assertGreater(len(transitions), 1)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))

    def test_catalog_reads_each_header_once_with_one_eod_seek(self):
        media = TapeMedia()
        full = self.create(media)
        (self.source / 'new').write_text('delta')
        delta = self.create(media, base=full)
        operations = []
        original = TapeVolume.control
        def control(volume, operation, count=1):
            operations.append((operation, count))
            return original(volume, operation, count)
        with patch.object(TapeVolume, 'control', control), \
                patch.object(tb.os, 'memfd_create', side_effect=AssertionError('Unneeded snapshot allocation')):
            result = tb.inspect_all(media)
        self.assertEqual([b['id'] for b in result['backups']], [full, delta])
        self.assertEqual(sum(op == tb.MTEOM for op, count in operations), 1)
        positions = [count for op, count in operations if op == tb.MTSEEK]
        self.assertEqual(len(positions), 2)
        self.assertEqual(len(set(positions)), 2)

    def test_missing_backup_or_volume_in_valid_catalog_never_scans_archive_payload(self):
        media = TapeMedia()
        full = self.create(media)
        for backup_id, number in (('f' * 32, 1), (full, 2)):
            media.mt('rewind')
            volume = media.raw_open(False)
            try:
                with patch.object(TapeVolume, 'skip_payload', side_effect=AssertionError('Archive scan')):
                    self.assertFalse(tb.select_volume(volume, backup_id, number))
            finally:
                volume.close()

    def test_partial_catalog_cannot_hide_an_earlier_backup(self):
        media = TapeMedia()
        full = self.create(media)
        (self.source / 'new').write_text('one')
        first = self.create(media, full)
        (self.source / 'later').write_text('two')
        self.create(media, first)
        original = tb.latest_metadata
        def partial(*args, **kwargs):
            cached = original(*args, **kwargs)
            return {**cached, 'entries': cached['entries'][-1:]}
        media.mt('rewind')
        volume = media.raw_open(False)
        try:
            with patch.object(tb, 'latest_metadata', partial):
                self.assertTrue(tb.select_volume(volume, first, 1))
            self.assertEqual(tb.decoded_header(volume.read())['backup']['id'], first)
        finally:
            volume.close()

    def test_catalog_with_wrong_endpoints_is_not_trusted_for_negative_lookup(self):
        media = TapeMedia()
        full = self.create(media)
        original = tb.latest_metadata
        def wrong(*args, **kwargs):
            cached = original(*args, **kwargs)
            cached['entries'][0]['header_sha256'] = '0' * 64
            return cached
        media.mt('rewind')
        volume = media.raw_open(False)
        try:
            with patch.object(tb, 'latest_metadata', wrong), \
                    self.assertRaisesRegex(tb.BackupError, 'different or corrupt cartridge'):
                tb.select_volume(volume, 'f' * 32, 1)
        finally:
            volume.close()

    def test_inspection_reports_missing_catalog_without_scanning_unless_enabled(self):
        media = TapeMedia()
        with patch.object(tb, 'write_metadata', return_value=False):
            full = self.create(media)
        with patch.object(TapeVolume, 'skip_payload', side_effect=AssertionError('Unexpected archive scan')):
            result = tb.inspect_all(media, allow_scan=False)
        self.assertFalse(result['scan_complete'])
        self.assertTrue(result['errors'][0]['scan_required'])
        self.assertEqual(tb.inspect_all(media)['backups'][0]['id'], full)

    def test_incomplete_inspect_has_nonzero_status_and_retains_discovered_backup(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.create(media)
        tape = next(media.directory.glob('*.tape'))
        with tape.open('ab') as stream:
            stream.write(b'incomplete next backup')
        output = io.StringIO()
        with redirect_stdout(output):
            code = tb.main(['inspect', '--media-dir', str(media.directory)])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result['scan_complete'])
        self.assertEqual(result['backups'][0]['id'], full)

    def test_mode_aliases_and_different_homes_share_one_lock(self):
        with tb.drive_lock(os.makedev(9, 128)):
            with patch.object(Path, 'home', return_value=self.root / 'different-user'):
                for minor in (128, 160, 192, 224):
                    with self.subTest(minor=minor), self.assertRaisesRegex(tb.BackupError, 'Another operation'):
                        with tb.drive_lock(os.makedev(9, minor)):
                            self.fail('Competing drive operation acquired the lock')
            with tb.drive_lock(os.makedev(9, 129)):
                pass  # A separate physical drive is independent.
        with tb.drive_lock(os.makedev(9, 224)):
            pass

    def test_lock_symlink_is_rejected(self):
        (self.root / 'tape-backup-drive-9-0.lock').symlink_to(self.source / 'book')
        with self.assertRaisesRegex(tb.BackupError, 'shared drive lock'):
            with tb.drive_lock(os.makedev(9, 128)):
                self.fail('Followed a lock symlink')

    def test_nonregular_shared_lock_is_rejected_without_blocking_on_a_fifo(self):
        fifo = self.root / 'invalid-lock'
        os.mkfifo(fifo)
        result = subprocess.run([sys.executable, '-c',
            'import sys; from pathlib import Path; import tape_backup as tb\n'
            'try:\n'
            '    with tb.shared_lock(Path(sys.argv[1]), "restore lock", "busy"): pass\n'
            'except tb.BackupError as exc:\n'
            '    print(exc)\n', str(fifo)],
            cwd=Path(tb.__file__).resolve().parent, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Invalid shared restore lock', result.stdout)


if __name__ == '__main__':
    unittest.main()
