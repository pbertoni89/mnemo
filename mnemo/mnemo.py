#!/usr/bin/env python3
import os
import sys
import argparse
import subprocess
import shutil
import logging
import time
import tempfile
import json
import csv
import hashlib
from collections import defaultdict

# Set up global directories
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
CACHE_DIR = os.path.join(DATA_DIR, ".cache")
DATE_SUFFIX = time.strftime("%Y%m%d-%H%M%S")

# Configure logger to tee to both stdout and a date-stamped file
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s: %(message)s', datefmt='%H:%M:%S')

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(formatter)
logger.addHandler(stdout_handler)

try:
    os.makedirs(LOGS_DIR, exist_ok=True)
    main_log_file = os.path.join(LOGS_DIR, f"mnemo_log_{DATE_SUFFIX}.log")
    file_handler = logging.FileHandler(main_log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    logger.warning(f"Failed to set up file logger in {LOGS_DIR}: {e}")


def format_elapsed(start_time):
    """Helper to format elapsed time in hours, minutes, seconds."""
    elapsed = int(time.time() - start_time)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


def get_mountpoint(path):
    """Find the mountpoint of a given path."""
    path = os.path.abspath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def get_directory_size(sources, exclude_list=None, blacklist_path=None):
    """Precompute total required size for the given sources using du, respecting exclusions."""
    cmd = ["du", "-scb"]

    if exclude_list:
        for exc in exclude_list:
            cmd.append(f"--exclude={exc}")

    if blacklist_path:
        cmd.append(f"--exclude-from={blacklist_path}")

    cmd.extend(sources)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr:
        for line in result.stderr.strip().split('\n'):
            logger.warning(f"Size calculation skipped some items: {line}")

    if not result.stdout.strip():
        logger.error("Failed to calculate sizes: no output from du.")
        return 0

    try:
        lines = result.stdout.strip().split('\n')
        total_line = lines[-1]
        if 'total' in total_line:
            return int(total_line.split()[0])
    except Exception as e:
        logger.error(f"Failed to parse du output: {e}")

    return 0


def find_heavy(limit=10, directory="."):
    """Prints the first `limit` heavier files under `directory`."""
    start_time = time.time()
    logger.info(f"--- Top {limit} heaviest files in {directory} ---")
    cmd = f"find {subprocess.list2cmdline([directory])} -type f -exec du -h {{}} + 2>/dev/null | sort -rh | head -n {limit}"
    subprocess.run(cmd, shell=True)
    logger.info(f"find_heavy elapsed time: {format_elapsed(start_time)}")


def find_crowded(limit=10, directory="."):
    """Prints the first `limit` directories with the most files under `directory`."""
    start_time = time.time()
    logger.info(f"--- Top {limit} most crowded directories in {directory} ---")
    cmd = f"find {subprocess.list2cmdline([directory])} -type d -exec sh -c 'echo \"$(find \"$1\" -maxdepth 1 -type f 2>/dev/null | wc -l) $1\"' _ {{}} \\; 2>/dev/null | sort -rn | head -n {limit}"
    subprocess.run(cmd, shell=True)
    logger.info(f"find_crowded elapsed time: {format_elapsed(start_time)}")


def prepare_destination(dest, dry_run):
    """Create the destination if needed and return its mountpoint, halting if it is '/'."""
    if not os.path.exists(dest):
        if dry_run:
            logger.info(f"[DRY-RUN] Would create destination directory: {dest}")
        else:
            try:
                os.makedirs(dest, exist_ok=True)
                logger.info(f"Created destination directory: {dest}")
            except PermissionError:
                logger.error(f"Permission denied creating destination directory '{dest}'. Is the destination partition mounted?")
                sys.exit(1)
            except OSError as e:
                logger.error(f"Failed to create destination directory '{dest}': {e}")
                sys.exit(1)

    mountpoint = get_mountpoint(dest if os.path.exists(dest) else os.path.dirname(dest))
    if mountpoint == '/':
        logger.error(f"Destination mountpoint is '/'. Expected an external mount. Halting.")
        sys.exit(1)
    return mountpoint


def md5_file(path, chunk_size=1024 * 1024):
    """Return the hex md5 digest of a file, or None if it cannot be read."""
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(chunk_size), b''):
                h.update(chunk)
    except OSError as e:
        logger.warning(f"Cannot hash {path}: {e}")
        return None
    return h.hexdigest()


