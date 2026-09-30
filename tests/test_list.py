"""List real GNU tar archives across framed, appended, and multi-tape backups."""
from contextlib import redirect_stderr, redirect_stdout
import io
import gzip
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_append import TapeMedia
from test_tape_backup import tree_contents


class ListTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book.m4b').write_bytes(os.urandom(300_000))
        (self.source / 'old.txt').write_text('original')
        (self.source / 'space and\nnewline').write_text('odd name')
        (self.source / 'link').symlink_to('book.m4b')
        (self.source / 'empty').mkdir()
        self.media = tb.FileMedia(self.root / 'media')
        self.errors = io.StringIO()
        context = redirect_stderr(self.errors)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def backup(self, media=None, base=None, limit=None):
        return tb.backup(self.source, media or self.media, quiet=True,
                         buffer_size=tb.BLOCK_SIZE, volume_size=limit,
                         level='incremental' if base else 'full', base=base)

    def listing(self, media=None, backup_id=None, *, scan=True):
        output = io.StringIO()
        with redirect_stdout(output):
            summary = tb.list_files(media or self.media, backup_id, scan=scan)
        self.assertEqual(summary['data_verified'], scan)
        return output.getvalue().splitlines(), summary

    def test_full_discovery_lists_names_without_extraction_or_media_changes(self):
        full = self.backup()
        before = tree_contents(self.root)
        names, summary = self.listing()
        self.assertEqual(summary['id'], full)
        self.assertEqual(set(names), {'./', './empty/', './book.m4b', './old.txt',
                                     './link', './space and\\nnewline'})
        self.assertEqual(tree_contents(self.root), before)
        self.assertIn('complete', self.errors.getvalue())

    def test_selects_appended_incremental_and_lists_only_archived_changes(self):
        full = self.backup()
        (self.source / 'old.txt').unlink()
        (self.source / 'new.txt').write_text('new')
        delta = self.backup(base=full)
        names, summary = self.listing(backup_id=delta)
        self.assertEqual(summary['id'], delta)
        self.assertIn('./new.txt', names)
        self.assertNotIn('./book.m4b', names)
        self.assertNotIn('./old.txt', names)
        names, _ = self.listing(backup_id=full)
        self.assertIn('./old.txt', names)
        self.assertNotIn('./new.txt', names)

    def test_physical_volume_changes_preserve_records_and_print_names_once(self):
        media = TapeMedia()
        full = self.backup(media, limit=8 * tb.BLOCK_SIZE)
        self.assertGreater(len(media.tapes), 1)
        before = [list(t.records) for t in media.tapes]
        media.requests.clear()
        media.loaded = False
        names, summary = self.listing(media, full)
        self.assertEqual(names.count('./book.m4b'), 1)
        self.assertEqual(summary['volumes'], len(media.tapes))
        self.assertEqual(media.requests, [(full, n, 'read')
                                         for n in range(1, len(media.tapes) + 1)])
        self.assertEqual([t.records for t in media.tapes], before)

    def test_missing_continuation_fails_instead_of_reporting_complete(self):
        full = self.backup(limit=8 * tb.BLOCK_SIZE)
        tapes = sorted(self.media.directory.glob('*.tape'))
        self.assertGreater(len(tapes), 1)
        tapes[-1].unlink()
        with redirect_stdout(io.StringIO()), self.assertRaises(tb.BackupError):
            tb.list_files(self.media, full)

    def test_valid_tar_with_corrupt_completion_is_not_a_successful_listing(self):
        full = self.backup()
        tape = next(self.media.directory.glob('*.tape'))
        data = bytearray(tape.read_bytes())
        for offset in range(0, len(data), tb.BLOCK_SIZE):
            record = data[offset:offset + tb.BLOCK_SIZE]
            if record.startswith(tb.MAGIC):
                head = tb.decoded_header(record)
                if head.get('kind') == 'end':
                    data[offset + tb.BLOCK_SIZE] ^= 1
                    break
        else:
            self.fail('No completion marker')
        tape.write_bytes(data)
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(tb.BackupError, 'Checksum mismatch'):
            tb.list_files(self.media, full, scan=True)

    def test_default_uses_index_without_tar_or_archive_reads(self):
        (self.source / 'book.m4b').write_bytes(os.urandom(4 * 1024**2))
        full = self.backup()
        self.assertTrue(self.media.last_result['filename_index'])
        expected, _ = self.listing(backup_id=full)
        reads = []
        original = tb.Volume.read
        def read(volume, **kwargs):
            record = original(volume, **kwargs)
            reads.append(len(record))
            return record
        with patch.object(tb, 'require_tar', side_effect=AssertionError('Started tar')), \
                patch.object(tb.StreamReader, 'frames', side_effect=AssertionError('Archive scan')), \
                patch.object(tb.Volume, 'read', read):
            names, result = self.listing(scan=False)
        self.assertEqual(names, expected)
        self.assertEqual(result['id'], full)
        self.assertEqual(result['listing_method'], 'index')
        self.assertFalse(result['completion_verified'])
        self.assertLess(sum(reads), 1024**2)
        self.assertIn('file data not verified', self.errors.getvalue())

    def test_index_preserves_long_sparse_link_unicode_and_escaped_names(self):
        (self.source / ('long-' + 'x' * 200)).write_text('long name')
        (self.source / 'éclair\\literal\t\x1b').write_text('special name')
        os.link(self.source / 'old.txt', self.source / 'hard-link')
        with (self.source / 'sparse').open('wb') as stream:
            stream.seek(16 * 1024**2)
            stream.write(b'end')
        full = self.backup()
        expected, _ = self.listing(backup_id=full)
        indexed, _ = self.listing(backup_id=full, scan=False)
        self.assertEqual(indexed, expected)
        self.assertNotIn('\x1b', '\n'.join(indexed))

    def test_appends_retain_each_backups_exact_index(self):
        full = self.backup()
        expected_full, _ = self.listing(backup_id=full)
        (self.source / 'old.txt').unlink()
        (self.source / 'added').write_text('new')
        delta = self.backup(base=full)
        expected_delta, _ = self.listing(backup_id=delta)
        with patch.object(tb.StreamReader, 'frames', side_effect=AssertionError('Archive scan')):
            self.assertEqual(self.listing(backup_id=full, scan=False)[0], expected_full)
            self.assertEqual(self.listing(backup_id=delta, scan=False)[0], expected_delta)
            self.assertEqual(self.listing(scan=False)[0], expected_full)

    def test_multitape_index_needs_only_final_cartridge(self):
        media = TapeMedia()
        full = self.backup(media, limit=8 * tb.BLOCK_SIZE)
        count = len(media.tapes)
        self.assertGreater(count, 1)
        expected, _ = self.listing(media, full)
        final = media.tapes[-1]
        before = list(final.records)
        media.tapes = [final]
        media.active = final
        media.requests.clear()
        with patch.object(tb.StreamReader, 'frames', side_effect=AssertionError('Archive scan')):
            names, summary = self.listing(media, scan=False)
        self.assertEqual(names, expected)
        self.assertEqual(summary['id'], full)
        self.assertEqual(summary['volumes'], count)
        self.assertEqual(media.requests, [(None, 0, 'read')])
        self.assertEqual(final.records, before)

    def test_released_tape_without_index_falls_back_to_verified_scan(self):
        self.media.directory.mkdir()
        fixture = Path(__file__).parent / 'fixtures/legacy-format3-full.tape.gz'
        with gzip.open(fixture, 'rb') as stream:
            data = stream.read()
        full = tb.decoded_header(data[:tb.BLOCK_SIZE])['backup']['id']
        (self.media.directory / f'{full}.0001.tape').write_bytes(data)
        output = io.StringIO()
        with redirect_stdout(output):
            result = tb.list_files(self.media)
        self.assertTrue(result['data_verified'])
        self.assertIn('./book.txt', output.getvalue().splitlines())
        self.assertIn('scanning all backup volumes', self.errors.getvalue())

    def test_corrupt_index_metadata_emits_nothing_then_allows_scan(self):
        full = self.backup()
        tape = next(self.media.directory.glob('*.tape'))
        data = bytearray(tape.read_bytes())
        for offset in range(0, len(data), tb.BLOCK_SIZE):
            record = data[offset:offset + tb.BLOCK_SIZE]
            if record.startswith(tb.MAGIC) and tb.decoded_header(record).get('type') == 'metadata':
                data[offset + tb.BLOCK_SIZE + 20] ^= 1
                break
        else:
            self.fail('No metadata file')
        tape.write_bytes(data)
        output = io.StringIO()
        with redirect_stdout(output), \
                patch.object(tb.StreamReader, 'frames', side_effect=AssertionError('Archive scan')), \
                self.assertRaisesRegex(tb.BackupError, 'No usable filename index'):
            tb.list_files(self.media, full, index_only=True)
        self.assertEqual(output.getvalue(), '')
        with redirect_stdout(output):
            result = tb.list_files(self.media, full)
        self.assertTrue(result['data_verified'])
        self.assertEqual(output.getvalue().splitlines().count('./book.m4b'), 1)

    def test_index_from_wrong_header_is_not_used(self):
        full = self.backup()
        original = tb.latest_metadata
        def wrong_header(*args, **kwargs):
            cached = original(*args, **kwargs)
            cached['entries'][0]['header_sha256'] = '0' * 64
            return cached
        output = io.StringIO()
        with patch.object(tb, 'latest_metadata', wrong_header), redirect_stdout(output), \
                self.assertRaisesRegex(tb.BackupError, 'No usable filename index'):
            tb.list_files(self.media, full, index_only=True)
        self.assertEqual(output.getvalue(), '')

    def test_index_and_catalog_limits_preserve_backup_and_scan_fallback(self):
        for constant, limit in (('MAX_FILE_INDEX_BYTES', 10), ('MAX_CATALOG_BYTES', 250)):
            with self.subTest(constant=constant):
                media = tb.FileMedia(self.root / constant)
                with patch.object(tb, constant, limit):
                    full = self.backup(media)
                self.assertFalse(media.last_result['filename_index'])
                self.assertTrue(media.last_result['metadata_complete'])
                self.assertTrue(media.last_result['append_ready'])
                self.assertTrue(any('Filename index unavailable' in w for w in media.last_result['warnings']))
                with redirect_stdout(io.StringIO()):
                    result = tb.list_files(media, full)
                self.assertTrue(result['data_verified'])

    def test_index_process_failure_does_not_fail_backup(self):
        original = tb.logged_process
        def process(args, **kwargs):
            if args[:2] == ['tar', '--list']:
                args = [sys.executable, '-c', 'raise SystemExit(9)']
            return original(args, **kwargs)
        with patch.object(tb, 'logged_process', process):
            full = self.backup()
        self.assertFalse(self.media.last_result['filename_index'])
        self.assertTrue(tb.scan(self.media, full)['data_verified'])

    def test_index_obeys_exclusions(self):
        full = tb.backup(self.source, self.media, excludes=['book.m4b', 'empty'],
                         quiet=True, buffer_size=tb.BLOCK_SIZE)
        expected, _ = self.listing(backup_id=full)
        names, _ = self.listing(backup_id=full, scan=False)
        self.assertEqual(names, expected)
        self.assertNotIn('./book.m4b', names)
        self.assertNotIn('./empty/', names)

    def test_invalid_index_text_is_never_printed(self):
        full = self.backup()
        original = tb.latest_metadata
        def invalid(*args, **kwargs):
            cached = original(*args, **kwargs)
            cached['entries'][-1]['file_index']['text'] = '\x1b[2Jfake\n'
            return cached
        output = io.StringIO()
        with patch.object(tb, 'latest_metadata', invalid), redirect_stdout(output), \
                self.assertRaisesRegex(tb.BackupError, 'No usable filename index'):
            tb.list_files(self.media, full, index_only=True)
        self.assertEqual(output.getvalue(), '')

    def test_index_does_not_claim_to_detect_corrupt_archive_data(self):
        full = self.backup()
        tape = next(self.media.directory.glob('*.tape'))
        data = bytearray(tape.read_bytes())
        data[2 * tb.BLOCK_SIZE] ^= 1  # First data frame's payload, away from metadata.
        tape.write_bytes(data)
        names, summary = self.listing(backup_id=full, scan=False)
        self.assertIn('./book.m4b', names)
        self.assertFalse(summary['data_verified'])
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(tb.BackupError, 'Checksum mismatch'):
            tb.list_files(self.media, full, scan=True)

    def test_first_cartridge_without_index_falls_back_without_reprompting_it(self):
        media = TapeMedia()
        full = self.backup(media, limit=8 * tb.BLOCK_SIZE)
        media.active = media.tapes[0]
        media.requests.clear()
        with redirect_stdout(io.StringIO()):
            result = tb.list_files(media, full)
        self.assertTrue(result['data_verified'])
        self.assertEqual(media.requests, [(full, 0, 'read'),
                                         *[(full, n, 'read') for n in range(2, len(media.tapes) + 1)]])

    def test_cli_index_only_and_scan_modes(self):
        full = self.backup()
        args = ['list', '--media-dir', str(self.media.directory), '--backup', full]
        outputs = []
        for mode in ('--index-only', '--scan'):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(tb.main([*args, mode]), 0)
            outputs.append(output.getvalue())
        self.assertEqual(*outputs)
        with self.assertRaises(SystemExit):
            tb.make_parser().parse_args([*args, '--scan', '--index-only'])

    def test_cli_honors_media_lock_and_keeps_stdout_for_file_names(self):
        full = self.backup()
        args = ['list', '--media-dir', str(self.media.directory), '--backup', full]
        output = io.StringIO()
        with redirect_stdout(output):
            with self.media.lock():
                self.assertEqual(tb.main(args), 1)
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(tb.main(args), 0)
        self.assertIn('./book.m4b', output.getvalue())
        self.assertNotIn(full, output.getvalue())
        self.assertNotIn('MiB/s', output.getvalue())

    def test_list_stdout_and_stderr_wait_for_media_prompt_lock(self):
        output, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            process = None
            try:
                with tb.OUTPUT_LOCK:
                    process = tb.logged_process([sys.executable, '-c',
                        "import sys; print('./book.m4b'); print('diagnostic', file=sys.stderr)"],
                        result_stdout=True, stdin=subprocess.DEVNULL)
                    self.assertEqual(process.wait(timeout=3), 0)
                    self.assertEqual(output.getvalue(), '')
                    self.assertEqual(diagnostics.getvalue(), '')
            finally:
                tb.stop_process(process)
        self.assertEqual(output.getvalue(), './book.m4b\n')
        self.assertEqual(diagnostics.getvalue(), 'diagnostic\n')


if __name__ == '__main__':
    unittest.main()
