"""--heavy: list the heaviest files under each source."""
import subprocess
import time

from mnemo import logger, format_elapsed


def find_heavy(limit=10, directory="."):
    """Prints the first `limit` heavier files under `directory`."""
    start_time = time.time()
    logger.info(f"--- Top {limit} heaviest files in {directory} ---")
    cmd = (f"find {subprocess.list2cmdline([directory])} -type f -exec du -h {{}} + 2>/dev/null"
           f" | sort -rh | head -n {limit}")
    subprocess.run(cmd, shell=True)
    logger.info(f"find_heavy elapsed time: {format_elapsed(start_time)}")
