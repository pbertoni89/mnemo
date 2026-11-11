# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in this repository.

## What this is

`mnemo` is a personal, single-file backup tool: a Python 3 CLI (`mnemo/mnemo.py`) that wraps `rsync` to copy a set of source directories onto an external drive in increments. It has no dependencies beyond the standard library and the system tools `rsync`, `du`, `find`, `tail` and `ncdu` (optional). There is no build step, test suite or linter config.

`README.md` is the user-facing documentation. Keep it in sync when you change flags, prompts or behaviour.

## Running

```bash
# Typical invocation (what driver.sh's mnemo_quick does; run it from the repo root,
# because the list paths are relative to the current directory)
./mnemo/mnemo.py --whitelist ./whitelist.txt --blacklist ./blacklist.txt --backup

# Always try changes with a dry run first
./mnemo/mnemo.py --whitelist ./whitelist.txt --blacklist ./blacklist.txt --backup --dry-run

# Analysis modes
./mnemo/mnemo.py --whitelist ./whitelist.txt --heavy 20     # heaviest files per source
./mnemo/mnemo.py --whitelist ./whitelist.txt --crowded 20   # dirs with the most files per source

# Copy keeping one copy of each set of duplicates (driver.sh: mnemo_squeeze_dups DIR [DIR...])
./mnemo/mnemo.py --squeeze-dups --source DIR [DIR...] [--dry-run]
```

The modes `--backup`, `--heavy`, `--crowded` and `--squeeze-dups` are mutually exclusive, and exactly one is required. Sources come from `--source` and/or `--whitelist`; `main()` makes them absolute and checks they exist.

`driver.sh` is meant to be `source`d, not executed. `mnemo_old_stuff_ignore_yet_keep_me` is a legacy full-system `tar` backup that the user wants kept; don't delete or "clean up" it.

## Layout and local state

- `whitelist.txt`: source directories, one per line, with `#` comments. Gitignored because it's machine-specific.
- `blacklist.txt`: rsync exclude patterns, passed as `--exclude-from`. Gitignored.
- `logs/`: per-run files named with a shared `DATE_SUFFIX` (`%Y%m%d-%H%M%S`). Gitignored.
  - All modes: `mnemo_log_*`, the logger tee.
  - `--backup`: `mnemo_selected_*` (the file list) and `mnemo_backup_stdout_*` / `mnemo_backup_stderr_*` (rsync output).
  - `--squeeze-dups`: `mnemo_squeeze_dups_*.csv` (`was_dup,md5sum,path` for every entry sent to rsync, appended per source) and `mnemo_squeeze_dups_stderr_*`.
- `.cache/cache.json`: `{"git_repos": {<abs source path>: [<repo dirs>]}}`. Gitignored.

`DATA_DIR` is resolved as the parent of the script's directory (the repo root), so logs and cache always land in the repo, whatever the cwd. The log file is opened when the module loads, so **every invocation, even `--help`, creates a `logs/mnemo_log_*` file**. Delete the ones your own test runs create.

## Destination handling (shared)

`prepare_destination()` creates the destination if needed (it only logs in dry-run mode) and **refuses if its mountpoint is `/`**: the destination must be an external mount. The default is `/run/media/pbertoni/storage/pb-backup/`. Both `--backup` and `--squeeze-dups` mirror absolute source paths under the destination (`DEST/home/…`), so their outputs land in the same tree.

## How `backup()` works

The steps run in this order, and each one matters:

1. Run `prepare_destination()`.
2. Unless `--also-repos` is given, find every git working tree under each source (`find -name .git -prune`) and add its parent dir to the excludes. Results are cached per source in `.cache/cache.json`. The cache is **never invalidated automatically**: `--no-cache` bypasses it for one run without rewriting it, so to pick up new repos you delete `cache.json`.
3. Build the file list with an rsync `--dry-run --out-format=%n` pass, so the list honours exactly the same excludes.
4. Check space with `du -scb` against the destination's free space. If it doesn't fit: `--permissive` proceeds silently; otherwise the user is prompted, and on "no" can open `ncdu` on the heaviest source.
5. If `--delete` is set (and this is not a dry run), ask for confirmation.
6. Run `rsync -aRv` with stdout and stderr sent to the log files, and `tail -f` the stdout log live. Exit codes 0, 23 and 24 count as success (23 and 24 log a warning); 11 means the disk is full; anything else is fatal.

## How `squeeze_dups()` works

It copies each source to the destination, keeping only one copy of each set of identical files. Each source is handled on its own; duplicates are never matched across sources. The blacklist, `--exclude` and git-repo exclusion are deliberately **not** applied here.

1. `find_duplicate_groups()` walks the source and buckets files by size, then md5-hashes only the files whose size matches another's. It returns `(groups, singles)`. Singles are everything else: unique files, empty files, symlinks (including symlinks to directories), and unreadable files, which are left for rsync to report.
2. For each group, the paths are listed sorted alphabetically and `prompt_dup_choice()` asks: `N` picks that file; `a`/`b` mean always first/always last, sticky for the rest of the run (no more prompts); `s` skips the group, so none of its files are copied; `q`, Ctrl-C or EOF quits.
3. `rsync_file_list()` copies the chosen duplicates plus all singles in a single `rsync -a --from0 --files-from=LIST --out-format=%n / DEST/` pass, so files already up to date are skipped and an existing target is overwritten. It reads rsync's stdout and logs one line per transferred file, tagged `chosen duplicate` or `non-duplicate`, skipping directory lines. It then logs how many entries were already up to date. Dry-run adds `--dry-run` and logs `[DRY-RUN] Would copy …` lines instead. It uses the same exit-code rules as `backup()`. LIST is a NUL-separated temp file, deleted afterwards. The readable record is the CSV, written with the `csv` module (so odd names are quoted). Before the copy, `squeeze_dups()` md5-hashes every single that is a regular file (not a symlink) and was never hashed, so the CSV `md5sum` is empty only for symlinks and unreadable files. This is a full read of the source, which is the expensive part on big trees. Singles come from `find_duplicate_groups()` as `(path, md5 or None)` pairs, and chosen duplicates are tracked as `(path, md5)`.

On `q`, only the choices made so far in the current source are copied, without its singles, and the remaining sources aren't processed. Nothing in the sources is ever modified.

## Conventions

- Output goes through the module `logger`, which writes to both stdout and `logs/mnemo_log_*`. Use `print` only for raw listings (such as `--verbose` file lists) and prompt feedback.
- Fatal conditions use `logger.error(...)` followed by `sys.exit(1)`. Interactive prompts catch `KeyboardInterrupt` and exit cleanly.
- External commands are passed as argument lists to `subprocess.run`. The exceptions are `find_heavy` and `find_crowded`, which use `shell=True` pipelines and quote the path with `list2cmdline`.
- This tool writes to the user's backup drive. Never run it for real (without `--dry-run`), and especially never with `--delete`, unless the user asks. For testing, use throwaway directories as both source and `--destination`, on a mount other than `/` (for example `/tmp` when it's a tmpfs).
