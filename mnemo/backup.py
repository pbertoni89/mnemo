"""--backup: incremental rsync of the sources to DEST/<absolute path>."""
import json
import os
import shutil
import subprocess
import sys
import time

from mnemo import (
    CACHE_DIR, LOGS_DIR, DATE_SUFFIX, logger,
    format_elapsed, get_directory_size, ask_yes_no,
    check_destination_outside_sources, prepare_destination,
)


def backup(dest_raw, sources, exclude, dry_run, verbose, blacklist=None, permissive=False, delete_mode=False,
           also_repos=False, no_cache=False):
    """Main backup logic handling Rsync, size checks, mountpoints, and file listing."""
    start_time = time.time()
    dest = os.path.abspath(dest_raw)

    cache_file = os.path.join(CACHE_DIR, "cache.json")

    check_destination_outside_sources(dest, sources)
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
            proceed = ask_yes_no("Do you want to proceed with the backup anyway (assuming delta is smaller)? [y/N]: ")
            if proceed is None:
                logger.info("Prompt aborted by user.")
                sys.exit(1)
            if not proceed:
                if ask_yes_no("Do you want to run 'ncdu' on the heaviest source entry to investigate? [y/N]: "):
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

    # Deletion Mode Safety Prompt
    if delete_mode and not dry_run:
        logger.warning("DELETE MODE ENABLED. "
                       "Files at the destination not present in the sources will be permanently removed.")
        if not ask_yes_no("Are you sure you want to proceed with deletion? [y/N]: "):
            logger.info("Aborted by user.")
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
    logger.info("Starting backup...")

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
                logger.error(f"Insufficient space encountered during transfer (rsync code 11). "
                             f"Check {err_f.name} for details. Halting immediately.")
                sys.exit(1)

            # Codes 23 and 24 are partial transfers due to permissions or vanished files
            elif backup_result.returncode in (0, 23, 24):
                if backup_result.returncode != 0:
                    logger.warning(f"Backup completed with rsync warnings (code {backup_result.returncode}). "
                                   f"Check {err_f.name} for details.")
                else:
                    logger.info("Backup completed successfully.")
            else:
                logger.error(f"Rsync failed with return code {backup_result.returncode}. "
                             f"Check {err_f.name} for details.")
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
        logger.info(f"*** Reminder: Please safely unmount '{mountpoint}' "
                    "before physically disconnecting the drive. ***")
