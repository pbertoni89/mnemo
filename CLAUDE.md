# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in this repository.

## What this is

`mnemo` is a personal backup tool: a Python 3 CLI (`mnemo/driver.py`) that wraps `rsync` to copy a set of source directories onto an external drive in increments. It has no dependencies beyond the standard library and the system tools `rsync`, `du`, `find`, `tail` and `ncdu` (optional). The one exception is `--stt`, which needs faster-whisper from the uv venv in `.venv/` (see below). There is no build step, test suite or linter config.

The code is the `mnemo/` package, one module per mode, plus a small command line:

| File | Contents |
|---|---|
| `mnemo/driver.py` | The executable: argparse, source/whitelist resolution, and dispatch to the modes (`main()`). |
| `mnemo/backup.py` | `--backup`: `backup()`. |
| `mnemo/heavy.py` | `--heavy`: `find_heavy()`. |
| `mnemo/crowded.py` | `--crowded`: `find_crowded()`. |
| `mnemo/squeeze_dups.py` | `--squeeze-dups`: duplicate scan, group prompting, mode 1 (move to `mnemo-thrash`) and mode 2 (rsync copy). |
| `mnemo/stt.py` | `--stt`: the venv re-exec, Whisper model loading and the transcription pool. |
| `mnemo/__init__.py` | Shared plumbing. |

`__init__.py` contains:
- the repo paths (`DATA_DIR`, `LOGS_DIR`, `CACHE_DIR`, `DATE_SUFFIX`) and the tee `logger`;
- the prompt helpers (`ask`, `ask_yes_no`, `ask_number`);
- the destination helpers used by more than one mode (`check_destination_outside_sources`, `prepare_destination`);
- generic helpers (`get_env`, `parse_bool`, `format_elapsed`, `get_mountpoint`, `is_within`, `get_directory_size`, `du_bytes`, `human_size`, `md5_file`).

Mode modules import from the package with absolute imports (`from mnemo import …`). `driver.py` runs as a script, so Python puts `mnemo/` first on `sys.path`. `driver.py` replaces that entry with the repo root, which makes `import mnemo` work and stops `mnemo/*.py` from shadowing other top-level modules.

New code goes in the module of its mode. Helpers used by more than one mode go in `__init__.py`. A new mode gets a new module and a dispatch branch in `driver.py`.

`stt.py` must only import faster-whisper, ctranslate2 and numpy inside functions: `driver.py` imports every mode module at start-up under the system Python, where those packages don't exist. `ensure_stt_runtime(script)` takes `driver.py`'s path from `main()`, because it re-execs the command-line script, not `stt.py`.

`README.md` is the user-facing documentation. Keep it in sync when you change flags, prompts or behaviour.

## Running

```bash
# Typical invocation (what driver.sh's mnemo_quick does; run it from the repo root,
# because the list paths are relative to the current directory)
./mnemo/driver.py --whitelist ./whitelist.txt --blacklist ./blacklist.txt --backup

# Always try changes with a dry run first
./mnemo/driver.py --whitelist ./whitelist.txt --blacklist ./blacklist.txt --backup --dry-run

# Analysis modes
./mnemo/driver.py --whitelist ./whitelist.txt --heavy 20     # heaviest files per source
./mnemo/driver.py --whitelist ./whitelist.txt --crowded 20   # dirs with the most files per source

# Copy keeping one copy of each set of duplicates (driver.sh: mnemo_squeeze_dups DIR [DIR...])
./mnemo/driver.py --squeeze-dups --source DIR [DIR...] [--dry-run]

# Transcribe audio to sibling .txt files (driver.sh: mnemo_stt DIR [DIR...]; env built by mnemo_stt_setup [force])
./mnemo/driver.py --stt --source DIR [DIR...] [--dry-run]
```

The modes `--backup`, `--heavy`, `--crowded`, `--squeeze-dups` and `--stt` are mutually exclusive, and exactly one is required. Sources come from `--source` and/or `--whitelist`; `main()` in `driver.py` makes them absolute and checks they exist.