def find_duplicate_groups(src):
    """Walk `src` and split its entries into duplicate groups and singles.

    Returns (groups, singles): groups is [(md5, [sorted paths])] for every set of
    byte-identical files; singles is [(path, md5 or None)] for every other entry
    (unique files, empty files, symlinks, unreadable files), which gets copied without
    asking. Files are first bucketed by size, so only same-size files get hashed:
    a single's md5 is None unless it happened to be hashed.
    """
    logger.info(f"Walking {src}...")
    by_size = defaultdict(list)
    singles = []
    for root, dirs, files in os.walk(src):
        # os.walk lists symlinks to directories under dirs without following them
        singles.extend((os.path.join(root, d), None) for d in dirs if os.path.islink(os.path.join(root, d)))
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.islink(path):
                    singles.append((path, None))
                    continue
                size = os.path.getsize(path)
            except OSError as e:
                logger.warning(f"Cannot stat {path}: {e}")
                singles.append((path, None))  # let rsync try, and report, it
                continue
            if size > 0:
                by_size[size].append(path)
            else:
                singles.append((path, None))

    candidates = []
    for paths in by_size.values():
        if len(paths) > 1:
            candidates.extend(paths)
        else:
            singles.append((paths[0], None))
    logger.info(f"Hashing {len(candidates)} same-size candidate files in {src}...")

    by_hash = defaultdict(list)
    for path in candidates:
        digest = md5_file(path)
        if digest:
            by_hash[digest].append(path)
        else:
            singles.append((path, None))

    groups = []
    for digest, paths in by_hash.items():
        if len(paths) > 1:
            groups.append((digest, sorted(paths)))
        else:
            singles.append((paths[0], digest))
    groups.sort(key=lambda g: g[1][0])
    return groups, singles


def rsync_file_list(chosen, singles, dest, dry_run):
    """Copy `chosen` duplicates and `singles`, both [(abs path, md5 or None)], to dest/<abs path>.

    Everything goes in one rsync --files-from pass, so files already up to date at the
    destination are skipped and re-runs are cheap. Every entry is also recorded in
    logs/mnemo_squeeze_dups_<date>.csv (was_dup,md5sum,path).
    Logs one line per file actually transferred and returns how many there were.
    Halts on fatal rsync errors; partial-transfer codes 23/24 only warn.
    """
    entries = [(True, path, digest) for path, digest in chosen] + [(False, path, digest) for path, digest in singles]

    csv_path = os.path.join(LOGS_DIR, f"mnemo_squeeze_dups_{DATE_SUFFIX}.csv")
    write_header = not os.path.exists(csv_path)
    with open(csv_path, 'a', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["was_dup", "md5sum", "path"])
        for was_dup, path, digest in entries:
            writer.writerow([str(was_dup).lower(), digest or "", path])
    logger.info(f"Entries for this source recorded in: {csv_path}")

    stderr_log_path = os.path.join(LOGS_DIR, f"mnemo_squeeze_dups_stderr_{DATE_SUFFIX}.log")
    prefix = "[DRY-RUN] Would copy" if dry_run else "Copied"
    chosen_set = {path for path, _digest in chosen}
    transferred = 0

    # rsync's input list is a throwaway temp file: NUL-separated (--from0) so any name is safe,
    # relative to '/', which is the rsync source. --files-from implies --relative, recreating
    # the absolute source path under dest; --out-format prints one line per transferred item.
    with tempfile.NamedTemporaryFile('w', prefix="mnemo_squeeze_dups_", suffix=".lst") as list_f:
        list_f.write(''.join(path.lstrip(os.sep) + '\0' for _was_dup, path, _digest in entries))
        list_f.flush()

        cmd = ["rsync", "-a", "--from0", f"--files-from={list_f.name}", "--out-format=%n"]
        if dry_run:
            cmd.append("--dry-run")
        cmd.extend(["/", dest + "/"])

        logger.info(f"Syncing {len(entries)} entries to {dest} (rsync stderr: {stderr_log_path})...")
        with open(stderr_log_path, 'a') as err_f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f, text=True, errors='replace')
            for line in proc.stdout:
                name = line.rstrip('\n')
                if not name or name.endswith('/'):
                    continue  # parent directories created along the way
                path = os.sep + name
                kind = "chosen duplicate" if path in chosen_set else "non-duplicate"
                logger.info(f"{prefix} ({kind}): {path}")
                transferred += 1
            returncode = proc.wait()

    up_to_date = len(entries) - transferred
    if up_to_date > 0:
        logger.info(f"{up_to_date} entries were already up to date at the destination.")

    if returncode == 0:
        return transferred
    if returncode in (23, 24):
        logger.warning(f"Copy completed with rsync warnings (code {returncode}). Check {stderr_log_path} for details.")
        return transferred
    if returncode == 11:
        logger.error(f"Insufficient space encountered during transfer (rsync code 11). Check {stderr_log_path} for details. Halting immediately.")
    else:
        logger.error(f"Rsync failed with return code {returncode}. Check {stderr_log_path} for details.")
    sys.exit(1)


