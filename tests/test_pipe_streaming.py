"""Opaque streams preserve bytes and never commit failed managed producers."""
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import tape_backup as tb
from ssh_fixture import SSHServer
from test_tape_backup import FaultMedia


SCRIPT = str(Path(tb.__file__).resolve().with_name('keeper.py'))
CLI = [os.environ['TAPE_BACKUP_BINARY']] if os.environ.get('TAPE_BACKUP_BINARY') else [sys.executable, SCRIPT]


class PipeStreamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = tb.FileMedia(self.root / 'media')
        self.errors = io.StringIO()
        quiet = redirect_stderr(self.errors)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def backup(self, payload=b'opaque\x00stream\xff\n', **kwargs):
        with tempfile.TemporaryFile() as source:
            source.write(payload)
            source.seek(0)
            return tb.backup_stream('test source', self.media, input_fd=source.fileno(),
                                    buffer_size=128 * 1024, **kwargs)

    def command(self, code, **kwargs):
        return tb.backup_stream('managed source', self.media,
                                command=[sys.executable, '-c', code],
                                buffer_size=128 * 1024, **kwargs)

    def cli(self, *args, **kwargs):
        return subprocess.run([*CLI, *args], capture_output=True,
                              timeout=15, **kwargs)

    def assert_incomplete(self):
        self.assertFalse(getattr(self.media, 'last_result', {}).get('archive_complete'))
        with self.assertRaises(tb.BackupError):
            tb.scan(self.media)

    def test_stdin_round_trip_across_volumes_inventory_and_readback(self):
        payload = os.urandom(800_003)
        self.media.inventory_output = self.root / 'inventory.json'
        backup_id = self.backup(payload, volume_size=512 * 1024,
                                expected_bytes=len(payload), verify=True)
        result = self.media.last_result
        self.assertGreater(result['volumes'], 1)
        self.assertTrue(result['data_verified'])
        self.assertFalse(result['append_ready'])
        self.assertEqual(result['snapshot_bytes'], 0)
        self.assertEqual(result['data_sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result['stream']['completion'], 'stdin-eof')
        inventory = tb.read_inventory(self.media.inventory_output)
        self.assertEqual(len(inventory['backups']), result['volumes'])
        restored = io.BytesIO()
        summary = tb.restore_stream(backup_id, self.media, restored)
        self.assertTrue(summary['data_verified'])
        self.assertEqual(restored.getvalue(), payload)
        self.assertIn('Total job ETA', self.errors.getvalue())
        self.assertIn('Tape ETA', self.errors.getvalue())

    def test_catalog_with_no_snapshot_is_readable_without_data_scan(self):
        backup_id = self.backup()
        self.assertTrue(self.media.last_result['metadata_complete'])
        with patch.object(tb.StreamReader, 'frames', side_effect=AssertionError('Unexpected scan')):
            result = tb.inspect_all(self.media, allow_scan=False)
        self.assertTrue(result['scan_complete'])
        self.assertEqual(result['backups'][0]['id'], backup_id)
        self.assertEqual(result['backups'][0]['archive_type'], 'stream')
        tb.inventory_document(result['backups'])

    def test_managed_command_exit_is_checked_before_completion(self):
        backup_id = self.command('import sys; sys.stdout.buffer.write(b"hello\\x00world")', verify=True)
        self.assertEqual(self.media.last_result['stream']['completion'], 'command-exit')
        self.assertEqual(self.media.last_result['warnings'], [])
        output = io.BytesIO()
        tb.restore_stream(backup_id, self.media, output)
        self.assertEqual(output.getvalue(), b'hello\x00world')

    def test_failed_producer_leaves_no_completion_marker(self):
        with self.assertRaisesRegex(tb.BackupError, 'status 7'):
            self.command('import sys; sys.stdout.buffer.write(b"x" * 200000); sys.exit(7)')
        self.assert_incomplete()

    def test_managed_ssh_uses_standard_remote_tools_and_checks_remote_failure(self):
        server = SSHServer()
        self.addCleanup(server.close)
        command = ['ssh', '-T', '-F', str(server.config), '-oBatchMode=yes',
                   '-oStrictHostKeyChecking=yes', 'backup-source']
        backup_id = tb.backup_stream('remote standard tools', self.media,
                                     command=[*command, 'printf "hello from SSH"'],
                                     buffer_size=128 * 1024)
        output = io.BytesIO()
        tb.restore_stream(backup_id, self.media, output)
        self.assertEqual(output.getvalue(), b'hello from SSH')
        self.media = tb.FileMedia(self.root / 'failed-ssh')
        with self.assertRaisesRegex(tb.BackupError, 'status 6'):
            tb.backup_stream('failed remote command', self.media,
                             command=[*command, 'printf partial; exit 6'],
                             buffer_size=128 * 1024)
        self.assert_incomplete()

    def test_failure_after_stdout_eof_still_prevents_completion(self):
        with self.assertRaisesRegex(tb.BackupError, 'status 9'):
            self.command('import os,time; os.write(1,b"payload"); os.close(1); time.sleep(.1); os._exit(9)')
        self.assert_incomplete()

    def test_empty_input_rejected(self):
        with self.assertRaisesRegex(tb.BackupError, 'empty'):
            self.backup(b'')
        self.assert_incomplete()

    def test_exact_size_mismatch_prevents_completion(self):
        for size in (1, 100):
            with self.subTest(size=size):
                self.media = tb.FileMedia(self.root / f'media-{size}')
                with self.assertRaisesRegex(tb.BackupError, 'expected|exceeds'):
                    self.backup(b'payload', expected_bytes=size)
                self.assert_incomplete()

    def test_estimated_size_is_advisory(self):
        backup_id = self.backup(b'payload', estimated_bytes=1)
        self.assertTrue(tb.scan(self.media, backup_id)['data_verified'])

    def test_write_failure_replays_without_duplicate_output(self):
        self.media = FaultMedia(self.root / 'faults', capacity=9 * tb.BLOCK_SIZE,
                                fail_commit=True, lost_records=1)
        payload = os.urandom(700_123)
        backup_id = self.backup(payload)
        output = io.BytesIO()
        tb.restore_stream(backup_id, self.media, output)
        self.assertEqual(output.getvalue(), payload)
        self.assertGreater(self.media.last_result['volumes'], 1)

    def test_corrupt_frame_is_rejected_before_emission(self):
        backup_id = self.backup(b'x' * 100_000)
        tape = next((self.root / 'media').glob('*.tape'))
        with tape.open('r+b') as stream:
            stream.seek(2 * tb.BLOCK_SIZE)
            stream.write(b'!')
        output = io.BytesIO()
        with self.assertRaisesRegex(tb.BackupError, 'Checksum mismatch'):
            tb.restore_stream(backup_id, self.media, output)
        self.assertEqual(output.getvalue(), b'')

    def test_missing_end_marker_fails_even_after_payload_was_emitted(self):
        payload = b'payload'
        backup_id = self.backup(payload)
        tape = next((self.root / 'media').glob('*.tape'))
        with tape.open('r+b') as stream:
            stream.truncate(3 * tb.BLOCK_SIZE)  # volume, data header, data payload
        output = io.BytesIO()
        with self.assertRaises(tb.BackupError):
            tb.restore_stream(backup_id, self.media, output)
        self.assertEqual(output.getvalue(), payload)
        self.assertIn('output may be partial', self.errors.getvalue())

    def test_regular_restore_and_listing_reject_opaque_stream(self):
        backup_id = self.backup()
        with self.assertRaisesRegex(tb.BackupError, 'stream-restore'):
            tb.restore([backup_id], self.root / 'destination', self.media, quiet=True)
        self.assertFalse((self.root / 'destination').exists())
        with self.assertRaisesRegex(tb.BackupError, 'stream-restore'):
            tb.list_files(self.media, backup_id)

    def test_idle_stdin_reader_can_be_cancelled_without_closing_input(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        source = tb.PipeSource(input_fd=read_fd)
        with tb.Progress('cancellation') as progress:
            reader = tb.ReadAhead(source.frames(tb.BLOCK_SIZE, progress), source.cancel, progress)
            with reader:
                deadline = time.monotonic() + 2
                while progress.reader_state != 'reading' and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(progress.reader_state, 'reading')
            self.assertFalse(reader.thread.is_alive())
        os.fstat(read_fd)  # caller owns stdin; cancellation must not close it

    def test_cli_pipe_preserves_stdout_and_inventory(self):
        payload = os.urandom(140_007)
        inventory = str(self.root / 'cli-inventory.json')
        result = self.cli('stream-backup', '--name', 'cli', '--stdin', '--json',
                          '--media-dir', str(self.root / 'media'), '--inventory', inventory,
                          '--buffer-size', '64KiB', input=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        restored = self.cli('stream-restore', '--backup', summary['id'],
                            '--media-dir', str(self.root / 'media'), '--inventory', inventory)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(restored.stdout, payload)

    def test_cli_command_argv_is_not_interpreted_by_a_shell(self):
        literal = 'spaces; $HOME `false`'
        result = self.cli('stream-backup', '--name', 'cli', '--json',
                          '--media-dir', str(self.root / 'media'), '--command',
                          sys.executable, '-c', 'import sys; print(sys.argv[1],end="")', literal)
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = self.cli('stream-restore', '--backup', json.loads(result.stdout)['id'],
                            '--media-dir', str(self.root / 'media'))
        self.assertEqual(restored.stdout, literal.encode())

    def test_cli_broken_output_pipe_fails_without_traceback(self):
        backup_id = self.backup(b'x' * 200_000)
        process = subprocess.Popen([*CLI, 'stream-restore', '--backup', backup_id,
                                    '--media-dir', str(self.root / 'media')],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        with process.stderr:
            errors = process.stderr.read()
        self.assertEqual(process.wait(timeout=10), 1, errors)
        self.assertIn(b'output pipe closed', errors)
        self.assertNotIn(b'Traceback', errors)

    def test_cli_interrupt_while_producer_waits_after_eof(self):
        ready = self.root / 'ready'
        code = ('import os,time,pathlib; os.write(1,b"payload"); os.close(1); '
                f'pathlib.Path({str(ready)!r}).touch(); time.sleep(30)')
        process = subprocess.Popen([*CLI, 'stream-backup', '--name', 'wait',
                                    '--media-dir', str(self.root / 'media'), '--command',
                                    sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 130, errors)
            self.assertEqual(output, b'')
            self.assert_incomplete()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


if __name__ == '__main__':
    unittest.main()