`driver.sh` is meant to be `source`d, not executed. `mnemo_old_stuff_ignore_yet_keep_me` is a legacy full-system `tar` backup that the user wants kept; don't delete or "clean up" it.

## Layout and local state

- `whitelist.txt`: source directories, one per line, with `#` comments. Gitignored because it's machine-specific.
- `blacklist.txt`: rsync exclude patterns, passed as `--exclude-from`. Gitignored.
- `logs/`: per-run files named with a shared `DATE_SUFFIX` (`%Y%m%d-%H%M%S`). Gitignored.
  - All modes: `mnemo_log_*`, the logger tee.
  - `--backup`: `mnemo_selected_*` (the file list) and `mnemo_backup_stdout_*` / `mnemo_backup_stderr_*` (rsync output).
  - `--squeeze-dups`: `mnemo_squeeze_dups_*.csv` (`was_dup,md5sum,path` for every entry sent to rsync, appended per source) and `mnemo_squeeze_dups_stderr_*`. Mode 1 writes `mnemo_squeeze_dups_moved_*.csv` instead (`moved_path,trash_path,kept_path,md5sum`, one row per file actually moved).
- `.cache/cache.json`: `{"git_repos": {<abs source path>: [<repo dirs>]}}`. Gitignored.
- `.venv/`: the uv environment for `--stt` (Python 3.12, faster-whisper, `av<19`, and the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels when `nvidia-smi` sees a GPU). Built by `mnemo_stt_setup`. Gitignored.

`DATA_DIR` is resolved as the parent of the package directory (the repo root), so logs and cache always land in the repo, whatever the cwd. The log file is opened when the `mnemo` package is imported, so **every invocation, even `--help`, creates a `logs/mnemo_log_*` file**. Delete the ones your own test runs create.

## Destination handling (shared)

Before that, `check_destination_outside_sources()` halts if the destination equals or lies inside any source (both modes). Paths are compared with `realpath` via `is_within()`, so symlinks and `..` can't bypass it. `squeeze_dups()` additionally halts if a source and its `DEST/<basename>` overlap in either direction, e.g. when the destination is the source's parent. These checks run before anything is created.

`prepare_destination()` creates the destination if needed (it only logs in dry-run mode) and **refuses if its mountpoint is `/`**: the destination must be an external mount. The default is `/run/media/pbertoni/storage/pb-backup/`. `--backup` mirrors absolute source paths under the destination (`DEST/home/…`). `--squeeze-dups` instead puts each source at `DEST/<basename of source>/…` (`squeeze_target()`).

## How `backup()` works

The steps run in this order, and each one matters:

1. Run `prepare_destination()`.
2. Unless `--also-repos` is given, find every git working tree under each source (`find -name .git -prune`) and add its parent dir to the excludes. Results are cached per source in `.cache/cache.json`. The cache is **never invalidated automatically**: `--no-cache` bypasses it for one run without rewriting it, so to pick up new repos you delete `cache.json`.
3. Build the file list with an rsync `--dry-run --out-format=%n` pass, so the list honours exactly the same excludes.
4. Check space with `du -scb` against the destination's free space. If it doesn't fit: `--permissive` proceeds silently; otherwise the user is prompted, and on "no" can open `ncdu` on the heaviest source.
5. If `--delete` is set (and this is not a dry run), ask for confirmation.
6. Run `rsync -aRv` with stdout and stderr sent to the log files, and `tail -f` the stdout log live. Exit codes 0, 23 and 24 count as success (23 and 24 log a warning); 11 means the disk is full; anything else is fatal.

## How `squeeze_dups()` works

`ask_squeeze_mode()` first asks for one of two modes. 1 (`SQUEEZE_MOVE`) moves the discarded copies out of the sources into the destination. 2 (`SQUEEZE_MERGE`) copies a squeezed tree to the destination. The question is asked in dry-run too. Both modes share `find_duplicate_groups()` and `choose_from_groups()`, which shows each group and returns `(picks, skipped, sticky, quit_requested)`, with picks as `[(digest, paths, kept)]`.

### Mode 1: `move_dups()`

