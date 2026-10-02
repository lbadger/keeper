# Changelog

## 1.1.0 — 2026-10-02

- Use seven-character, case-sensitive backup IDs for new backups, with collision
  checks against available metadata and continued support for existing 32-character IDs.
- Add `zfs-restore --backup ID --output FILE` to export a verified native ZFS send
  stream without ZFS installed, for later recovery with `zfs receive`.
- Add `zfs-restore --backup ID --stdout` to pipe the original send stream directly
  to another command, with payload-only stdout and failure exit status.

## 1.0.1 — 2026-10-01

- Add `backup --append-after ID` to append an independent full file backup to an
  existing tape, using the same append checks as ZFS backups.
- Clarify that inspection reports checks performed during the current operation,
  rather than verification history from an earlier backup.
- Allow an explicitly confirmed wipe of short-record media at the first blank
  cartridge prompt of a fresh full backup, while retaining append and continuation
  protection.

## 1.0.0 — 2026-09-30

First release of Keeper.

- Stream full and incremental GNU tar backups across multiple tapes, with
  automatic append, cartridge changes, bounded buffering, and checksums.
- Back up existing ZFS filesystem snapshots, including raw encrypted sends,
  incremental chains, and independent full snapshots appended to a shared tape.
- Back up arbitrary byte streams from stdin or a managed producer, with exact-byte
  restores and producer exit-status checks.
- Support SSH sources, exclusions, read-back verification, filename indexes,
  cartridge labels, optional inventories, and restore-chain planning.
- Restore full chains or apply later incrementals to a tracked destination.
- Provide status, diagnostics, compression control, rewind, eject, and confirmed wipe.
- Show a live terminal dashboard, per-run logs, and an ASCII Keeper help banner.
- Ship a portable Linux executable with Python bundled, a simplified README,
  a runnable file-backed demo, and detailed reference documentation.
- Preserve compatible legacy tapes, restore markers, and shared lock names.
