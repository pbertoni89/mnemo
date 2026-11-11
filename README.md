# mnemo

A small backup helper for Linux. It copies the directories you care about onto an external drive with `rsync`, and it gives you a few tools for deciding what is worth copying.

- **Incremental**: only new or changed files are copied, so the second run is much faster than the first.
- **Safe by default**: it refuses to write anywhere that isn't a separate mount or that lies inside a folder being backed up, checks free space before starting, asks before deleting anything, and has `--dry-run` for every mode.
- **Readable layout**: `--backup` puts files at `DESTINATION/<their absolute path>`. For example, `/home/me/Documents/cv.pdf` ends up at `DESTINATION/home/me/Documents/cv.pdf`, so restoring a file is a plain copy. `--squeeze-dups` instead puts each source folder directly under the destination, by its own name (see below).

## Requirements

- Python 3 (standard library only)
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
mnemo.py (--backup | --heavy [N] | --crowded [N] | --squeeze-dups) [options]
```

Pick exactly one mode.

| Mode | What it does |
|---|---|
| `--backup` | Copies the sources to the destination. |
| `--heavy [N]` | Lists the N largest files in each source (default 10). |
| `--crowded [N]` | Lists the N directories with the most files in each source (default 10). |
| `--squeeze-dups` | Copies the sources to the destination, keeping only one copy of each set of identical files. You choose which copy. |

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

## Examples

### Back up everything in your whitelist

```bash
cd ~/opt/mnemo
./mnemo/mnemo.py --whitelist whitelist.txt --blacklist blacklist.txt --backup
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
./mnemo/mnemo.py --whitelist whitelist.txt --blacklist blacklist.txt --backup --dry-run
```

### Back up a single folder to another drive

```bash
./mnemo/mnemo.py --backup --source ~/Pictures --destination /run/media/me/BIGDISK/backup
```

### Make the backup an exact mirror

This also removes files you have deleted locally:

```bash
./mnemo/mnemo.py --whitelist whitelist.txt --blacklist blacklist.txt --backup --delete
# DELETE MODE ENABLED. Files at the destination not present in the sources will be permanently removed.
# Are you sure you want to proceed with deletion? [y/N]:
```

### Find out why the backup is too big

```bash
./mnemo/mnemo.py --whitelist whitelist.txt --heavy 20     # the 20 largest files
./mnemo/mnemo.py --whitelist whitelist.txt --crowded 20   # the 20 most crowded folders
```

Add the culprits to `blacklist.txt` and run the backup again. If the destination is too small, `--backup` also offers to open `ncdu` on your largest source.

### Back up a messy folder without its duplicates

`--squeeze-dups` suits folders like `Downloads` or an old photo dump, where the same file sits in several places. It copies everything, but for each set of byte-identical files it shows you every copy and asks which one to keep:

```bash
./mnemo/mnemo.py --squeeze-dups --source ~/Downloads --destination /run/media/me/BIGDISK/squeezed
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

```text
--- Duplicate group 1/2: md5 b1946ac92492d2347c6235b4d2611184, 6 bytes each ---
  1) /home/me/Downloads/baz/gem.txt
  2) /home/me/Downloads/foo/bar.txt
Save which one? [1-2], a=always first, b=always last, s=skip, q=quit: 1
```

Pressing `1` saves `DESTINATION/Downloads/baz/gem.txt`. `foo/bar.txt` is not copied.

| Answer | Effect |
|---|---|
| `1`, `2`, … | Keep that copy. |
| `a` | Keep the first copy, for this group and every group after it, without asking again. |
| `b` | Keep the last copy, for this group and every group after it, without asking again. |
| `s` | Skip this group: none of its copies are copied. |
| `q` | Quit. The copies chosen so far in the current folder are still saved; nothing else is copied. |

Things to know:

- Your answers are collected first. The copying happens in one go afterwards, so you can answer and walk away.
- Files with no duplicates are copied without asking, along with empty files and symlinks.
- Each file actually copied gets its own line on screen and in the log, for example `Copied (non-duplicate): /home/me/Downloads/x/u` or `Copied (chosen duplicate): /home/me/Downloads/baz/gem.txt`. Files already up to date at the destination aren't copied again; only their number is reported.
- Duplicates are matched only within the same `--source` folder, not across folders.
- The blacklist does **not** apply to this mode.
- Your source folders are never modified. To actually free space, delete the extra copies yourself.
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

## Logs

Every run writes into `logs/` in the repo, with a timestamp in each file name:

| File | Contents |
|---|---|
| `mnemo_log_*.log` | Everything printed on screen. |
| `mnemo_selected_*.txt` | The files `--backup` selected. |
| `mnemo_backup_stdout_*.log`, `mnemo_backup_stderr_*.log` | rsync's output during `--backup`. |
| `mnemo_squeeze_dups_*.csv` | Every file `--squeeze-dups` copied or found up to date, as `was_dup,md5sum,path` (see below). |
| `mnemo_squeeze_dups_stderr_*.log` | rsync's errors during `--squeeze-dups`. |

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
