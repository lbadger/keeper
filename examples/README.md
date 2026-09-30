# Keeper examples

Start with the [README](../README.md) for physical tape, ZFS, SSH, and restore commands.

## File-backed round trip

From the repository root:

```bash
./build.sh
./examples/file-media-demo.sh
```

To use an already-built executable:

```bash
KEEPER_BIN=/absolute/path/to/keeper ./examples/file-media-demo.sh
```

The script uses Bash and ordinary Linux tools (`dd`, `cat`, `diff`, and `cmp`).
It creates a new temporary directory and:

1. Makes a full backup of sample files and verifies it.
2. Adds, changes, and removes files, then backs up and verifies the incremental.
3. Inspects simulated cartridges, lists archived changes, and plans the restore.
4. Restores the chain and compares every file with the current source.
5. Backs up a producer's byte stream, restores it, and compares the bytes.

It keeps its files and prints their location. No physical tape is accessed.
A nonzero exit means one of the commands or comparisons failed.

For interactive experiments, use `--media-dir DIRECTORY` with any backup or
restore command instead of `--device`. `--volume-size 1MiB` forces small simulated
cartridges; `--buffer-size 64KiB` keeps this demo's memory usage small.
