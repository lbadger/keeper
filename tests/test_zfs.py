"""Native stream framing plus opt-in OpenZFS kernel integration tests."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import tape_backup as tb
from ssh_fixture import SSHServer
from test_append import TapeMedia
from test_tape_backup import tree_contents


def identity(snapshot='tank/books@one', guid='1001', parent=None, raw=False):
    return {'snapshot': snapshot, 'dataset': snapshot.split('@')[0], 'guid': guid,
            'base_snapshot': parent['snapshot'] if parent else None,
            'base_guid': parent['guid'] if parent else None, 'raw': raw}


class ZFSStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = tb.FileMedia(self.root / 'media')
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def create(self, metadata, base=None, limit=None, fail=False, **options):
        def properties(name, keys):
            values = {'type': 'snapshot' if '@' in name else 'filesystem', 'encryption': 'off',
                      'guid': metadata['base_guid'] if name == metadata['base_snapshot'] else metadata['guid']}
            return {key: values[key] for key in keys}
        def source(value):
            self.assertEqual(value, metadata)
            return tb.logged_process([sys.executable, '-c',
                'import sys; sys.stdout.buffer.write(b"test zfs payload" * 50000); '
                f'sys.exit({1 if fail else 0})'], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        with patch.object(tb.shutil, 'which', return_value='/zfs'), \
                patch.object(tb, 'zfs_properties', properties), \
                patch.object(tb, 'run_command', return_value='size\t800000\n'), \
                patch.object(tb, 'start_zfs', source):
            return tb.backup_zfs(metadata['snapshot'], self.media, base=base,
                                 buffer_size=tb.BLOCK_SIZE, volume_size=limit, quiet=True, **options)

    def test_independent_full_then_incremental_preserve_existing_cartridge(self):
        for media in (self.media, TapeMedia()):
            with self.subTest(physical=isinstance(media, TapeMedia)):
                self.media = media
                first = self.create(identity(raw=True), raw=True)
                if isinstance(media, TapeMedia):
                    tape = media.tapes[0]
                    records = lambda: list(tape.records)
                else:
                    tape = next(media.directory.glob('*.tape'))
                    records = tape.read_bytes
                before = records()
                metadata = identity('tank/photos@one', '2001')
                second = self.create(metadata, append_after=first, verify=True)
                delta_metadata = identity('tank/photos@two', '2002', metadata)
                delta = self.create(delta_metadata, base=second)
                self.assertEqual(records()[:len(before)], before)
                self.assertEqual(len(media.tapes) if isinstance(media, TapeMedia) else
                                 len(list(media.directory.glob('*.tape'))), 1)
                for backup_id, parent in ((first, None), (second, None), (delta, second)):
                    summary = tb.scan(media, backup_id)
                    self.assertTrue(summary['data_verified'])
                    self.assertEqual(summary['parent'], parent)
                    self.assertEqual(summary['level'], 'incremental' if parent else 'full')
                    self.assertEqual(summary['ancestors'], [parent] if parent else [])
                entries = tb.inspect_all(media)['backups']
                self.assertEqual([entry['id'] for entry in entries], [first, second, delta])
                self.assertEqual(tb.restore_plan(second, entries)['backup_ids'], [second])
                self.assertEqual(tb.restore_plan(delta, entries)['backup_ids'], [second, delta])

    def test_incremental_dataset_and_raw_mismatch_do_not_write_to_tape(self):
        first = self.create(identity())
        before = tree_contents(self.media.directory)
        for metadata, options, error in (
                (identity('tank/photos@one', '2001'), {}, "tank/photos.*tank/books.*--append-after"),
                (identity('tank/books@two', '1002', raw=True), {'raw': True},
                 'raw send mode True differs from parent mode False')):
            with self.subTest(error=error), self.assertRaisesRegex(tb.BackupError, error):
                self.create(metadata, base=first, **options)
            self.assertEqual(tree_contents(self.media.directory), before)
            self.assertIsNone(self.media.append_volume)
            self.assertFalse(self.media.append_mode)

    def test_incremental_still_inherits_raw_mode(self):
        metadata = identity(raw=True)
        first = self.create(metadata, raw=True)
        delta = self.create(identity('tank/books@two', '1002', metadata, raw=True), base=first)
        self.assertTrue(tb.scan(self.media, delta)['zfs']['raw'])

    def test_independent_append_requires_completed_tail(self):
        for media in (self.media, TapeMedia()):
            with self.subTest(physical=isinstance(media, TapeMedia)):
                self.media = media
                first = self.create(identity())
                second = self.create(identity('tank/photos@one', '2001'), append_after=first)
                metadata = identity('tank/music@one', '3001')
                before = (list(media.active.records) if isinstance(media, TapeMedia) else
                          tree_contents(media.directory))
                with self.assertRaisesRegex(tb.BackupError, 'latest completed|follows the base'):
                    self.create(metadata, append_after=first)
                after = (list(media.active.records) if isinstance(media, TapeMedia) else
                         tree_contents(media.directory))
                self.assertEqual(before, after)
                with self.assertRaisesRegex(tb.BackupError, 'ZFS send failed'):
                    self.create(metadata, append_after=second, fail=True)
                before = (list(media.active.records) if isinstance(media, TapeMedia) else
                          tree_contents(media.directory))
                with self.assertRaises(tb.BackupError):
                    self.create(metadata, append_after=second)
                after = (list(media.active.records) if isinstance(media, TapeMedia) else
                         tree_contents(media.directory))
                self.assertEqual(before, after)
                for backup_id in (first, second):
                    self.assertTrue(tb.scan(media, backup_id)['data_verified'])

    def test_independent_append_after_tar_preserves_restore_and_filename_index(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'book').write_text('original data')
        first = tb.backup(source, self.media, buffer_size=tb.BLOCK_SIZE, quiet=True)
        second = self.create(identity(), append_after=first)
        restored = self.root / 'restored'
        tb.restore([first], restored, self.media, quiet=True)
        self.assertEqual(tree_contents(restored), tree_contents(source))
        output = io.StringIO()
        with redirect_stdout(output):
            tb.list_files(self.media, first, index_only=True)
        self.assertIn('book', output.getvalue())
        self.assertTrue(tb.scan(self.media, second)['data_verified'])

    def test_tar_full_can_append_after_zfs_without_reusing_its_snapshot(self):
        first = self.create(identity())
        source = self.root / 'source'
        source.mkdir()
        (source / 'book').write_text('independent tar data')
        tape = next(self.media.directory.glob('*.tape'))
        before = tape.read_bytes()
        second = tb.backup(source, self.media, append_after=first, quiet=True, verify=True)
        self.assertEqual(tape.read_bytes()[:len(before)], before)
        self.assertEqual(len(list(self.media.directory.glob('*.tape'))), 1)
        self.assertTrue(tb.scan(self.media, first)['data_verified'])
        tb.restore([second], self.root / 'restored', self.media, quiet=True)
        self.assertEqual(tree_contents(source), tree_contents(self.root / 'restored'))

    def test_independent_append_rollover_protects_previous_backup_chain(self):
        self.media = TapeMedia()
        metadata = identity()
        first = self.create(metadata, limit=512 * 1024)
        delta = self.create(identity('tank/books@two', '1002', metadata), base=first, limit=512 * 1024)
        tapes = [(tape, list(tape.records)) for tape in self.media.tapes]
        # A smaller cap forces the appended full to begin on a fresh cartridge.
        second = self.create(identity('tank/photos@one', '2001'), append_after=delta, limit=256 * 1024)
        self.assertTrue({first, delta, second}.issubset(self.media.protected_backup_ids))
        self.assertTrue(self.media.protected_headers)
        for tape, records in tapes:
            self.assertEqual(tape.records, records)
        for backup_id in (first, delta, second):
            self.assertTrue(tb.scan(self.media, backup_id)['data_verified'])
        summary = tb.scan(self.media, second)
        self.assertEqual(summary['ancestors'], [])
        self.assertIsNone(summary['parent'])

    def test_append_after_cli_preview_and_write(self):
        first = self.create(identity())
        metadata = identity('tank/photos@one', '2001')
        args = ['zfs-backup', '--snapshot', metadata['snapshot'], '--append-after', first,
                '--media-dir', str(self.media.directory), '--buffer-size', '64KiB', '--json',
                '--log-file', str(self.root / 'cli.log')]
        output = io.StringIO()
        before = tree_contents(self.media.directory)
        with patch.object(tb, 'prepare_zfs', return_value={'source': metadata['dataset'],
                'estimated_bytes': 800000, 'zfs': metadata}), \
                patch.object(tb, 'prepare_append', side_effect=AssertionError('Prepared tape for writing')), \
                patch.object(tb, 'start_zfs', side_effect=AssertionError('Started send')), redirect_stdout(output):
            self.assertEqual(tb.main([*args, '--dry-run']), 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(preview['append_after'], first)
        self.assertEqual(preview['level'], 'full')
        self.assertIsNone(preview['parent'])
        self.assertIn(first, tb.format_backup_preview(preview))
        self.assertEqual(before, tree_contents(self.media.directory))
        output = io.StringIO()
        with patch.object(tb, 'prepare_zfs', return_value={'source': metadata['dataset'],
                'estimated_bytes': 800000, 'zfs': metadata}), \
                patch.object(tb, 'start_zfs', side_effect=lambda value: tb.logged_process(
                    [sys.executable, '-c', 'print("test zfs payload")'],
                    stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)), redirect_stdout(output):
            self.assertEqual(tb.main([*args, '--verify']), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result['data_verified'])
        self.assertIsNone(result['parent'])
        self.assertEqual(len(list(self.media.directory.glob('*.tape'))), 1)

    def test_base_and_append_after_are_mutually_exclusive_before_media_access(self):
        with self.assertRaisesRegex(tb.BackupError, 'cannot be combined'):
            tb.backup_zfs('tank/books@one', None, base='a' * 32, append_after='b' * 32)
        with self.assertRaises(SystemExit) as error:
            tb.make_parser().parse_args(['zfs-backup', '--snapshot', 'tank/books@one',
                                         '--base', 'a' * 32, '--append-after', 'b' * 32])
        self.assertEqual(error.exception.code, 2)

    def test_full_and_incremental_multivolume_streams_verify(self):
        first = identity()
        full = self.create(first, limit=512 * 1024)
        second = identity('tank/books@two', '1002', first)
        delta = self.create(second, base=full, limit=512 * 1024)
        for backup_id, expected in ((full, first), (delta, second)):
            info = tb.scan(self.media, backup_id)
            self.assertTrue(info['data_verified'])
            self.assertGreater(info['volumes'], 1)
            self.assertEqual(info['zfs'], expected)
            self.assertEqual(info['archive_type'], 'zfs')
            self.assertEqual(info['ancestors'], [full] if backup_id == delta else [])
            self.assertTrue(info['ancestry_complete'])
        first_tape = self.media.directory / f'{full}.0001.tape'
        self.assertTrue(first_tape.read_bytes().startswith(tb.ZFS_MAGIC))

    def test_send_failure_never_commits_a_complete_backup(self):
        with self.assertRaisesRegex(tb.BackupError, 'ZFS send failed'):
            self.create(identity(), fail=True)
        backup_id = next(self.media.directory.glob('*.tape')).name.split('.')[0]
        with self.assertRaises(tb.BackupError):
            tb.scan(self.media, backup_id)

    def test_zfs_preview_and_multivolume_plan_without_starting_another_send(self):
        first = identity()
        full = self.create(first, limit=512 * 1024)
        second = identity('tank/books@two', '1002', first)
        with patch.object(tb, 'prepare_zfs', return_value={'source': second['dataset'],
                'estimated_bytes': 800000, 'zfs': second}), \
                patch.object(tb, 'start_zfs', side_effect=AssertionError('started send')):
            preview = tb.backup_zfs(second['snapshot'], self.media, base=full, dry_run=True)
        self.assertEqual(preview['parent'], full)
        delta = self.create(second, base=full, limit=512 * 1024)
        entries = tb.inspect_all(self.media, allow_scan=True)['backups']
        plan = tb.restore_plan(delta, entries)
        self.assertTrue(plan['plan_complete'])
        self.assertEqual(plan['backup_ids'], [full, delta])

    def test_tar_listing_and_restore_reject_native_stream_before_spawning_tar(self):
        full = self.create(identity())
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(tb.BackupError, 'ZFS streams'):
            tb.list_files(self.media, full)
        with patch.object(tb, 'extract_restore_stream', side_effect=AssertionError('Tar was started')):
            with self.assertRaisesRegex(tb.BackupError, 'native ZFS'):
                tb.restore([full], self.root / 'restored', self.media)
        self.assertFalse((self.root / 'restored').exists())

    def test_zfs_parent_cannot_be_used_for_a_tar_incremental(self):
        full = self.create(identity())
        source = self.root / 'source'
        source.mkdir()
        before = tree_contents(self.media.directory)
        with self.assertRaisesRegex(tb.BackupError, 'tar parent'):
            tb.backup(source, self.media, level='incremental', base=full)
        self.assertEqual(before, tree_contents(self.media.directory))

    def test_encryption_requires_explicit_raw_and_base_guid_cannot_change(self):
        metadata = identity()
        def properties(name, keys):
            if '@' not in name:
                return {'type': 'filesystem', 'encryption': 'aes-256-gcm'}
            return {'type': 'snapshot', 'guid': '9999'}
        with patch.object(tb.shutil, 'which', return_value='/zfs'), patch.object(tb, 'zfs_properties', properties):
            with self.assertRaisesRegex(tb.BackupError, 'require --raw'):
                tb.prepare_zfs('tank/books@one')
            metadata['raw'] = True
            with self.assertRaisesRegex(tb.BackupError, 'base snapshot changed'):
                tb.prepare_zfs('tank/books@two', metadata, raw=True)

    def test_zfs_commands_never_force_receive_or_use_shell_interpolation(self):
        parent = identity(raw=True)
        metadata = identity('tank/books@two', '1002', parent, raw=True)
        self.assertEqual(tb.zfs_send_command(metadata),
                         ['zfs', 'send', '-p', '-w', '-i', 'tank/books@one', 'tank/books@two'])
        for name in ('-danger@snap', 'tank/fs;touch x@snap', 'tank/../fs@snap', 'tank/fs'):
            with self.subTest(name=name), self.assertRaises(tb.BackupError):
                tb.zfs_name(name, snapshot=True)

    def test_zfs_restore_uses_a_shared_destination_lock_across_homes(self):
        target = 'tank/recovered'
        with tb.restore_lock(target, zfs=True), \
                patch.object(Path, 'home', return_value=self.root / 'other-home'), \
                patch.object(tb.shutil, 'which', return_value='/zfs'), \
                patch.object(tb, 'zfs_target_exists', side_effect=AssertionError('Touched locked destination')):
            with self.assertRaisesRegex(tb.BackupError, 'Another operation is restoring'):
                tb.restore_zfs(['a' * 32], target, self.media)


class ZFSSSHTests(unittest.TestCase):
    def test_real_ssh_transport_streams_native_full_and_incremental_sources(self):
        # The SSH and framing are real; this controlled executable models the
        # OpenZFS subprocess interface. Kernel behavior is covered separately.
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            root = Path(temporary)
            server = SSHServer()
            self.addCleanup(server.close)
            tools = root / 'tools'
            tools.mkdir()
            fake = tools / 'zfs'
            fake.write_text('#!' + sys.executable + '''
import sys
args = sys.argv[1:]
if args[0] == 'get':
    properties, name = args[-2:]
    values = {'type': 'snapshot' if '@' in name else 'filesystem',
              'encryption': 'off', 'guid': '1002' if name.endswith('@two') else '1001'}
    for prop in properties.split(','):
        print(prop + '\\t' + values[prop])
elif args[0] == 'send' and '-nP' in args:
    print('size\\t800000')
elif args[0] == 'send':
    sys.stdout.buffer.write(b'native SSH stream' * 50000)
else:
    sys.exit(2)
''')
            fake.chmod(0o700)
            helper = root / 'remote helper'
            helper.write_text('#!/bin/sh\nexec env ' + shlex.quote('PATH=' + str(tools) + ':' + os.environ['PATH']) +
                ' ' + shlex.join([sys.executable, str(Path(tb.__file__).resolve().with_name('keeper.py'))]) + ' "$@"\n')
            helper.chmod(0o700)
            ssh = tb.SSHConfig('backup-source', config=server.config, program=str(helper))
            media = tb.FileMedia(root / 'media')
            full = tb.backup_zfs('tank/books@one', media, ssh=ssh, buffer_size=tb.BLOCK_SIZE,
                                 volume_size=512 * 1024)
            delta = tb.backup_zfs('tank/books@two', media, ssh=ssh, base=full,
                                  buffer_size=tb.BLOCK_SIZE, volume_size=512 * 1024)
            info = tb.scan(media, delta)
            self.assertTrue(info['data_verified'])
            self.assertEqual(info['parent'], full)
            self.assertEqual(info['zfs']['base_guid'], '1001')
            self.assertEqual(info['zfs']['guid'], '1002')
            self.assertEqual(info['ssh']['host'], 'backup-source')
            independent = tb.backup_zfs('tank/photos@one', media, ssh=ssh, append_after=delta,
                                        raw=True, buffer_size=tb.BLOCK_SIZE, volume_size=512 * 1024)
            info = tb.scan(media, independent)
            self.assertTrue(info['data_verified'])
            self.assertEqual(info['zfs']['dataset'], 'tank/photos')
            self.assertTrue(info['zfs']['raw'])
            self.assertIsNone(info['zfs']['base_snapshot'])
            self.assertIsNone(info['parent'])
            self.assertEqual(info['ancestors'], [])


@unittest.skipUnless(os.environ.get('TAPE_BACKUP_ZFS_TEST_POOL'),
                     'Set TAPE_BACKUP_ZFS_TEST_POOL to a dedicated scratch pool for real ZFS tests')
class ZFSKernelTests(unittest.TestCase):
    def setUp(self):
        pool = os.environ['TAPE_BACKUP_ZFS_TEST_POOL']
        tb.zfs_name(pool)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = pool + '/tape-backup-test-' + uuid.uuid4().hex
        subprocess.run(['zfs', 'create', '-o', 'mountpoint=none', self.dataset], check=True)
        self.addCleanup(subprocess.run, ['zfs', 'destroy', '-r', self.dataset], check=True)
        self.source_dataset = self.dataset + '/source'
        self.source = self.root / 'source'
        subprocess.run(['zfs', 'create', '-o', f'mountpoint={self.source}', '-o', 'compression=lz4',
                        self.source_dataset], check=True)
        (self.source / 'book').write_bytes(os.urandom(700_000))
        (self.source / 'deleted').write_text('original')
        (self.source / 'empty').mkdir()
        (self.source / 'link').symlink_to('book')
        self.media = tb.FileMedia(self.root / 'media')

    def snapshot(self, name):
        snapshot = self.source_dataset + '@' + name
        subprocess.run(['zfs', 'snapshot', snapshot], check=True)
        return snapshot

    def view(self, dataset, name):
        path = self.root / name
        subprocess.run(['zfs', 'set', f'mountpoint={path}', 'canmount=on', dataset], check=True)
        if tb.zfs_properties(dataset, ['mounted'])['mounted'] != 'yes':
            subprocess.run(['zfs', 'mount', dataset], check=True)
        return path

    def test_real_full_incremental_and_stepwise_receive_across_volumes(self):
        full = tb.backup_zfs(self.snapshot('one'), self.media, buffer_size=tb.BLOCK_SIZE,
                             volume_size=512 * 1024)
        self.assertTrue(tb.scan(self.media, full)['data_verified'])
        first_tree = tree_contents(self.source)
        (self.source / 'book').write_text('changed')
        (self.source / 'deleted').unlink()
        (self.source / 'added').write_text('new')
        delta = tb.backup_zfs(self.snapshot('two'), self.media, base=full, buffer_size=tb.BLOCK_SIZE,
                              volume_size=512 * 1024)
        destination = self.dataset + '/all'
        output = io.StringIO()
        with redirect_stderr(output):
            tb.restore_zfs([full, delta], destination, self.media)
        self.assertEqual(output.getvalue().count('Total 100.0% (complete)'), 1)
        self.assertEqual(tree_contents(self.view(destination, 'all')), tree_contents(self.source))
        steps = self.dataset + '/steps'
        tb.restore_zfs([full], steps, self.media)
        self.assertEqual(tree_contents(self.view(steps, 'steps')), first_tree)
        subprocess.run(['zfs', 'unmount', steps], check=True)
        tb.restore_zfs([delta], steps, self.media)
        self.assertEqual(tree_contents(self.view(steps, 'steps')), tree_contents(self.source))
        with self.assertRaises(tb.BackupError):
            tb.restore_zfs([full], destination, self.media)

    def test_real_raw_encrypted_snapshot_round_trip(self):
        encrypted = self.dataset + '/encrypted'
        key = self.root / 'key'
        key.write_text(uuid.uuid4().hex)
        source = self.root / 'encrypted'
        subprocess.run(['zfs', 'create', '-o', 'encryption=on', '-o', 'keyformat=passphrase',
                        '-o', f'keylocation=file://{key}', '-o', f'mountpoint={source}', encrypted], check=True)
        (source / 'secret').write_text('encrypted payload')
        snapshot = encrypted + '@one'
        subprocess.run(['zfs', 'snapshot', snapshot], check=True)
        with self.assertRaisesRegex(tb.BackupError, '--raw'):
            tb.backup_zfs(snapshot, self.media)
        full = tb.backup_zfs(snapshot, self.media, raw=True, buffer_size=tb.BLOCK_SIZE)
        destination = self.dataset + '/raw-restored'
        tb.restore_zfs([full], destination, self.media)
        self.assertNotEqual(tb.zfs_properties(destination, ['encryption'])['encryption'], 'off')
        subprocess.run(['zfs', 'load-key', '-L', f'file://{key}', destination], check=True)
        self.assertEqual(tree_contents(self.view(destination, 'raw-restored')), tree_contents(source))

    def test_real_independent_datasets_share_cartridge_and_restore_separately(self):
        first = tb.backup_zfs(self.snapshot('one'), self.media, buffer_size=tb.BLOCK_SIZE)
        tape = next(self.media.directory.glob('*.tape'))
        before = tape.read_bytes()
        other_dataset = self.dataset + '/other'
        other_source = self.root / 'other'
        subprocess.run(['zfs', 'create', '-o', f'mountpoint={other_source}', other_dataset], check=True)
        (other_source / 'photo').write_bytes(os.urandom(100_000))
        subprocess.run(['zfs', 'snapshot', other_dataset + '@one'], check=True)
        second = tb.backup_zfs(other_dataset + '@one', self.media, append_after=first,
                               buffer_size=tb.BLOCK_SIZE, verify=True)
        self.assertEqual(len(list(self.media.directory.glob('*.tape'))), 1)
        self.assertEqual(tape.read_bytes()[:len(before)], before)
        for backup_id, source, name in ((first, self.source, 'books-restored'),
                                        (second, other_source, 'photos-restored')):
            destination = self.dataset + '/' + name
            tb.restore_zfs([backup_id], destination, self.media)
            self.assertEqual(tree_contents(self.view(destination, name)), tree_contents(source))

    @unittest.skipUnless(os.environ.get('TAPE_BACKUP_BINARY'), 'Set TAPE_BACKUP_BINARY for native ZFS binary tests')
    def test_real_standalone_binary_zfs_full_and_incremental_restore(self):
        binary = str(Path(os.environ['TAPE_BACKUP_BINARY']).resolve())
        def run(*args):
            result = subprocess.run([binary, *map(str, args)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        inventory = self.root / 'inventory.json'
        common = ['--media-dir', self.media.directory, '--buffer-size', '64KiB', '--volume-size', '512KiB',
                  '--inventory', inventory, '--label-prefix', 'ZFS']
        first_snapshot = self.snapshot('one')
        preview = json.loads(run('zfs-backup', '--snapshot', first_snapshot, '--dry-run', '--json'))
        self.assertTrue(preview['dry_run'])
        self.assertFalse(self.media.directory.exists())
        full = json.loads(run('zfs-backup', '--snapshot', first_snapshot, *common, '--json', '--verify'))
        self.assertTrue(full['data_verified'])
        (self.source / 'deleted').unlink()
        (self.source / 'new').write_text('binary incremental')
        delta = run('zfs-backup', '--snapshot', self.snapshot('two'), '--base', full['id'], *common)
        plan = json.loads(run('zfs-restore', '--to', delta, '--plan', '--inventory', inventory, '--json'))
        self.assertTrue(plan['plan_complete'])
        self.assertEqual(plan['backup_ids'], [full['id'], delta])
        target = self.dataset + '/binary-restored'
        run('zfs-restore', '--to', delta, '--inventory', inventory, '--dataset', target,
            '--media-dir', self.media.directory)
        self.assertEqual(tree_contents(self.view(target, 'binary-restored')), tree_contents(self.source))


if __name__ == '__main__':
    unittest.main()