def prompt_dup_choice(count):
    """Ask which duplicate to save. Returns an int in [1, count], or 'a', 'b', 's' or 'q'."""
    while True:
        try:
            ans = input(f"Save which one? [1-{count}], a=always first, b=always last, s=skip, q=quit: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return 'q'
        if ans in ('a', 'b', 's', 'q'):
            return ans
        if ans.isdigit() and 1 <= int(ans) <= count:
            return int(ans)
        print(f"Invalid choice '{ans}'.")


def squeeze_dups(dest_raw, sources, dry_run):
    """Copy each source to dest/<abs path>, keeping only one user-chosen copy of each set of duplicates.

    Per source: ask about every duplicate group first, then copy the chosen files plus
    every non-duplicate file in one rsync pass. On 'q', only the choices made so far
    in the current source are copied, and the remaining sources are not processed.
    """
    start_time = time.time()
    dest = os.path.abspath(dest_raw)
    mountpoint = prepare_destination(dest, dry_run)

    sticky = None  # 'a' or 'b' once the user picks "always first/last"
    copied = 0
    skipped = 0
    quit_requested = False

    for src in sources:
        groups, singles = find_duplicate_groups(src)
        logger.info(f"Found {len(groups)} groups of duplicates and {len(singles)} non-duplicate entries in {src}")
        chosen = []

        for i, (digest, paths) in enumerate(groups, 1):
            size = os.path.getsize(paths[0])
            logger.info(f"--- Duplicate group {i}/{len(groups)}: md5 {digest}, {size} bytes each ---")
            for n, path in enumerate(paths, 1):
                logger.info(f"  {n}) {path}")

            if sticky:
                choice = 1 if sticky == 'a' else len(paths)
            else:
                ans = prompt_dup_choice(len(paths))
                if ans == 'q':
                    quit_requested = True
                    break
                if ans == 's':
                    skipped += 1
                    logger.info("Skipped.")
                    continue
                if ans in ('a', 'b'):
                    sticky = ans
                    choice = 1 if ans == 'a' else len(paths)
                else:
                    choice = ans

            selected = paths[choice - 1]
            chosen.append((selected, digest))
            logger.info(f"Selected {selected}")

        if quit_requested:
            logger.info("Quit requested by user: copying only the duplicates chosen so far in this source.")
            singles = []

        # Fill in the md5 of singles that were never hashed (unique size), for the CSV.
        # Symlinks are recorded without one; unreadable files are left for rsync to report.
        to_hash = [i for i, (path, digest) in enumerate(singles) if digest is None and os.path.isfile(path) and not os.path.islink(path)]
        if to_hash:
            logger.info(f"Hashing {len(to_hash)} non-duplicate files in {src} for the CSV record...")
            for i in to_hash:
                path = singles[i][0]
                singles[i] = (path, md5_file(path))

        if chosen or singles:
            copied += rsync_file_list(chosen, singles, dest, dry_run)

        if quit_requested:
            break

    verb = "Would copy" if dry_run else "Copied"
    logger.info(f"{verb} {copied} files, skipped {skipped} duplicate groups.")
    logger.info(f"squeeze_dups elapsed time: {format_elapsed(start_time)}")
    if not dry_run and copied:
        logger.info(f"*** Reminder: Please safely unmount '{mountpoint}' before physically disconnecting the drive. ***")


def backup(dest_raw, sources, exclude, dry_run, verbose, blacklist=None, permissive=False, delete_mode=False, also_repos=False, no_cache=False):
    """Main backup logic handling Rsync, size checks, mountpoints, and file listing."""
    start_time = time.time()
    dest = os.path.abspath(dest_raw)

    cache_file = os.path.join(CACHE_DIR, "cache.json")

    mountpoint = prepare_destination(dest, dry_run)

    # Prepare exclusions for Rsync
    blacklist_path = None
    if blacklist:
        blacklist_path = os.path.abspath(blacklist)
        if not os.path.isfile(blacklist_path):
            logger.error(f"Blacklist file does not exist: {blacklist_path}")
            sys.exit(1)

    # Dynamically find and exclude git repositories using cache
    if not also_repos:
        git_excludes = []
        cache_data = {}
        git_cache = {}
        cache_updated = False

        if not no_cache and os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    cache_data = json.load(f)
                    git_cache = cache_data.get("git_repos", {})
            except Exception as e:
                logger.warning(f"Failed to read cache file: {e}")

        for src in sources:
            if not no_cache and src in git_cache:
                cached_repos = git_cache[src]
                logger.info(f"Loaded {len(cached_repos)} cached git repositories for {src}")
                git_excludes.extend(cached_repos)
            else:
                logger.info(f"Scanning for git repositories in {src}...")
                src_git_excludes = []
                try:
                    # -prune prevents traversing into the .git dir itself once found
                    cmd = ["find", src, "-type", "d", "-name", ".git", "-prune"]
                    find_res = subprocess.run(cmd, capture_output=True, text=True)
                    for line in find_res.stdout.splitlines():
                        line = line.strip()
                        if line:
                            repo_dir = os.path.dirname(line)
                            src_git_excludes.append(repo_dir)
                            git_excludes.append(repo_dir)

                    git_cache[src] = src_git_excludes
                    cache_updated = True
                except Exception as e:
                    logger.warning(f"Failed to scan for git repositories in {src}: {e}")

        if cache_updated and not no_cache:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                cache_data["git_repos"] = git_cache
                with open(cache_file, 'w') as f:
                    json.dump(cache_data, f, indent=4)
                logger.info("Updated git repositories cache.")
            except Exception as e:
                logger.warning(f"Failed to write cache file: {e}")

        if git_excludes:
            exclude.extend(git_excludes)

    # List selected files (using rsync dry-run to perfectly respect exclusions)
    logger.info("Listing selected files...")
    list_cmd = ["rsync", "-aR", "--dry-run", "--out-format=%n"]
    if delete_mode:
        list_cmd.append("--delete")
    for exc in exclude:
        list_cmd.extend(["--exclude", exc])
    if blacklist_path:
        list_cmd.append(f"--exclude-from={blacklist_path}")
    list_cmd.extend(sources)
    list_cmd.append(dest + "/")

    list_result = subprocess.run(list_cmd, capture_output=True, text=True)

    if list_result.stderr:
        for line in list_result.stderr.strip().split('\n'):
            if line:
                logger.warning(f"File listing skipped some items: {line}")

    files_list = [f for f in list_result.stdout.split('\n') if f.strip()]

    if verbose:
        for f in files_list:
            print(f)
    else:
        selected_log_path = os.path.join(LOGS_DIR, f"mnemo_selected_{DATE_SUFFIX}.txt")
        with open(selected_log_path, 'w') as tf:
            tf.write("\n".join(files_list) + "\n")
        logger.info(f"Selected files list dumped to: {selected_log_path}")

    # Precompute size requirements
    logger.info("Calculating required disk space...")
    required_bytes = get_directory_size(sources, exclude_list=exclude, blacklist_path=blacklist_path)
    free_bytes = shutil.disk_usage(mountpoint).free

    logger.info(f"Required space: {required_bytes / (1024**3):.2f} GB ({len(files_list)} total files under sync)")
    logger.info(f"Available space: {free_bytes / (1024**3):.2f} GB")

    if required_bytes > free_bytes:
        if permissive:
            logger.info("Permissive mode active: ignoring space constraints and proceeding.")
        else:
            logger.warning("Insufficient space on the destination partition (based on total source size).")
            try:
                ans = input("Do you want to proceed with the backup anyway (assuming delta is smaller)? [y/N]: ").strip().lower()
                if ans != 'y':
                    ans_ncdu = input("Do you want to run 'ncdu' on the heaviest source entry to investigate? [y/N]: ").strip().lower()
                    if ans_ncdu == 'y':
                        if shutil.which("ncdu"):
                            logger.info("Determining the heaviest source entry...")
                            source_sizes = []
                            for src in sources:
                                source_sizes.append((get_directory_size([src], exclude, blacklist_path), src))

                            # Sort by size descending and pick ONLY the top 1
                            source_sizes.sort(key=lambda x: x[0], reverse=True)
                            heaviest_source = source_sizes[0][1]

                            logger.info(f"Launching ncdu on: {heaviest_source}")
                            subprocess.run(["ncdu", heaviest_source])
                        else:
                            logger.error("The 'ncdu' utility is not installed or not found in PATH.")
                    logger.info("Halting backup process.")
                    sys.exit(1)
            except KeyboardInterrupt:
                print()
                logger.info("Prompt aborted by user.")
                sys.exit(1)

    # Deletion Mode Safety Prompt
    if delete_mode and not dry_run:
        logger.warning("DELETE MODE ENABLED. Files at the destination not present in the sources will be permanently removed.")
        try:
            ans = input("Are you sure you want to proceed with deletion? [y/N]: ").strip().lower()
            if ans != 'y':
                logger.info("Aborted by user.")
                sys.exit(1)
        except KeyboardInterrupt:
            print()
            logger.info("Prompt aborted by user.")
            sys.exit(1)

    # Build final rsync command
    rsync_cmd = ["rsync", "-aRv"]
    if dry_run:
        rsync_cmd.append("--dry-run")
    if delete_mode:
        rsync_cmd.append("--delete")

    for exc in exclude:
        rsync_cmd.extend(["--exclude", exc])

    if blacklist_path:
        rsync_cmd.append(f"--exclude-from={blacklist_path}")

    # Rsync inherently processes arguments in exact array order
    rsync_cmd.extend(sources)
    rsync_cmd.append(dest + "/")

    if dry_run:
        logger.info(f"[DRY-RUN] Would execute: {' '.join(rsync_cmd)}")
        logger.info(f"backup elapsed time: {format_elapsed(start_time)}")
        return

    # Run backup
    logger.info(f"Starting backup...")

    stdout_log_path = os.path.join(LOGS_DIR, f"mnemo_backup_stdout_{DATE_SUFFIX}.log")
    stderr_log_path = os.path.join(LOGS_DIR, f"mnemo_backup_stderr_{DATE_SUFFIX}.log")

    tail_proc = None
    try:
        with open(stdout_log_path, 'w') as out_f, open(stderr_log_path, 'w') as err_f:
            logger.info(f"Rsync stdout redirected to: {out_f.name}")
            logger.info(f"Rsync stderr redirected to: {err_f.name}")
            logger.info(f"To monitor progress in another terminal, run: watch ls -lh {LOGS_DIR}/mnemo_backup_*")

            logger.info("Starting live tail of rsync stdout...")
            # Spawn tail in the background to output to the current terminal
            tail_proc = subprocess.Popen(["tail", "-f", out_f.name])

            backup_result = subprocess.run(rsync_cmd, stdout=out_f, stderr=err_f)

            if tail_proc:
                tail_proc.terminate()
                tail_proc.wait()
                tail_proc = None

            # 11 = Error in file I/O (e.g. ENOSPC / Disk Full)
            if backup_result.returncode == 11:
                logger.error(f"Insufficient space encountered during transfer (rsync code 11). Check {err_f.name} for details. Halting immediately.")
                sys.exit(1)

            # Codes 23 and 24 are partial transfers due to permissions or vanished files
            elif backup_result.returncode in (0, 23, 24):
                if backup_result.returncode != 0:
                    logger.warning(f"Backup completed with rsync warnings (code {backup_result.returncode}). Check {err_f.name} for details.")
                else:
                    logger.info("Backup completed successfully.")
            else:
                logger.error(f"Rsync failed with return code {backup_result.returncode}. Check {err_f.name} for details.")
                sys.exit(1)
    except KeyboardInterrupt:
        print()
        logger.critical("Backup interrupted by user (KeyboardInterrupt). Exiting gracefully.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Backup execution failed: {e}")
        sys.exit(1)
    finally:
        if tail_proc:
            tail_proc.terminate()
            tail_proc.wait()
        logger.info(f"backup elapsed time: {format_elapsed(start_time)}")
        logger.info(f"*** Reminder: Please safely unmount '{mountpoint}' before physically disconnecting the drive. ***")


def main(args=None):
    parser = argparse.ArgumentParser(description="Gradual backup script over rsync")

    # Mutually exclusive group for the main operational modes
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup", action="store_true", help="Run the gradual backup process")
    group.add_argument("--heavy", type=int, nargs='?', const=10, metavar='N', help="Find the N heaviest files in the sources")
    group.add_argument("--crowded", type=int, nargs='?', const=10, metavar='N', help="Find the N most crowded directories in the sources")
    group.add_argument("--squeeze-dups", action="store_true", help="Copy each source to the destination, keeping one interactively chosen copy of each set of identical files")

    parser.add_argument("--destination", default="/run/media/pbertoni/storage/pb-backup/", help="Destination directory (USB mount expected)")
    parser.add_argument("--source", nargs='*', default=[], help="Directories to backup/analyze")
    parser.add_argument("--whitelist", help="Path to a file containing source directories")
    parser.add_argument("--exclude", nargs='*', default=[], help="Patterns to exclude")
    parser.add_argument("--blacklist", help="Path to a file containing exclusion patterns for rsync")
    parser.add_argument("--permissive", action="store_true", help="Skip sufficient space checks and prompt for continuation")
    parser.add_argument("--delete", action="store_true", help="Enable rsync deletion mode")
    parser.add_argument("--also-repos", action="store_true", help="Do not automatically exclude git repositories")
    parser.add_argument("--no-cache", action="store_true", help="Disable caching of scanned results (like git repositories)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print selected files to stdout instead of a temp file")
    parser.add_argument("--dry-run", action="store_true", help="Print actions and exit")

    parsed_args = parser.parse_args(args)

    raw_sources = parsed_args.source[:]

    # Read whitelist if provided
    if parsed_args.whitelist:
        whitelist_path = os.path.abspath(parsed_args.whitelist)
        if not os.path.isfile(whitelist_path):
            logger.error(f"Whitelist file does not exist: {whitelist_path}")
            sys.exit(1)

        with open(whitelist_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    raw_sources.append(line)

    if not raw_sources:
        logger.error("No source directories provided. You must specify --source or provide a non-empty --whitelist file.")
        sys.exit(1)

    sources = [os.path.abspath(src) for src in raw_sources]

    # Ensure all sources exist
    for src in sources:
        if not os.path.isdir(src):
            logger.error(f"Source directory does not exist: {src}")
            sys.exit(1)

    # Handle utility routines
    if parsed_args.heavy is not None:
        for src in sources:
            find_heavy(parsed_args.heavy, src)
        sys.exit(0)

    if parsed_args.crowded is not None:
        for src in sources:
            find_crowded(parsed_args.crowded, src)
        sys.exit(0)

    if parsed_args.squeeze_dups:
        squeeze_dups(parsed_args.destination, sources, parsed_args.dry_run)
        sys.exit(0)

    # Proceed with --backup logic
    if parsed_args.backup:
        backup(
            dest_raw=parsed_args.destination,
            sources=sources,
            exclude=parsed_args.exclude,
            dry_run=parsed_args.dry_run,
            verbose=parsed_args.verbose,
            blacklist=parsed_args.blacklist,
            permissive=parsed_args.permissive,
            delete_mode=parsed_args.delete,
            also_repos=parsed_args.also_repos,
            no_cache=parsed_args.no_cache
        )


if __name__ == "__main__":
    main()