The chosen copy stays in place; the others are moved to `trash_dir()` = `DEST/<basename>/mnemo-thrash/<path relative to src>`. Nothing is deleted outright, and nothing else is copied. Because mode 1 writes under `DEST/<basename>` like mode 2, the basename-clash check, the overlap checks, the "Proceed?" prompt (worded "Discarded copies will be moved to …/mnemo-thrash") and `prepare_destination()` all run before the branch. Free space on the destination is deliberately not checked.

For each source: ask about every group, build `[(discarded, kept, digest)]`, and call `move_discarded()`. For each file, that function:

1. re-hashes the kept copy (once per kept file) and skips if it no longer matches the scanned md5;
2. skips if `os.path.samefile(discarded, kept)` (a hardlink or bind-mount alias: moving it frees nothing, or moves the kept copy out);
3. picks a target with `free_trash_path()`, which appends `~N` and never overwrites;
4. calls `move_verified()`. That tries `os.rename`. On `EXDEV`, it does `copy2` to `<target>.mnemo-partial`, checks the md5, `os.replace`s it into place, and only then removes the original. On any failure the partial is removed and the original stays.

Moves are appended to `logs/mnemo_squeeze_dups_moved_*.csv` (`moved_path,trash_path,kept_path,md5sum`). On `q`, the current source's choices so far are still moved, and later sources are skipped. The summary shows source `du` before -> after and the trash size, and `spared space` is the source shrinkage. Dry-run logs `Would move` lines and `would spare about` (sum of file sizes). Empty directories are left in the source. The directory name is `mnemo-thrash` (`SQUEEZE_TRASH_DIR`), spelled as the user asked.

### Mode 2: copy to the destination

It copies each source to `DEST/<basename of source>`, keeping only one copy of each set of identical files. It halts before doing anything if two sources share a basename, since they would merge. Each source is handled on its own; duplicates are never matched across sources. The blacklist, `--exclude` and git-repo exclusion are deliberately **not** applied here.

0. After those checks, and before `prepare_destination()`, it asks `Ready to squeeze duplicates. Output directory will be … Proceed? [y/N]`, listing every `DEST/<basename>`. Only yes continues; no, Enter, Ctrl-C and EOF abort with exit 1. This exists because the user often forgets `--destination` and gets the default. Dry-run skips the prompt and only logs the output dirs. Scripted test runs must therefore feed the mode (`1` or `2`) and then `y` on stdin. Mode 1 shows the same prompt.
1. `find_duplicate_groups()` walks the source and buckets files by size, then md5-hashes only the files whose size matches another's. It returns `(groups, singles)`. Singles are everything else: unique files, empty files, symlinks (including symlinks to directories), and unreadable files, which are left for rsync to report.
2. For each group, the paths are listed sorted alphabetically and `choose_from_groups()` asks: `N` picks that file; `a`/`b` mean always first/always last, sticky for the rest of the run (no more prompts); `s` skips the group, so none of its files are copied; `q`, Ctrl-C or EOF quits.
3. `rsync_file_list()` copies the chosen duplicates plus all singles in a single `rsync -a --from0 --files-from=LIST --out-format=%n <parent of src>/ DEST/` pass. LIST paths are relative to the parent of the source, so `--files-from`'s implied `--relative` recreates them as `DEST/<basename>/…`, so files already up to date are skipped and an existing target is overwritten. It reads rsync's stdout and logs one line per transferred file, tagged `chosen duplicate` or `non-duplicate`, skipping directory lines. It then logs how many entries were already up to date. Dry-run adds `--dry-run` and logs `[DRY-RUN] Would copy …` lines instead. It uses the same exit-code rules as `backup()`. LIST is a NUL-separated temp file, deleted afterwards. The readable record is the CSV, written with the `csv` module (so odd names are quoted). Before the copy, `squeeze_dups()` md5-hashes every single that is a regular file (not a symlink) and was never hashed, so the CSV `md5sum` is empty only for symlinks and unreadable files. This is a full read of the source, which is the expensive part on big trees. Singles come from `find_duplicate_groups()` as `(path, md5 or None)` pairs, and chosen duplicates are tracked as `(path, md5)`.

