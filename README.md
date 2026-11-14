# mnemo

A small backup helper for Linux. It copies the directories you care about onto an external drive with `rsync`, and it gives you a few tools for deciding what is worth copying.

- **Incremental**: only new or changed files are copied, so the second run is much faster than the first.
- **Safe by default**: it refuses to write anywhere that isn't a separate mount or that lies inside a folder being backed up, checks free space before starting, asks before deleting anything, and has `--dry-run` for every mode.
- **Transcripts of voice notes**: `--stt` writes a text transcript next to each audio recording, so they can be searched (see below).
- **Readable layout**: `--backup` puts files at `DESTINATION/<their absolute path>`. For example, `/home/me/Documents/cv.pdf` ends up at `DESTINATION/home/me/Documents/cv.pdf`, so restoring a file is a plain copy. `--squeeze-dups` instead puts each source folder directly under the destination, by its own name (see below).

## Requirements

- Python 3 (standard library only)
- For `--stt` only: [uv](https://docs.astral.sh/uv/), which builds a separate environment with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (see below). An NVIDIA GPU is used if present.
- `rsync`, plus `du`, `find` and `tail` (coreutils/findutils)
- `ncdu` (optional), used to investigate when the destination is too small

## Setup

```bash
git clone <this repo> ~/opt/mnemo
cd ~/opt/mnemo
```

Create two plain-text files in the repo root. Both are gitignored, because they're specific to your machine.

**`whitelist.txt`** lists what to back up, one directory per line. Lines starting with `#` are comments.

```text
/home/me/Documents
/home/me/Pictures
/home/me/opt
```

**`blacklist.txt`** lists what to skip, using `rsync` exclude patterns, one per line.

```text
# DIRECTORIES
node_modules
.venv
cmake-build-debug
Trash-1000

# FILES
*.iso
*.tmp
```

Optionally, load the shell shortcuts:

```bash
source ~/opt/mnemo/driver.sh   # add this to ~/.bashrc to always have them
```

## Usage

```text
driver.py (--backup | --heavy [N] | --crowded [N] | --squeeze-dups | --stt) [options]
```

Pick exactly one mode.

| Mode | What it does |
|---|---|
| `--backup` | Copies the sources to the destination. |
| `--heavy [N]` | Lists the N largest files in each source (default 10). |
| `--crowded [N]` | Lists the N directories with the most files in each source (default 10). |
| `--squeeze-dups` | Keeps only one copy of each set of identical files, chosen by you. It either moves the other copies out of the sources into the destination, or copies a squeezed version of each source to the destination. |
| `--stt` | Transcribes every `.m4a`, `.aac` and `.amr` file in the sources to a `.txt` file next to it. Ignores `--destination`. |

| Option | Meaning |
|---|---|
| `--source DIR [DIR ...]` | Directories to work on. |
| `--whitelist FILE` | Reads the directories to work on from a file (it can be combined with `--source`). |
| `--destination DIR` | Where to copy to. Must be on a separate mount, such as a USB drive. Default: `/run/media/pbertoni/storage/pb-backup/`. |
| `--blacklist FILE` | `rsync` exclude patterns (`--backup` only). |
| `--exclude PAT [PAT ...]` | Extra exclude patterns on the command line (`--backup` only). |
| `--dry-run` | Shows what would happen without copying anything. |
| `--delete` | Also deletes files from the destination that no longer exist in the sources. You are asked to confirm. |
| `--permissive` | Skips the free-space check and doesn't ask. |
| `--also-repos` | Also backs up git repositories, which are skipped by default. |
| `--no-cache` | Rescans for git repositories instead of using the cache. |
| `-v`, `--verbose` | Prints the list of selected files instead of saving it to `logs/`. |

Whenever mnemo asks a question, an answer it doesn't recognise is rejected and the question is asked again, so a typo is never taken as a choice. In `[y/N]` questions, pressing Enter means no. Ctrl-C or Ctrl-D at a question aborts the run.

## Examples

### Back up everything in your whitelist

```bash
cd ~/opt/mnemo
./mnemo/driver.py --whitelist whitelist.txt --blacklist blacklist.txt --backup
```

With `driver.sh` loaded, run `mnemo_quick` from the repo root to do the same thing.

Before copying, mnemo:

1. checks that the destination is on an external mount,
2. skips any git repositories it finds (they usually live on a remote anyway),
3. writes the list of files it will copy to `logs/`,
4. compares the space needed with the space free on the drive.

While it copies, rsync's output scrolls past. When it finishes, mnemo reminds you to unmount the drive.

### See what would happen first

```bash
./mnemo/driver.py --whitelist whitelist.txt --blacklist blacklist.txt --backup --dry-run
```

### Back up a single folder to another drive

```bash
./mnemo/driver.py --backup --source ~/Pictures --destination /run/media/me/BIGDISK/backup
```

### Make the backup an exact mirror

This also removes files you have deleted locally:

```bash
./mnemo/driver.py --whitelist whitelist.txt --blacklist blacklist.txt --backup --delete
# DELETE MODE ENABLED. Files at the destination not present in the sources will be permanently removed.
# Are you sure you want to proceed with deletion? [y/N]:
```

### Find out why the backup is too big

```bash
./mnemo/driver.py --whitelist whitelist.txt --heavy 20     # the 20 largest files
./mnemo/driver.py --whitelist whitelist.txt --crowded 20   # the 20 most crowded folders
```

Add the culprits to `blacklist.txt` and run the backup again. If the destination is too small, `--backup` also offers to open `ncdu` on your largest source.

### Back up a messy folder without its duplicates

`--squeeze-dups` suits folders like `Downloads` or an old photo dump, where the same file sits in several places. For each set of byte-identical files it shows you every copy and asks which one to keep. First it asks what to do with the copies you don't keep:

```text
How do you want to squeeze duplicates?
  1) Just remove the discarded ones (moved out of the sources into DEST/<source name>/mnemo-thrash, nothing else is copied)
  2) Squeeze and merge inside destination (the sources are left untouched)
Choice [1-2]:
```

The questions about each group of duplicates are the same in both modes:

```text
--- Duplicate group 1/2: md5 b1946ac92492d2347c6235b4d2611184, 6 bytes each ---
  1) /home/me/Downloads/baz/gem.txt
  2) /home/me/Downloads/foo/bar.txt
Save which one? [1-2], a=always first, b=always last, s=skip, q=quit: 1
```

| Answer | Effect |
|---|---|
| `1`, `2`, … | Keep that copy. |
| `a` | Keep the first copy, for this group and every group after it, without asking again. |
| `b` | Keep the last copy, for this group and every group after it, without asking again. |
| `s` | Skip this group: leave all its copies in the source (mode 1) or copy none of them (mode 2). |
| `q` | Quit. The choices made so far in the current folder are still carried out; nothing else is done. |

#### Mode 1: move the discarded copies out of the sources

This keeps the chosen copy where it is and moves the others out of your source folders. They go into the destination, under a `mnemo-thrash` folder that mirrors where they came from. Nothing is deleted, and nothing else is copied.

```bash
mnemo_squeeze_dups ~/Downloads --destination /run/media/me/BIGDISK/squeezed    # then answer 1
```

Pressing `1` in the example above keeps `~/Downloads/baz/gem.txt` and moves `~/Downloads/foo/bar.txt` to `/run/media/me/BIGDISK/squeezed/Downloads/mnemo-thrash/foo/bar.txt`. As in mode 2, mnemo first confirms where things will go:

```text
Ready to squeeze duplicates. Discarded copies will be moved to /run/media/me/BIGDISK/squeezed/Downloads/mnemo-thrash. Proceed? [y/N]:
```

Things to know:

- To undo, move the files back; `logs/mnemo_squeeze_dups_moved_<timestamp>.csv` lists every move as `moved_path,trash_path,kept_path,md5sum`.
- When the destination is on another drive, each file is copied, the copy's md5 is checked, and only then is the original deleted. If anything goes wrong, the original stays where it is.
- Just before moving, mnemo checks the kept copy still matches. If it changed after the scan or is gone, the discarded copy stays in place with a warning.
- A copy that is really the same file as the kept one (a hardlink, or the same file seen through a bind mount) is left alone: moving it would free nothing, or would move the kept copy itself.
- Nothing in `mnemo-thrash` is ever overwritten. If a file of the same name is already there from an earlier run, the new one gets `~1`, `~2`, … appended.
- Folders left empty in the source are not removed.
- At the end mnemo shows each source folder's size before and after, the size of its `mnemo-thrash`, and the total space spared in the sources.
- With `--dry-run`, it lists what it would move and an estimate of the space spared, and moves nothing.
- `mnemo-thrash` sits inside `DESTINATION/<source name>`, the same folder mode 2 writes to. If you use both modes with the same destination, the trash ends up inside the squeezed copy.

#### Mode 2: squeeze and merge inside the destination

This copies everything to the destination, except the copies you didn't choose. Your source folders are left untouched.

```bash
./mnemo/driver.py --squeeze-dups --source ~/Downloads --destination /run/media/me/BIGDISK/squeezed
# or, with driver.sh loaded:
mnemo_squeeze_dups ~/Downloads --destination /run/media/me/BIGDISK/squeezed
```

Each source is copied into a folder with the same name directly under the destination, so `~/Downloads` becomes `/run/media/me/BIGDISK/squeezed/Downloads`. Relative paths work too, which is handy on the backup drive itself:

```bash
cd /run/media/me/BIGDISK/dumps-audio
mnemo_squeeze_dups . --destination ../dumps-squeezed-audio
# -> /run/media/me/BIGDISK/dumps-squeezed-audio/dumps-audio
```

Because of this, two sources can't share a name (for example `~/a/photos` and `~/b/photos`). mnemo refuses to start rather than mix them. It also refuses a destination that would land on the source itself, such as `--source /x/foo --destination /x`.

Before doing anything, mnemo shows where the output will go and waits for confirmation. This is handy when you forget `--destination` and would otherwise get the default:

```text
Ready to squeeze duplicates. Output directory will be /run/media/pbertoni/storage/pb-backup/Downloads. Proceed? [y/N]:
```

`n` or just pressing Enter stops without creating or copying anything. With `--dry-run` there's no question; the output folder is only printed.

In the example above, pressing `1` saves `DESTINATION/Downloads/baz/gem.txt`. `foo/bar.txt` is not copied.

Things to know:

- Your answers are collected first. The copying happens in one go afterwards, so you can answer and walk away.
- Files with no duplicates are copied without asking, along with empty files and symlinks.
- Each file actually copied gets its own line on screen and in the log, for example `Copied (non-duplicate): /home/me/Downloads/x/u` or `Copied (chosen duplicate): /home/me/Downloads/baz/gem.txt`. Files already up to date at the destination aren't copied again; only their number is reported.
- Duplicates are matched only within the same `--source` folder, not across folders.
- The blacklist does **not** apply to this mode.
- Your source folders are never modified. To free space in the sources themselves, use mode 1.
- At the end, mnemo shows how much space each source and its squeezed copy take, and the total space spared across all sources:

  ```text
  --- Space used (du -sh) ---
       41G  /home/me/Downloads
       29G  /run/media/me/BIGDISK/squeezed/Downloads
  squeeze_dups elapsed time: 00h 12m 40s, spared space: 12G
  ```

  On a dry run nothing is copied, so the copy's size and the spared space show `n/a`.

Each run also records every file it sent to the destination in `logs/mnemo_squeeze_dups_<timestamp>.csv`:

```csv
was_dup,md5sum,path
true,b1946ac92492d2347c6235b4d2611184,/home/me/Downloads/baz/gem.txt
false,58695d384396a0e0c2ed9cbd9dbb5076,/home/me/Downloads/x/u
false,d41d8cd98f00b204e9800998ecf8427e,/home/me/Downloads/e1
false,,/home/me/Downloads/x/linkdir
```

- `was_dup` is `true` for the copy you chose from a duplicate group and `false` for everything else.
- `md5sum` is filled for every regular file, so it matches what `md5sum FILE` prints. It's empty only for symlinks and unreadable files. Hashing reads every file once, so on a large source expect a "Hashing … for the CSV record" step that takes about as long as reading the whole source.
- Copies you didn't choose, and groups you skipped, aren't listed.

A quick way to see only the duplicates you kept: `grep '^true,' logs/mnemo_squeeze_dups_*.csv`.

To preview the choices and the list of files that would be copied, without copying anything:

```bash
mnemo_squeeze_dups ~/Downloads --destination /run/media/me/BIGDISK/squeezed --dry-run
```

A dry run still asks which mode you want, since that decides what gets previewed.

### Transcribe voice recordings

`--stt` (speech-to-text) finds every `.m4a`, `.aac` and `.amr` file under each source, in any letter case, and writes its transcript next to it, adding `.txt` to the name: `memo.m4a` gets `memo.m4a.txt`. Nothing is copied and `--destination` is ignored.

It needs faster-whisper, which is the only part of mnemo that isn't in the standard library, so it lives in its own environment in `.venv/` in the repo. Create it once, with `driver.sh` loaded:

```bash
mnemo_stt_setup         # creates .venv/ (Python 3.12, faster-whisper, CUDA libraries if there's an NVIDIA GPU)
mnemo_stt_setup force   # deletes .venv/ and builds it again
```

Then:

```bash
mnemo_stt ~/Recordings ~/Phone/Voice --dry-run   # list the files that would be transcribed
mnemo_stt ~/Recordings ~/Phone/Voice
# or: ./mnemo/driver.py --stt --source ~/Recordings ~/Phone/Voice
```

You can run `mnemo/driver.py` with the system Python: for `--stt`, it restarts itself inside `.venv/`. The first run downloads the Whisper model into `~/.cache/huggingface`.

```text
Loading Whisper model: model=small, device=cuda, compute_type=int8_float32, num_workers=2, cpu_threads=4
Transcribing 3 files: language=auto, beam_size=5, vad_filter=True
[1/3] /home/me/Recordings/sub/ciao.aac.txt (it 91%, 3s audio in 2s)
[2/3] /home/me/Recordings/memo.m4a.txt (en 96%, 4s audio in 3s)
```

Things to know:

- Several files are transcribed in parallel (see `MNEMO_NUM_WORKERS`). Lines appear in the order files finish.
- Files that already have a `.txt` are skipped, so if you stop a run with Ctrl-C, running it again picks up where it stopped. To transcribe a file again, delete its `.txt`.
- A `.txt` is never left half-written: each transcript is written to a hidden temporary file and renamed when complete.
- If the GPU can't be used (no CUDA libraries, unsupported compute type, out of memory at load), mnemo says so and continues on the CPU.
- A file that fails to transcribe is logged and the others go on. The run then exits with status 1.

Settings come from environment variables, for example `MNEMO_MODEL=medium MNEMO_LANGUAGE=it mnemo_stt ~/Recordings`:

| Variable | Default | Meaning |
|---|---|---|
| `MNEMO_MODEL` | `small` | Whisper model name (`tiny`, `base`, `small`, `medium`, `large-v3`, …) or path. |
| `MNEMO_DEVICE` | `auto` | `cuda`, `cpu`, or `auto` (the GPU if there is one). |
| `MNEMO_COMPUTE_TYPE` | `float16` on GPU, `int8` on CPU | If the device doesn't support it, the best supported type is used instead, with a warning. Older GPUs (before compute capability 7.0, such as Pascal cards) have no efficient `float16` and get `int8_float32`. |
| `MNEMO_NUM_WORKERS` | 2 on GPU, CPU cores / 4 on CPU | Files transcribed at the same time. On GPU each one needs extra video memory. |
| `MNEMO_CPU_THREADS` | CPU cores / workers | Threads per worker. |
| `MNEMO_LANGUAGE` | detected per file | Language code, such as `en` or `it`. Setting it skips detection. |
| `MNEMO_BEAM_SIZE` | `5` | Beam search width. `1` is faster and a bit less accurate. |
| `MNEMO_VAD_FILTER` | `true` | Skip silences before transcribing. This avoids invented text during long pauses. |

## Logs

Every run writes into `logs/` in the repo, with a timestamp in each file name:

| File | Contents |
|---|---|
| `mnemo_log_*.log` | Everything printed on screen. |
| `mnemo_selected_*.txt` | The files `--backup` selected. |
| `mnemo_backup_stdout_*.log`, `mnemo_backup_stderr_*.log` | rsync's output during `--backup`. |
| `mnemo_squeeze_dups_*.csv` | Every file `--squeeze-dups` copied or found up to date, as `was_dup,md5sum,path` (see below). |
| `mnemo_squeeze_dups_stderr_*.log` | rsync's errors during `--squeeze-dups`. |
| `mnemo_squeeze_dups_moved_*.csv` | Every file `--squeeze-dups` mode 1 moved to `mnemo-thrash`, as `moved_path,trash_path,kept_path,md5sum`. |

To follow a long backup from another terminal:

```bash
watch ls -lh ~/opt/mnemo/logs/mnemo_backup_*
```

## Troubleshooting

- **"Destination mountpoint is '/'"**: the drive isn't mounted, so the destination would end up on your system disk. Mount the drive and try again.
- **"Permission denied creating destination directory"**: same cause; check that the drive is mounted and writable.
- **"Destination … is inside source …"**: the destination is one of the folders being backed up, or lies inside one. Every run would copy the backup into itself. Pick a destination outside all sources, such as a sibling folder (`../something`).
- **"Source … would be copied to …, which overlaps it"** (`--squeeze-dups`): the source would be copied onto itself, usually because `--destination` is the source's parent folder. Use a different destination.
- **A new git repository is still being backed up, or an old one skipped**: the list of repositories is cached in `.cache/cache.json`. Delete that file to rescan, or use `--no-cache` for a single run.
- **"Backup completed with rsync warnings (code 23/24)"**: some files couldn't be read or vanished during the copy. Check the `*_stderr_*` log for details.


## Open issues

- adding new git repositories won't make them available i.e. the cache.json should always be updated to discover them

