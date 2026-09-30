#!/usr/bin/env bash
# End-to-end Keeper example using files as cartridges; no tape drive is opened.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
keeper_bin="${KEEPER_BIN:-$repo_dir/dist/keeper}"
if [[ ! -x "$keeper_bin" ]]; then
  printf 'Build Keeper with ./build.sh first, or set KEEPER_BIN to its executable.\n' >&2
  exit 1
fi

demo_dir="$(mktemp -d "${TMPDIR:-/tmp}/keeper-demo.XXXXXXXX")"
printf 'Demo files: %s\n' "$demo_dir"
mkdir "$demo_dir/source"
printf 'first version\n' > "$demo_dir/source/notes.txt"
printf 'remove in the incremental\n' > "$demo_dir/source/removed.txt"
dd if=/dev/urandom of="$demo_dir/source/photo.bin" bs=1024 count=300 status=none
common=(--media-dir "$demo_dir/tapes" --buffer-size 64KiB --volume-size 1MiB
        --inventory "$demo_dir/inventory.json" --log-file "$demo_dir/keeper.log")

full_id="$("$keeper_bin" backup --source "$demo_dir/source" --label-prefix DEMO --verify "${common[@]}")"
printf 'second version\n' > "$demo_dir/source/notes.txt"
printf 'new file\n' > "$demo_dir/source/added.txt"
rm -- "$demo_dir/source/removed.txt"
delta_id="$("$keeper_bin" backup --source "$demo_dir/source" --level incremental \
  --base "$full_id" --verify "${common[@]}")"

"$keeper_bin" inspect --media-dir "$demo_dir/tapes" --text
"$keeper_bin" list --backup "$delta_id" --media-dir "$demo_dir/tapes" --scan
"$keeper_bin" restore --to "$delta_id" --plan --inventory "$demo_dir/inventory.json" --text
"$keeper_bin" restore --backup "$full_id" "$delta_id" \
  --media-dir "$demo_dir/tapes" --destination "$demo_dir/restored" \
  --log-file "$demo_dir/keeper.log"
diff -r "$demo_dir/source" "$demo_dir/restored"

stream_id="$("$keeper_bin" stream-backup --name demo-bytes --verify \
  --media-dir "$demo_dir/stream-tapes" --buffer-size 64KiB \
  --log-file "$demo_dir/keeper.log" --command cat "$demo_dir/source/photo.bin")"
"$keeper_bin" stream-restore --backup "$stream_id" --media-dir "$demo_dir/stream-tapes" \
  --log-file "$demo_dir/keeper.log" > "$demo_dir/restored-photo.bin"
cmp "$demo_dir/source/photo.bin" "$demo_dir/restored-photo.bin"

printf '\nFull backup: %s\nIncremental: %s\nStream: %s\n' "$full_id" "$delta_id" "$stream_id"
printf 'All comparisons passed. Demo files remain in %s\n' "$demo_dir"
