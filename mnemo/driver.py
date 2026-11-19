#!/usr/bin/env python3
"""mnemo's command line: parse the arguments, resolve the sources, and run the chosen mode.

Each mode lives in its own module of the mnemo package; shared helpers are in mnemo/__init__.py.
"""
import argparse
import os
import sys

# Run as a script, Python puts mnemo/ on sys.path, not the repo root. Replace it with the repo root,
# so the package imports below work and mnemo/*.py can't shadow other top-level modules.
sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from mnemo import logger  # noqa: E402
from mnemo.backup import backup  # noqa: E402
from mnemo.crowded import find_crowded  # noqa: E402
from mnemo.heavy import find_heavy  # noqa: E402
from mnemo.sed_names import check_sed_names_args, sed_names  # noqa: E402
from mnemo.squeeze_dups import SQUEEZE_MODE_NAMES, squeeze_dups  # noqa: E402
from mnemo.stt import ensure_stt_runtime, stt  # noqa: E402


def main(args=None):
    parser = argparse.ArgumentParser(description="Gradual backup script over rsync")

    # Mutually exclusive group for the main operational modes
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup", action="store_true", help="Run the gradual backup process")
    group.add_argument("--heavy", type=int, nargs='?', const=10, metavar='N',
                       help="Find the N heaviest files in the sources")
    group.add_argument("--crowded", type=int, nargs='?', const=10, metavar='N',
                       help="Find the N most crowded directories in the sources")
    group.add_argument("--squeeze-dups", action="store_true",
                       help="Keep one interactively chosen copy of each set of identical files: either move the "
                            "others out of the sources into the destination, or copy a squeezed tree there "
                            "(see --squeeze-mode)")
    group.add_argument("--stt", action="store_true",
                       help="Speech-to-text: write FILE.txt next to every .m4a/.aac/.amr FILE in the sources "
                            "(ignores --destination; settings via MNEMO_* env vars)")
    group.add_argument("--sed-names", action="store_true",
                       help="Rename every file and directory under the sources whose name contains --from, "
                            "replacing it with --to (ignores --destination; asks for confirmation)")

    parser.add_argument("--squeeze-mode", choices=list(SQUEEZE_MODE_NAMES),
                        help="With --squeeze-dups: 'discard' moves the discarded copies out of the sources into "
                             "the destination, 'merge' copies a squeezed tree there. Asked if omitted")
    parser.add_argument("--from", dest="sed_from", metavar="OLD",
                        help="With --sed-names: the literal text to replace in names (case-sensitive; "
                             "letters, digits, '.', '-' and '_' only)")
    parser.add_argument("--to", dest="sed_to", metavar="NEW",
                        help="With --sed-names: the literal text to put in its place (same characters)")
    parser.add_argument("--destination", default="/run/media/pbertoni/storage/pb-backup/",
                        help="Destination directory (USB mount expected)")
    parser.add_argument("--source", nargs='*', default=[], help="Directories to backup/analyze")
    parser.add_argument("--whitelist", help="Path to a file containing source directories")
    parser.add_argument("--exclude", nargs='*', default=[], help="Patterns to exclude")
    parser.add_argument("--blacklist", help="Path to a file containing exclusion patterns for rsync")
    parser.add_argument("--permissive", action="store_true",
                        help="Skip sufficient space checks and prompt for continuation")
    parser.add_argument("--delete", action="store_true", help="Enable rsync deletion mode")
    parser.add_argument("--also-repos", action="store_true", help="Do not automatically exclude git repositories")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable caching of scanned results (like git repositories)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print selected files to stdout instead of a temp file")
    parser.add_argument("--dry-run", action="store_true", help="Print actions and exit")

    parsed_args = parser.parse_args(args)
    if parsed_args.squeeze_mode and not parsed_args.squeeze_dups:
        parser.error("--squeeze-mode requires --squeeze-dups")
    if parsed_args.sed_names:
        problem = check_sed_names_args(parsed_args.sed_from, parsed_args.sed_to)
        if problem:
            parser.error(problem)
    elif parsed_args.sed_from is not None or parsed_args.sed_to is not None:
        parser.error("--from and --to require --sed-names")

    # Must happen before anything is logged, as it may re-exec this script under the stt venv
    if parsed_args.stt and not parsed_args.dry_run:
        ensure_stt_runtime(os.path.abspath(__file__))

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
        logger.error("No source directories provided. "
                     "You must specify --source or provide a non-empty --whitelist file.")
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

    if parsed_args.stt:
        stt(sources, parsed_args.dry_run)
        sys.exit(0)

    if parsed_args.sed_names:
        sed_names(sources, parsed_args.sed_from, parsed_args.sed_to, parsed_args.dry_run)
        sys.exit(0)

    if parsed_args.squeeze_dups:
        squeeze_dups(parsed_args.destination, sources, parsed_args.dry_run, parsed_args.squeeze_mode)
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
