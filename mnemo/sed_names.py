"""--sed-names: replace a literal substring in the names of every file and directory under the sources.

Only basenames change, never the path above them, and never the source directories themselves.
"""
import csv
import os
import re
import sys
import time

from mnemo import LOGS_DIR, DATE_SUFFIX, logger, format_elapsed, is_within, ask_yes_no

# --from and --to are literal strings. Restricting them to these characters keeps them free of '/',
# shell and regex specials, so a rename can only ever change a name, never move an entry elsewhere.
SED_NAMES_ALLOWED = re.compile(r"[A-Za-z0-9._-]+")


def check_sed_names_args(old, new):
    """Return an error message if --from/--to are unusable, else None."""
    for flag, value in (("--from", old), ("--to", new)):
        if value is None:
            return f"--sed-names requires {flag}"
        if not SED_NAMES_ALLOWED.fullmatch(value):
            return f"{flag} {value!r} must be non-empty and contain only letters, digits, '.', '-' and '_'"
    if old == new:
        return "--from and --to are the same: nothing to rename"
    return None


def plan_renames(src, old, new):
    """List the renames under `src` as [(parent, name, new_name, kind)], deepest entries first.

    `kind` is 'dir', 'file' or 'link'.

    Deepest first means children are renamed while their parent still has its old name, so every
    path in the plan stays valid while it is applied. Symlinks are renamed, never followed.
    """
    plan = []
    for root, dirs, files in os.walk(src, topdown=False):
        for names, kind in ((files, "file"), (dirs, "dir")):
            for name in sorted(names):
                if old in name:
                    # os.walk lists a symlink to a directory under dirs; either way only the link is renamed
                    if os.path.islink(os.path.join(root, name)):
                        kind = "link"
                    plan.append((root, name, name.replace(old, new), kind))
    return plan


def find_conflicts(plan):
    """Return the set of plan indexes whose new name is already taken, so they must be skipped.

    A new name is taken if something else already exists there, or if an earlier entry of the
    plan claims it in the same directory. A target that is the entry itself (a case-only rename
    on a case-insensitive filesystem) is not a conflict. Nothing is ever overwritten.
    """
    conflicts = set()
    claimed = set()
    for i, (parent, name, new_name, _kind) in enumerate(plan):
        old_path, new_path = os.path.join(parent, name), os.path.join(parent, new_name)
        if new_name in (".", ".."):
            conflicts.add(i)
            continue
        taken = os.path.lexists(new_path) and not os.path.samestat(os.lstat(old_path), os.lstat(new_path))
        if taken or new_path in claimed:
            conflicts.add(i)
        else:
            claimed.add(new_path)
    return conflicts


def sed_names(sources, old, new, dry_run):
    """Rename every file and directory under the sources whose name contains `old`, replacing it with `new`.

    Every occurrence in a name is replaced, case-sensitively. The full list is shown first, then
    the user must confirm (dry-run stops after the list). Renames that would overwrite something
    are skipped. Each rename done is recorded in logs/mnemo_sed_names_<date>.csv.
    """
    start_time = time.time()

    # A source inside another would be planned twice, and its second pass would find paths already renamed
    for src in sources:
        for other in sources:
            if src != other and is_within(src, other):
                logger.error(f"Source {src} is inside source {other}. Pass only the outer one. Halting.")
                sys.exit(1)

    logger.info(f"Looking for names containing {old!r} (to become {new!r}) in {', '.join(sources)}...")
    plan = [entry for src in sources for entry in plan_renames(src, old, new)]
    conflicts = find_conflicts(plan)
    todo = [entry for i, entry in enumerate(plan) if i not in conflicts]

    if not plan:
        logger.info(f"No file or directory name contains {old!r}. Nothing to do.")
        return

    # Shown in path order, parents before children, which reads better than the deepest-first apply order
    for i in sorted(range(len(plan)), key=lambda i: os.path.join(plan[i][0], plan[i][1])):
        parent, name, new_name, kind = plan[i]
        note = "  [SKIPPED: target already exists]" if i in conflicts else ""
        print(f"  {kind:<4}  {os.path.join(parent, name)}  ->  {new_name}{note}")

    n_dirs = sum(1 for entry in todo if entry[3] == "dir")
    n_links = sum(1 for entry in todo if entry[3] == "link")
    n_files = len(todo) - n_dirs - n_links
    summary = f"{len(todo)} entries to rename: {n_files} files and {n_dirs} directories"
    if n_links:
        summary += f" and {n_links} symlinks"
    if conflicts:
        summary += f" ({len(conflicts)} more skipped, as their new name is already taken)"
    logger.info(summary)

    if not todo:
        return
    if dry_run:
        logger.info("[DRY-RUN] Nothing renamed.")
        return
    if not ask_yes_no(f"Rename these {len(todo)} entries? [y/N]: "):
        logger.info("Aborted by user.")
        sys.exit(1)

    csv_path = os.path.join(LOGS_DIR, f"mnemo_sed_names_{DATE_SUFFIX}.csv")
    renamed = failed = 0
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_path", "new_path"])
        for parent, name, new_name, _kind in todo:
            old_path, new_path = os.path.join(parent, name), os.path.join(parent, new_name)
            # Checked again right before renaming, as os.rename silently replaces an existing file
            if os.path.lexists(new_path) and not os.path.samestat(os.lstat(old_path), os.lstat(new_path)):
                logger.warning(f"Skipped {old_path}: {new_path} appeared meanwhile")
                failed += 1
                continue
            try:
                os.rename(old_path, new_path)
            except OSError as e:
                logger.warning(f"Cannot rename {old_path}: {e}")
                failed += 1
                continue
            writer.writerow([old_path, new_path])
            f.flush()
            renamed += 1
            logger.info(f"Renamed: {old_path} -> {new_name}")

    logger.info(f"Renamed {renamed} entries, {failed} failed. Renames recorded in: {csv_path}")
    logger.info(f"sed_names elapsed time: {format_elapsed(start_time)}")
    if failed:
        sys.exit(1)
