"""Shared plumbing for mnemo: repo paths, the tee logger, and small generic helpers.

driver.py holds the modes (--backup, --squeeze-dups, --stt, ...) and imports from here.
"""
import os
import sys
import subprocess
import logging
import time
import math
import hashlib

# Set up global directories
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
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


def get_env(name, default, cast=str):
    """Read the MNEMO_<name> environment variable, falling back to `default` when unset or empty."""
    raw = os.environ.get(f"MNEMO_{name}", "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.error(f"Invalid value for MNEMO_{name}: {raw!r}")
        sys.exit(1)


def parse_bool(raw):
    """Parse a yes/no string (1/0, true/false, yes/no, on/off); raise ValueError otherwise."""
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ValueError(raw)


def ask(prompt, accept):
    """Prompt until `accept(answer)` returns something other than None, and return that.

    The answer is passed stripped and lowercased. Anything `accept` rejects prints a notice
    and asks again, so a typo can never be taken as a choice. Returns None on Ctrl-C or EOF.
    """
    while True:
        try:
            raw = input(prompt)
        except (KeyboardInterrupt, EOFError):
            print()
            return None
        value = accept(raw.strip().lower())
        if value is not None:
            return value
        print(f"Invalid answer {raw.strip()!r}, please try again.")


def ask_yes_no(prompt):
    """Ask a [y/N] question: y/yes is True, n/no or a bare Enter (the default) is False.

    Any other answer asks again. Returns None on Ctrl-C or EOF, which callers treat as no.
    """
    return ask(prompt, {"y": True, "yes": True, "n": False, "no": False, "": False}.get)


def ask_number(prompt, count, letters=()):
    """Ask for an integer in [1, count] or one of `letters`; returns the int or the letter (None on Ctrl-C/EOF)."""
    def accept(answer):
        if answer in letters:
            return answer
        # isdecimal(), not isdigit(): int() rejects digits like '²' that isdigit() accepts
        if answer.isdecimal() and 1 <= int(answer) <= count:
            return int(answer)
        return None
    return ask(prompt, accept)


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


def is_within(path, parent):
    """True if `path` is `parent` or lies under it, after resolving symlinks and '..'."""
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return os.path.commonpath([path, parent]) == parent


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


def du_bytes(path):
    """Disk usage of `path` in bytes (du -s), or None if it can't be measured."""
    if not os.path.exists(path):
        return None
    result = subprocess.run(["du", "-s", "--block-size=1", path], capture_output=True, text=True)
    try:
        return int(result.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def human_size(n):
    """Format a byte count like du -h does (powers of 1024, rounded up, one decimal below 10)."""
    if n is None:
        return "n/a"
    sign = "-" if n < 0 else ""
    value = abs(n)
    if value < 1024:
        return f"{sign}{value}"
    for unit in "KMGTPE":
        value /= 1024
        if value < 10:
            value = math.ceil(value * 10) / 10
            if value < 10:
                return f"{sign}{value:.1f}{unit}"
        value = math.ceil(value)
        if value < 1024 or unit == "E":
            return f"{sign}{value}{unit}"


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
