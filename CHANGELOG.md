# Changelog

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
