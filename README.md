```text
 _  __
| |/ /___  ___ _ __   ___ _ __
| ' // _ \/ _ \ '_ \ / _ \ '__|
| . \  __/  __/ |_) |  __/ |
|_|\_\___|\___| .__/ \___|_|
              |_|
```

# Keeper

Back up files, ZFS snapshots, and byte streams to Linux tape drives. Keeper
handles multiple cartridges, incremental chains, checksums, and restores without
staging an archive on disk. Backup metadata travels with the tapes.

| What you want to save | Back up | Restore |
| --- | --- | --- |
| Files and directories | `backup` | `restore` |
| An existing ZFS filesystem snapshot | `zfs-backup` | `zfs-restore` |
| Bytes from a command or stdin | `stream-backup` | `stream-restore` |

## Install

Download `keeper` and `keeper.sha256` from [v1.1.0](https://github.com/lbadger/keeper/releases/tag/v1.1.0):

```bash
curl -fLO https://github.com/lbadger/keeper/releases/download/v1.1.0/keeper
curl -fLO https://github.com/lbadger/keeper/releases/download/v1.1.0/keeper.sha256
sha256sum --check keeper.sha256
chmod +x keeper
./keeper --version
```

The release binary targets **Linux x86-64 with glibc 2.31 or newer** and zlib. It bundles
Python; it is not fully statically linked. The host needs GNU tar for file
backups/restores and `mt` from `mt-st` for physical tape operations. SSH and
native ZFS workflows additionally need OpenSSH and OpenZFS on the relevant hosts.
Exporting a ZFS stream with `zfs-restore --output FILE` or `--stdout` does not need OpenZFS.
On Debian/Ubuntu, the basic tools are installed with `sudo apt install tar mt-st`.

Use an account with permission to operate the tape drive and read the source.
Restoring ownership and privileged file metadata may require root. The default
device is **`/dev/nst0`**; add `--device /dev/nst1` to select another drive.

```bash
./keeper                  # Main menu
./keeper backup --help    # Options for one command
./keeper status           # Drive state without moving the tape
```

To build from source, run `./build.sh` with Docker, or `./build.sh --local` with
Python 3.11+, pip, venv, and binutils. The result is `dist/keeper`. You can also
run `python3 keeper.py`. See [build and test instructions](docs/reference.md#file-backed-testing-and-building).

## Back up and restore files

Start with a blank cartridge. Keeper asks for more blank cartridges when needed
and leaves the final tape loaded. Save the full backup ID printed on success and
label each cartridge with the label and volume information shown in the prompts.

```bash
# Full backup, followed by read-back verification.
full_id=$(./keeper backup --source /srv/data --verify \
  --label-prefix DATA --inventory tapes.json)

# After files change, append an incremental to the full's final cartridge.
delta_id=$(./keeper backup --source /srv/data --level incremental \
  --base "$full_id" --verify --inventory tapes.json)

# Restore both backups into an empty or absent directory.
./keeper restore --backup "$full_id" "$delta_id" --destination /srv/recovered
```

For the next incremental, use the newest successful ID as `--base`. The source,
SSH identity, and exclusions must match, and the base must be the last completed
backup on its final cartridge. Restore requires the full backup and every
incremental through the chosen recovery point, in order.

To add an independent full backup to the same tape, use the last completed ID:

```bash
./keeper backup --source /srv/other-data --append-after LAST_BACKUP_ID --verify
```

Load that backup's final cartridge. The new full can use a different source or
exclusions and restores with its own ID alone. `--append-after` cannot be combined
with `--base` or `--level incremental`.

Use a filesystem snapshot or pause applications for a consistent source.
Keeper includes mounted directories below the source and does not follow symlinks.

Exclude paths relative to the source root; quote wildcard patterns:

```bash
./keeper backup --source /srv/data --exclude cache --exclude '*.tmp'
```

Incrementals inherit exclusions. Changing the policy requires a new full backup.
`--exclude-from FILE` reads one pattern per line. More on [exclusions](docs/reference.md#exclude-files-and-folders)
and [stepwise restores](docs/reference.md#restore-directly-from-tape).

## Find backups, list files, and verify

```bash
./keeper inspect                                  # Backup IDs on the loaded tape
./keeper inspect --backup BACKUP_ID                # One backup's header
./keeper list --backup BACKUP_ID --index-only       # Tar filenames; load the final cartridge
./keeper verify --backup BACKUP_ID                 # Read and check that entire backup
```

**`verify` checks one backup per invocation**, across all of its cartridges.
Without `--backup`, it selects the first backup starting on the loaded tape.
It does not automatically verify other backups or an incremental's parents;
run it separately for every ID you want checked.

`inspect` and indexed `list` read metadata, not file contents. If an index is
unavailable, ordinary `list` scans the selected backup; `--index-only` prevents
that fallback. Use `list --scan` to read and verify the archive while listing.
For tape inspection without a usable catalog, `inspect --scan` permits a full
cartridge scan. Tape scans can take a long time.

Inspection reports `Data checksums: Not checked during this operation`, even
after a successful backup with `--verify`. This describes the inspection itself;
verification history is not stored on tape. Use `--verify --json` when backing up
to capture the successful read-back result as `data_verified: true`.

If read-back verification fails after a backup was committed, retain its tapes
and retry `verify --backup ID`. The committed backup has not been erased.

New backup IDs are **seven case-sensitive letters and digits**, for example
`7aQm3Kx`. Copy them exactly for `--backup`, `--base`, `--append-after`, or `--to`.
Existing 32-character IDs remain supported. Keep a shared `--inventory` to check
new IDs against backups on offline cartridges as well.

## Back up ZFS snapshots

These commands use **existing snapshots of one filesystem dataset**. Keeper does
not create snapshots, recursively replicate child datasets, or back up zvols.

```bash
# Full snapshot on blank media.
./keeper zfs-backup --snapshot tank/photos@full --verify

# Incremental of the same dataset; keep the parent's source snapshot.
./keeper zfs-backup --snapshot tank/photos@next --base PHOTOS_FULL_ID

# A different dataset as an independent full backup on the same tape.
./keeper zfs-backup --snapshot tank/documents@full --append-after LAST_BACKUP_ID

# Restore a chain into a new dataset whose parent pool/dataset already exists.
./keeper zfs-restore --backup PHOTOS_FULL_ID PHOTOS_DELTA_ID --dataset tank/recovered

# Get a snapshot off tape on a machine without ZFS.
./keeper zfs-restore --backup PHOTOS_FULL_ID --output /srv/recovery/photos-full.zfs
# Later, on a ZFS machine:
zfs receive -u tank/recovered < /srv/recovery/photos-full.zfs
```

`--base` requires the same dataset, source host, and send mode. `--append-after`
adds an independent full backup after the tape's last completed backup; it has no
restore dependency on that backup. The two options cannot be combined.

Encrypted datasets require `--raw`. Incrementals inherit their parent's raw mode;
an independent full needs its own `--raw` option. Keep the encryption keys separately.

`--dataset` verifies before receiving, requiring two tape passes. The destination
stays read-only and unmounted. See [ZFS restore and mounting](docs/reference.md#native-zfs-streaming).

`--output FILE` exports the original send stream in one tape pass, with checksums
and completion verified before the final file appears. It requires a new filename
and no ZFS installation. Export each full or incremental backup separately, then
receive the files in order. These files contain native ZFS streams, not extracted
files; raw encrypted streams remain encrypted.

For a direct pipe, use `--stdout` and check the whole pipeline's exit status:

```bash
set -o pipefail
./keeper zfs-restore --backup PHOTOS_FULL_ID --stdout | zfs receive -u tank/recovered
```

Stdout contains only the original send stream. A later verification failure can
leave partial output or an incomplete receive; check success before using the result.

## Back up over SSH

Run Keeper on the tape host. Install the same version on the source host and
configure SSH keys and a trusted host key first. The source path is remote and
must be absolute.

```bash
./keeper backup --ssh backup@fileserver --source /srv/data \
  --remote-program /home/backup/keeper --verify
```

Omit `--remote-program` when `keeper` is in the remote account's PATH.
`zfs-backup` accepts the same SSH options. Restore uses the tapes locally and does
not need the original source host. See [SSH setup and options](docs/reference.md#back-up-a-remote-source-over-ssh).

## Back up a command's output

Use this for a database dump, tar stream, or other producer. A managed command
must exit successfully before Keeper marks its backup complete. Put `--command`
last; everything after it belongs to the producer.

```bash
./keeper stream-backup --name remote-files --verify \
  --command ssh -T -oBatchMode=yes -oStrictHostKeyChecking=yes backup@fileserver \
  'tar -C /srv/data -cf - .'

# Recover the original bytes; publish the output file only after success.
./keeper stream-restore --backup STREAM_ID > recovered.tar.partial && \
  mv recovered.tar.partial recovered.tar
```

Only the tape host needs Keeper for this example. Each stream backup starts on
blank media. Its internal format and incremental dependencies are your
responsibility. Stdin is also supported, but EOF cannot prove producer success;
use shell `pipefail` and check the pipeline status. See [pipe workflows](docs/reference.md#pipe-streaming-without-a-remote-binary).

## Manage tapes

| Command | Action |
| --- | --- |
| `./keeper status` | Show readiness, position, compression, and drive statistics |
| `./keeper doctor` | Show the same diagnostic information |
| `./keeper rewind` | Return to the beginning and leave the tape loaded |
| `./keeper eject` | Unlock, rewind, and unload the tape |
| `./keeper compression status` | Show hardware compression; use `on` or `off` to change it |
| `./keeper wipe` | **Erase the loaded tape**, after a `WIPE` confirmation |

Full backups normally require blank media. Both `backup` and `zfs-backup` accept
`--append-after LAST_BACKUP_ID` to add an independent full to an existing tape.
Continuation cartridges must be blank. Keeper never erases automatically.

At a tape-change prompt, press Enter after loading the requested cartridge,
use `eject` to unload, or `q` to stop. Blank-cartridge prompts also offer `wipe`
with a separate confirmation. Drive commands share a lock, including while a
backup is waiting for media. For loaders, see [automated tape loading](docs/reference.md#automated-tape-loading).

An interrupted backup can leave an incomplete tail that blocks appending.
There is no resume after process exit. Preserve completed backups and start a
new full on separate media when needed. An interrupted in-place restore must
be rebuilt into a separate destination. See [failure recovery](docs/reference.md#bounded-buffering-and-failure-recovery).

## Useful options

| Option | When to use it |
| --- | --- |
| `--dry-run` | Validate and estimate a tar/ZFS backup without writing tape |
| `--verify` | Read back after a backup; standalone `verify` checks an existing backup |
| `--inventory tapes.json` | Keep optional cartridge observations and restore-chain information |
| `--label-prefix NAME` | Assign readable labels to new cartridges |
| `--buffer-size 256MiB` | Set each read-ahead/recovery budget; default is 1 GiB each |
| `--cartridge-capacity 2300GiB` | Improve capacity/ETA estimates without limiting writes |
| `--log-file keeper.log` | Choose the diagnostic log destination |

Options vary by command; use `COMMAND --help` for the exact list. Allow more
than 2 GiB of RAM with the default buffers, plus snapshot/catalog memory.
A dry run does not validate the writable tape position or available capacity.

With an inventory, plan a restore before loading tapes:

```bash
./keeper restore --to LATEST_BACKUP_ID --plan --inventory tapes.json
./keeper restore --to LATEST_BACKUP_ID --inventory tapes.json --destination /srv/recovered
```

The inventory is optional; tape contents remain authoritative. A complete plan
is not data verification. More on [inventories](docs/reference.md#cartridge-labels-and-optional-inventory)
and [restore planning](docs/reference.md#plan-and-discover-a-restore-chain).

## Try it without a tape drive

From a source checkout, build Keeper and run the example:

```bash
./build.sh
./examples/file-media-demo.sh
```

The demo creates sample files in a new temporary directory, writes a full and
incremental backup to simulated cartridges, verifies both, and compares the
restored files. It also checks a byte-stream round trip and leaves the results
for inspection. It never accesses a physical tape drive.

For your own trials, replace `--device` with `--media-dir ./demo-tapes`.
The two options are mutually exclusive. See [examples](examples/README.md).

## Logs, compatibility, and testing

Keeper prints a log path at startup. Logs normally live in
`~/.local/state/keeper/logs/` (or `$XDG_STATE_HOME/keeper/logs/`). Progress and
prompts use the terminal; backup IDs, JSON, filenames, and restored streams use
stdout. `--quiet` suppresses per-file log entries. `NO_COLOR=1` disables color;
`TERM=dumb` also disables the live dashboard.

Keeper reads tape formats 3 (tar), 4 (ZFS), and 5 (byte streams), including
compatible tapes written by the earlier tape-backup application. Shared lock
names and restore markers retain their legacy names for interoperability.
A `tape-backup` executable alias is included in source builds. These tapes use
Keeper framing: use Keeper to restore them, not `tar -xf /dev/nst0`.

The test suite covers real GNU tar, SSH, file-backed media, a tape simulator,
and the standalone binary. Native ZFS tests require a dedicated scratch pool.
Test restore and cartridge rollover on your drive with scratch media before
relying on it. Checksums detect corruption; they do not authenticate or encrypt
ordinary file backups.

See the [full reference](docs/reference.md), [streaming design](docs/continuous-streaming.md),
[append design](docs/append-incrementals.md), and [release notes](docs/releases/v1.1.0.md).
