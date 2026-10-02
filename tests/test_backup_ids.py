"""Short backup identities, collision handling, and legacy chain compatibility."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_cli_status import TerminalOutput, without_color
from test_tape_backup import tree_contents


class BackupIDTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = tb.FileMedia(self.root / 'media')
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book').write_text('original')
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def backup(self, **options):
        return tb.backup(self.source, self.media, quiet=True, buffer_size=tb.BLOCK_SIZE, **options)

    def test_short_and_legacy_ids_are_accepted_without_weakening_cartridge_ids(self):
        for identifier in ('7aQm3Kx', '1234567', 'abcdef1', '0123456789abcdef' * 2):
            self.assertEqual(tb.valid_id(identifier), identifier)
        for identifier in (None, 1234567, '', 'abc123', 'abc12345', '0aQm3Kx',
                           'OaQm3Kx', 'IaQm3Kx', 'laQm3Kx', 'a' * 31, 'a' * 33,
                           '../abcd', 'abc/def', 'abc def', '7aQm3Kx\n'):
            with self.subTest(identifier=identifier), self.assertRaises(tb.BackupError):
                tb.valid_id(identifier)
        self.assertEqual(tb.valid_cartridge_id('0' * 32), '0' * 32)
        with self.assertRaisesRegex(tb.BackupError, 'cartridge ID'):
            tb.cartridge_identity({'cartridge_identity': {'id': '7aQm3Kx', 'label': None}})

    def test_generation_retries_inventory_catalog_parent_and_ancestor_collisions(self):
        media = SimpleNamespace(entries=[{'id': 'AAAAAAA'}],
                                inventory_entries=[{'id': 'BBBBBBB', 'parent': 'CCCCCCC',
                                                    'ancestors': ['CCCCCCC', 'DDDDDDD']}])
        parent = {'id': 'EEEEEEE', 'ancestors': ['FFFFFFF']}
        append_job = {'id': 'GGGGGGG', 'ancestors': ['HHHHHHH']}
        candidates = 'AAAAAAABBBBBBBCCCCCCCDDDDDDDEEEEEEEFFFFFFFGGGGGGGHHHHHHH7aQm3Kx'
        with patch.object(tb.secrets, 'choice', side_effect=candidates):
            self.assertEqual(tb.new_backup_id(media, parent, append_job), '7aQm3Kx')

    def test_fresh_media_object_checks_appended_ids_not_only_tape_filenames(self):
        with patch.object(tb.secrets, 'choice', side_effect='AAAAAAABBBBBBB'):
            full = self.backup()
            appended = self.backup(append_after=full)
        paths = list(self.media.directory.glob('*.tape'))
        self.assertEqual([path.name for path in paths], [f'{full}.0001.tape'])
        original = paths[0].read_bytes()
        self.media = tb.FileMedia(self.media.directory)
        with patch.object(tb.secrets, 'choice', side_effect=appended + 'CCCCCCC'):
            independent = self.backup()
        self.assertEqual(independent, 'CCCCCCC')
        self.assertEqual(paths[0].read_bytes(), original)
        for identifier in (full, appended, independent):
            self.assertTrue(tb.scan(self.media, identifier)['data_verified'])

    def test_unreadable_tape_filename_still_reserves_its_id(self):
        self.media.directory.mkdir()
        path = self.media.directory / 'AAAAAAA.0001.tape'
        path.write_bytes(b'incomplete tape header')
        with patch.object(tb.secrets, 'choice', side_effect='AAAAAAABBBBBBB'):
            self.assertEqual(tb.new_backup_id(self.media), 'BBBBBBB')
        self.assertEqual(path.read_bytes(), b'incomplete tape header')

    def test_repeated_collisions_fail_before_source_or_tape_writes(self):
        self.media.inventory_entries = [{'id': 'AAAAAAA'}]
        with patch.object(tb.secrets, 'choice', return_value='A'), \
                patch.object(tb, 'start_archive', side_effect=AssertionError('Started archive')), \
                patch.object(tb.StreamWriter, 'next_volume', side_effect=AssertionError('Wrote tape')), \
                self.assertRaisesRegex(tb.BackupError, 'unused backup ID'):
            self.backup()
        self.assertEqual(list(self.media.directory.glob('*.tape')), [])

    def test_short_incrementals_extend_legacy_backup_and_restore_history(self):
        legacy = '0123456789abcdef' * 2
        with patch.object(tb, 'new_backup_id', return_value=legacy):
            full = self.backup()
        destination = self.root / 'restored'
        tb.restore([full], destination, self.media, quiet=True)
        (self.source / 'book').write_text('changed')
        delta = self.backup(level='incremental', base=full)
        self.assertEqual(len(delta), 7)
        entries = tb.inspect_all(self.media)['backups']
        self.assertEqual(tb.restore_plan(delta, entries)['backup_ids'], [full, delta])
        self.assertEqual({len(entry['cartridge_id']) for entry in entries}, {32})
        tb.restore([delta], destination, self.media, quiet=True)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))
        self.assertEqual(tb.read_restore_marker(destination)['id'], delta)
        (self.source / 'book').unlink()
        (self.source / 'new').write_text('next incremental')
        last = self.backup(level='incremental', base=delta)
        tb.restore([last], destination, self.media, quiet=True)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))
        self.assertEqual(tb.read_restore_marker(destination)['id'], last)

    def test_stream_backup_uses_complete_short_id_and_restores_exact_bytes(self):
        with tempfile.TemporaryFile() as source:
            source.write(b'bytes\x00from a stream\xff')
            source.seek(0)
            identifier = tb.backup_stream('test stream', self.media, input_fd=source.fileno(),
                                         buffer_size=tb.BLOCK_SIZE)
        self.assertEqual(len(identifier), 7)
        self.assertEqual(tb.inspect_backup(self.media, identifier)['id'], identifier)
        output = io.BytesIO()
        tb.restore_stream(identifier, self.media, output)
        self.assertEqual(output.getvalue(), b'bytes\x00from a stream\xff')

    def test_progress_labels_distinguish_short_ids_from_restore_action(self):
        for label, options, expected in (('7aQm3Kx', {'buffer_size': tb.BLOCK_SIZE}, 'Backup 7aQm3Kx'),
                                         ('Restore', {}, 'Restore')):
            output = TerminalOutput()
            progress = tb.Progress(label, **options)
            with redirect_stderr(output):
                progress.report()
            self.assertEqual(without_color(output.getvalue()).strip().splitlines()[0], expected)


if __name__ == '__main__':
    unittest.main()
