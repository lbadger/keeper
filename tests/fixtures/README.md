# Compatibility fixture

`legacy-format3-full.tape.gz` was created by the original tape-backup application
at commit `b648a910f7af45ff6dc53a7b609d9915fb525b97`, before the Keeper rename.
It used GNU tar, a 64 KiB buffer, and a 4 MiB simulated cartridge.
It contains `book.txt` with `format-3 compatibility fixture\n`, an empty directory
named `empty`, and a symbolic link `link` to `book.txt`.
It has no appended metadata footer. All contents are synthetic.
