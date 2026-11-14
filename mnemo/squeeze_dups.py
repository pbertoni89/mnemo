"""--squeeze-dups: keep one user-chosen copy of each set of identical files.

Mode 1 moves the discarded copies to DEST/<source name>/mnemo-thrash; mode 2 copies a squeezed
tree of each source to DEST/<source name>.
"""
import csv
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

from mnemo import (
    LOGS_DIR, DATE_SUFFIX, logger,
    format_elapsed, is_within, du_bytes, human_size, md5_file, ask_yes_no, ask_number,
    check_destination_outside_sources, prepare_destination,
)


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


def squeeze_target(src, dest):
    """Where --squeeze-dups puts `src`: dest/<basename of src>."""
    return os.path.join(dest, os.path.basename(src))


def rsync_file_list(chosen, singles, src, dest, dry_run):
    """Copy `chosen` duplicates and `singles`, both [(abs path, md5 or None)] under `src`, to dest/<basename of src>/.

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

    # rsync's input list is a throwaway temp file: NUL-separated (--from0) so any name is safe.
    # Paths are relative to the parent of src, which is the rsync source dir; --files-from implies
    # --relative, so they are recreated as dest/<basename of src>/... --out-format prints one line
    # per transferred item, relative to that same parent.
    base = os.path.dirname(src)
    with tempfile.NamedTemporaryFile('w', prefix="mnemo_squeeze_dups_", suffix=".lst") as list_f:
        list_f.write(''.join(os.path.relpath(path, base) + '\0' for _was_dup, path, _digest in entries))
        list_f.flush()

        cmd = ["rsync", "-a", "--from0", f"--files-from={list_f.name}", "--out-format=%n"]
        if dry_run:
            cmd.append("--dry-run")
        cmd.extend([base.rstrip(os.sep) + "/", dest + "/"])

        logger.info(f"Syncing {len(entries)} entries to {squeeze_target(src, dest)} "
                    f"(rsync stderr: {stderr_log_path})...")
        with open(stderr_log_path, 'a') as err_f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f, text=True, errors='replace')
            for line in proc.stdout:
                name = line.rstrip('\n')
                if not name or name.endswith('/'):
                    continue  # parent directories created along the way
                path = os.path.join(base, name)
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
        logger.error(f"Insufficient space encountered during transfer (rsync code 11). "
                     f"Check {stderr_log_path} for details. Halting immediately.")
    else:
        logger.error(f"Rsync failed with return code {returncode}. Check {stderr_log_path} for details.")
    sys.exit(1)


def choose_from_groups(groups, sticky):
    """Show each duplicate group and ask which copy to keep.

    Answers: N keeps that copy; a/b keep the first/last copy of this and every later group
    (sticky, no more prompts); s skips the group; q (or Ctrl-C/EOF) stops asking.
    `sticky` carries a previous a/b across sources.
    Returns (picks, skipped, sticky, quit_requested), where picks is [(digest, paths, kept_path)].
    """
    picks = []
    skipped = 0
    for i, (digest, paths) in enumerate(groups, 1):
        size = os.path.getsize(paths[0])
        logger.info(f"--- Duplicate group {i}/{len(groups)}: md5 {digest}, {size} bytes each ---")
        for n, path in enumerate(paths, 1):
            logger.info(f"  {n}) {path}")

        if sticky:
            choice = 1 if sticky == 'a' else len(paths)
        else:
            ans = ask_number(f"Save which one? [1-{len(paths)}], a=always first, b=always last, s=skip, q=quit: ",
                             len(paths), letters=('a', 'b', 's', 'q'))
            if ans is None or ans == 'q':
                return picks, skipped, sticky, True
            if ans == 's':
                skipped += 1
                logger.info("Skipped.")
                continue
            if ans in ('a', 'b'):
                sticky = ans
                choice = 1 if ans == 'a' else len(paths)
            else:
                choice = ans

        kept = paths[choice - 1]
        picks.append((digest, paths, kept))
        logger.info(f"Selected {kept}")
    return picks, skipped, sticky, False


SQUEEZE_MOVE, SQUEEZE_MERGE = 1, 2

SQUEEZE_TRASH_DIR = "mnemo-trash"


def ask_squeeze_mode():
    """Ask whether to move discarded duplicates out of the sources or copy a squeezed tree to the destination."""
    print("How do you want to squeeze duplicates?\n"
          f"  1) Just remove the discarded ones (moved out of the sources into DEST/<source name>/{SQUEEZE_TRASH_DIR}, "
          "nothing else is copied)\n"
          "  2) Squeeze and merge inside destination (the sources are left untouched)")
    mode = ask_number("Choice [1-2]: ", 2)
    if mode is None:
        logger.info("Aborted by user.")
        sys.exit(1)
    return mode


def trash_dir(src, dest):
    """Where mode 1 moves the discarded duplicates of `src`: dest/<basename of src>/mnemo-thrash."""
    return os.path.join(squeeze_target(src, dest), SQUEEZE_TRASH_DIR)


def free_trash_path(path):
    """`path`, or `path~1`, `path~2`, ... if it is already taken (never overwrite an earlier run's trash)."""
    candidate, n = path, 0
    while os.path.lexists(candidate):
        n += 1
        candidate = f"{path}~{n}"
    return candidate


def move_verified(path, target, digest):
    """Move `path` to `target`; across filesystems, copy, check the md5 of the copy, then delete the original.

    Raises OSError if anything fails; the original is only removed once an identical copy is in place.
    """
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        os.rename(path, target)  # same filesystem: atomic, content untouched
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    partial = f"{target}.mnemo-partial"
    try:
        shutil.copy2(path, partial)
        if md5_file(partial) != digest:
            raise OSError(f"the copy at {partial} does not match md5 {digest} (did the file change since the scan?)")
        os.replace(partial, target)
    except BaseException:
        if os.path.exists(partial):
            os.remove(partial)
        raise
    os.remove(path)


def move_discarded(moves, src, dest, dry_run):
    """Move each discarded duplicate of `src` to dest/<basename>/mnemo-thrash/<path relative to src>.

    `moves` is [(discarded_path, kept_path, digest)]. Every move is recorded in
    logs/mnemo_squeeze_dups_moved_<date>.csv (moved_path,trash_path,kept_path,md5sum).
    Returns (moved_count, moved_bytes).
    """
    csv_path = os.path.join(LOGS_DIR, f"mnemo_squeeze_dups_moved_{DATE_SUFFIX}.csv")
    trash = trash_dir(src, dest)
    moved = 0
    moved_bytes = 0
    verified_kept = {}  # kept path -> whether it still has the expected md5 (hash each kept copy once)

    for discarded, kept, digest in moves:
        target = os.path.join(trash, os.path.relpath(discarded, src))
        if dry_run:
            logger.info(f"[DRY-RUN] Would move: {discarded} -> {target} (keeping {kept})")
            moved += 1
            moved_bytes += os.path.getsize(discarded)
            continue

        # The files were hashed during the scan: if the kept copy changed since, it no longer stands in
        # for the discarded one, so leave the discarded one where it is
        if kept not in verified_kept:
            verified_kept[kept] = md5_file(kept) == digest
        if not verified_kept[kept]:
            logger.warning(f"Not moving {discarded}: the kept copy {kept} is missing or changed since the scan.")
            continue
        try:
            if os.path.samefile(discarded, kept):
                # A hardlink or bind-mount alias of the kept file: moving it frees nothing, and
                # with a bind mount it would move the kept copy itself out of the source
                logger.warning(f"Not moving {discarded}: it is the same file as {kept} (hardlink or bind mount).")
                continue
            target = free_trash_path(target)
            size = os.path.getsize(discarded)
            move_verified(discarded, target, digest)
        except OSError as e:
            logger.error(f"Failed to move {discarded}: {e}")
            continue

        moved += 1
        moved_bytes += size
        logger.info(f"Moved: {discarded} -> {target} (kept {kept})")
        write_header = not os.path.exists(csv_path)
        with open(csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["moved_path", "trash_path", "kept_path", "md5sum"])
            writer.writerow([discarded, target, kept, digest])

    if moved and not dry_run:
        logger.info(f"Moves recorded in: {csv_path}")
    return moved, moved_bytes


def move_dups(sources, dest, dry_run, mountpoint):
    """Mode 1 of --squeeze-dups: keep one user-chosen copy of each set of duplicates in place, and move the
    others to dest/<basename>/mnemo-thrash, so they leave the source without being lost.

    Per source: ask about every group, then move. On 'q', the choices made so far in the current
    source are still carried out, and the remaining sources are not processed.
    """
    start_time = time.time()
    sticky = None
    moved_total = 0
    skipped_total = 0
    spared = 0
    processed = []

    for src in sources:
        groups, _singles = find_duplicate_groups(src)
        logger.info(f"Found {len(groups)} groups of duplicates in {src}")
        picks, skipped, sticky, quit_requested = choose_from_groups(groups, sticky)
        skipped_total += skipped
        if quit_requested:
            logger.info("Quit requested by user: handling only the duplicates chosen so far in this source.")

        moves = [(path, kept, digest) for digest, paths, kept in picks for path in paths if path != kept]
        if moves:
            size_before = du_bytes(src)
            moved, moved_bytes = move_discarded(moves, src, dest, dry_run)
            moved_total += moved
            if dry_run:
                spared += moved_bytes
            else:
                size_after = du_bytes(src)
                if size_before is not None and size_after is not None:
                    spared += size_before - size_after
                processed.append((src, size_before, size_after))
        else:
            logger.info(f"Nothing to move out of {src}.")

        if quit_requested:
            break

    verb = "Would move" if dry_run else "Moved"
    logger.info(f"{verb} {moved_total} files out of the sources, skipped {skipped_total} duplicate groups.")
    if processed:
        logger.info("--- Space used (du -sh) ---")
        for src, before, after in processed:
            logger.info(f"  {human_size(before):>6} -> {human_size(after):>6}  {src}")
            trash = trash_dir(src, dest)
            logger.info(f"  {human_size(du_bytes(trash)):>16}  {trash}")
    spared_label = "would spare about" if dry_run else "spared space:"
    logger.info(f"squeeze_dups elapsed time: {format_elapsed(start_time)}, {spared_label} {human_size(spared)}")
    if not dry_run and moved_total:
        logger.info(f"*** Reminder: Please safely unmount '{mountpoint}' "
                    "before physically disconnecting the drive. ***")


def squeeze_dups(dest_raw, sources, dry_run):
    """Keep only one user-chosen copy of each set of duplicates, in one of two modes (asked first).

    1) move_dups(): move the discarded copies out of the sources into dest/<basename>/mnemo-thrash.
    2) Copy each source to dest/<basename of source>: ask about every duplicate group first,
       then copy the chosen files plus every non-duplicate file in one rsync pass. On 'q', only
       the choices made so far in the current source are copied, and the remaining sources are
       not processed. At the end, logs the du -sh size of each processed source and of its copy.
    Both modes write under dest/<basename of source>, so they share the same checks and confirmation.
    """
    mode = ask_squeeze_mode()
    start_time = time.time()
    dest = os.path.abspath(dest_raw)

    # Sources land at dest/<basename>, so two sources with the same basename would merge
    basenames = defaultdict(list)
    for src in sources:
        basenames[os.path.basename(src)].append(src)
    clashes = {name: srcs for name, srcs in basenames.items() if len(srcs) > 1}
    if clashes:
        for name, srcs in clashes.items():
            logger.error(f"Sources {', '.join(srcs)} would all be written to {os.path.join(dest, name)}.")
        logger.error("Each source must have a different basename. Halting.")
        sys.exit(1)

    check_destination_outside_sources(dest, sources)
    # e.g. --source /x/foo --destination /x would write /x/foo (or /x/foo/mnemo-thrash) onto the source
    for src in sources:
        target = squeeze_target(src, dest)
        if is_within(target, src) or is_within(src, target):
            logger.error(f"Source {src} would be written to {target}, which overlaps it. Halting.")
            sys.exit(1)

    # The default destination is easy to forget, so confirm it before creating or reading anything.
    # A dry run writes nothing, so it only states where the output would go.
    if mode == SQUEEZE_MOVE:
        dirs = [trash_dir(src, dest) for src in sources]
        what = "Discarded copies will be moved to"
    else:
        dirs = [squeeze_target(src, dest) for src in sources]
        what = "Output directories will be" if len(sources) > 1 else "Output directory will be"
    targets = ", ".join(dirs)
    if dry_run:
        logger.info(f"[DRY-RUN] {what} {targets}")
    else:
        if not ask_yes_no(f"Ready to squeeze duplicates. {what} {targets}. Proceed? [y/N]: "):
            logger.info("Aborted by user.")
            sys.exit(1)
        logger.info(f"{what} {targets}")

    mountpoint = prepare_destination(dest, dry_run)

    if mode == SQUEEZE_MOVE:
        move_dups(sources, dest, dry_run, mountpoint)
        return

    sticky = None  # 'a' or 'b' once the user picks "always first/last"
    copied = 0
    skipped = 0
    processed = []

    for src in sources:
        processed.append(src)
        groups, singles = find_duplicate_groups(src)
        logger.info(f"Found {len(groups)} groups of duplicates and {len(singles)} non-duplicate entries in {src}")
        picks, group_skipped, sticky, quit_requested = choose_from_groups(groups, sticky)
        skipped += group_skipped
        chosen = [(kept, digest) for digest, _paths, kept in picks]

        if quit_requested:
            logger.info("Quit requested by user: copying only the duplicates chosen so far in this source.")
            singles = []

        # Fill in the md5 of singles that were never hashed (unique size), for the CSV.
        # Symlinks are recorded without one; unreadable files are left for rsync to report.
        to_hash = [i for i, (path, digest) in enumerate(singles)
                   if digest is None and os.path.isfile(path) and not os.path.islink(path)]
        if to_hash:
            logger.info(f"Hashing {len(to_hash)} non-duplicate files in {src} for the CSV record...")
            for i in to_hash:
                path = singles[i][0]
                singles[i] = (path, md5_file(path))

        if chosen or singles:
            copied += rsync_file_list(chosen, singles, src, dest, dry_run)

        if quit_requested:
            break

    verb = "Would copy" if dry_run else "Copied"
    logger.info(f"{verb} {copied} files, skipped {skipped} duplicate groups.")
    logger.info("--- Space used (du -sh) ---")
    spared = None
    for src in processed:
        target = squeeze_target(src, dest)
        src_size, target_size = du_bytes(src), du_bytes(target)
        logger.info(f"  {human_size(src_size):>6}  {src}")
        logger.info(f"  {human_size(target_size):>6}  {target}")
        if src_size is not None and target_size is not None:
            spared = (spared or 0) + src_size - target_size
    logger.info(f"squeeze_dups elapsed time: {format_elapsed(start_time)}, spared space: {human_size(spared)}")
    if not dry_run and copied:
        logger.info(f"*** Reminder: Please safely unmount '{mountpoint}' "
                    "before physically disconnecting the drive. ***")