On `q`, only the choices made so far in the current source are copied, without its singles, and the remaining sources aren't processed. At the end, for every source processed, it logs the size of the source and of `DEST/<basename>` (`n/a` if missing, e.g. in dry-run). The final elapsed-time line ends with `, spared space: X`, the sum of source minus copy over all processed sources. Sizes are measured with `du -s --block-size=1` (`du_bytes()`) and formatted by `human_size()`, which matches `du -h` output (1024-based, rounded up, one decimal below 10). That way the spared figure is exact, not a difference of rounded strings. Nothing in the sources is ever modified.

## How `stt()` works

`--stt` never touches the destination. It writes `FILE.txt` (the full name plus `.txt`) next to every `.m4a`/`.aac`/`.amr` file under each source, matching the extension case-insensitively. Files whose `.txt` exists are skipped, which makes runs resumable.

1. `main()` calls `ensure_stt_runtime(script)` with its own path right after parsing, before anything is logged, unless it's a dry run. That re-execs `driver.py` with `.venv/bin/python` and `LD_LIBRARY_PATH` set to the venv's `site-packages/nvidia/*/lib` dirs, since the loader only reads that variable at process start. `MNEMO_STT_REEXEC=1` prevents a loop. The parent's log file, still empty, is deleted first, so each run leaves a single log. A dry run doesn't need the venv: it only lists files.
2. All `MNEMO_*` settings are read through `get_env()` before the model loads, so a bad value fails fast. The defaults are documented in the README.
3. `load_whisper_model()` resolves `MNEMO_DEVICE=auto` with `ctranslate2.get_cuda_device_count()`. If the requested compute type isn't in `get_supported_compute_types(device)`, it takes the first supported one from `STT_COMPUTE_FALLBACK`. On the user's Pascal GPU, `float16` isn't supported, so this gives `int8_float32`. Missing CUDA libraries only show up at inference time, so the GPU model transcribes one second of silence as a warm-up. Any exception there rebuilds the model on CPU.
4. One `WhisperModel` with `num_workers=N` is shared by a `ThreadPoolExecutor` of N threads, which is faster-whisper's supported way of running transcriptions concurrently. Each transcript goes to a hidden `.FILE.txt.*.part` temp file in the same directory and is `os.replace`d into place. On Ctrl-C the pending futures are cancelled, the in-flight temp files removed, and the process ends with `os._exit(1)`, because the worker threads are stuck in native code.

Pins: `av<19` because PyAV 19 removed the `metadata_errors` argument that faster-whisper 1.2.1 passes to `av.open()`, and Python 3.12 because the system Python (3.14) is ahead of faster-whisper's native wheels.

## Conventions

- Python follows PEP 8 with a 120-character line limit. Check with `uvx pycodestyle --max-line-length=120 mnemo/` (and `uvx pyflakes mnemo/`); both must be clean. Wrap long log messages and help strings with implicit string concatenation, and long calls with aligned continuation lines.
- Output goes through the module `logger`, which writes to both stdout and `logs/mnemo_log_*`. Use `print` only for raw listings (such as `--verbose` file lists) and prompt feedback.
- Fatal conditions use `logger.error(...)` followed by `sys.exit(1)`.
- **Every prompt goes through `ask()` / `ask_yes_no()` / `ask_number()` in `__init__.py`; never call `input()` directly.** They re-ask on any unrecognised answer, so a typo is never taken as a choice. `ask_yes_no` accepts y/yes/n/no, with Enter meaning no. `ask_number` validates with `isdecimal()`, because `isdigit()` accepts characters like `²` that `int()` rejects. All of them return None on Ctrl-C/EOF, which callers treat as no or abort.
- External commands are passed as argument lists to `subprocess.run`. The exceptions are `find_heavy` and `find_crowded`, which use `shell=True` pipelines and quote the path with `list2cmdline`.
- This tool writes to the user's backup drive. Never run it for real (without `--dry-run`), and especially never with `--delete`, unless the user asks. For testing, use throwaway directories as both source and `--destination`, on a mount other than `/` (for example `/tmp` when it's a tmpfs).
