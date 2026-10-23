#!/usr/bin/env python3
import os
import sys
import argparse
import subprocess
import shutil
import logging
import time
import tempfile

# Update logger to use sys.stdout instead of sys.stderr
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


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


def backup(dest_raw, sources, exclude, dry_run, verbose, blacklist=None, permissive=False, delete_mode=False, also_repos=False):
    """Main backup logic handling Rsync, size checks, mountpoints, and file listing."""
    start_time = time.time()
    dest = os.path.abspath(dest_raw)

    # Destination creation and mountpoint validation
    if not os.path.exists(dest):
        if dry_run:
            logger.info(f"[DRY-RUN] Would create destination directory: {dest}")
        else:
            os.makedirs(dest, exist_ok=True)
            logger.info(f"Created destination directory: {dest}")

    mountpoint = get_mountpoint(dest if os.path.exists(dest) else os.path.dirname(dest))
    if mountpoint == '/':
        logger.error(f"Destination mountpoint is '/'. Expected an external mount. Halting.")
        sys.exit(1)

    # Prepare exclusions for Rsync
    blacklist_path = None
    if blacklist:
        blacklist_path = os.path.abspath(blacklist)
        if not os.path.isfile(blacklist_path):
            logger.error(f"Blacklist file does not exist: {blacklist_path}")
            sys.exit(1)

    # Dynamically find and exclude git repositories
    if not also_repos:
        logger.info("Scanning for git repositories to exclude...")
        git_excludes = []
        for src in sources:
            try:
                # -prune prevents traversing into the .git dir itself once found
                cmd = ["find", src, "-type", "d", "-name", ".git", "-prune"]
                find_res = subprocess.run(cmd, capture_output=True, text=True)
                for line in find_res.stdout.splitlines():
                    line = line.strip()
                    if line:
                        repo_dir = os.path.dirname(line)
                        git_excludes.append(repo_dir)
            except Exception as e:
                logger.warning(f"Failed to scan for git repositories in {src}: {e}")

        if git_excludes:
            logger.info(f"Found and automatically excluding {len(git_excludes)} git repositories.")
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
        with tempfile.NamedTemporaryFile(delete=False, mode='w', prefix="mnemo_selected_", suffix=".txt") as tf:
            tf.write("\n".join(files_list) + "\n")
            logger.info(f"Selected files list dumped to: {tf.name}")

    # Precompute size requirements
    logger.info("Calculating required disk space...")
    required_bytes = get_directory_size(sources, exclude_list=exclude, blacklist_path=blacklist_path)
    free_bytes = shutil.disk_usage(mountpoint).free

    logger.info(f"Required space: {required_bytes / (1024**3):.2f} GB")
    logger.info(f"Available space: {free_bytes / (1024**3):.2f} GB")

    if required_bytes > free_bytes:
        if permissive:
            logger.warning("Insufficient space on the destination partition (--permissive flag is active).")
            try:
                ans = input("Do you want to proceed with the backup anyway? [y/N]: ").strip().lower()
                if ans != 'y':
                    logger.info("Halting backup process.")
                    sys.exit(1)
            except KeyboardInterrupt:
                print()
                logger.info("Prompt aborted by user.")
                sys.exit(1)
        else:
            logger.error("Insufficient space on the destination partition.")
            try:
                ans = input("Do you want to run 'ncdu' on the heaviest source entry to investigate? [y/N]: ").strip().lower()
                if ans == 'y':
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
            except KeyboardInterrupt:
                print()
                logger.info("Prompt aborted by user.")

            logger.info("Halting backup process.")
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
    try:
        with tempfile.NamedTemporaryFile(delete=False, mode='w', prefix="mnemo_backup_stdout_", suffix=".log") as out_f, \
             tempfile.NamedTemporaryFile(delete=False, mode='w', prefix="mnemo_backup_stderr_", suffix=".log") as err_f:

            logger.info(f"Rsync stdout redirected to: {out_f.name}")
            logger.info(f"Rsync stderr redirected to: {err_f.name}")

            backup_result = subprocess.run(rsync_cmd, stdout=out_f, stderr=err_f)

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
    except Exception as e:
        logger.error(f"Backup execution failed: {e}")
        sys.exit(1)
    finally:
        logger.info(f"backup elapsed time: {format_elapsed(start_time)}")


def main(args=None):
    parser = argparse.ArgumentParser(description="Gradual backup script over rsync")

    # Mutually exclusive group for the main operational modes
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup", action="store_true", help="Run the gradual backup process")
    group.add_argument("--heavy", type=int, nargs='?', const=10, metavar='N', help="Find the N heaviest files in the sources")
    group.add_argument("--crowded", type=int, nargs='?', const=10, metavar='N', help="Find the N most crowded directories in the sources")

    parser.add_argument("--destination", default="/run/media/pbertoni/storage/pb-backup/", help="Destination directory (USB mount expected)")
    parser.add_argument("--source", nargs='*', default=[], help="Directories to backup/analyze")
    parser.add_argument("--whitelist", help="Path to a file containing source directories")
    parser.add_argument("--exclude", nargs='*', default=[], help="Patterns to exclude")
    parser.add_argument("--blacklist", help="Path to a file containing exclusion patterns for rsync")
    parser.add_argument("--permissive", action="store_true", help="Skip sufficient space checks and prompt for continuation")
    parser.add_argument("--delete", action="store_true", help="Enable rsync deletion mode")
    parser.add_argument("--also-repos", action="store_true", help="Do not automatically exclude git repositories")
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
            also_repos=parsed_args.also_repos
        )


if __name__ == "__main__":
    main()
