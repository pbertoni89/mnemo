"""--crowded: list the directories with the most files under each source."""
import subprocess
import time

from mnemo import logger, format_elapsed


def find_crowded(limit=10, directory="."):
    """Prints the first `limit` directories with the most files under `directory`."""
    start_time = time.time()
    logger.info(f"--- Top {limit} most crowded directories in {directory} ---")
    cmd = (f"find {subprocess.list2cmdline([directory])} -type d"
           " -exec sh -c 'echo \"$(find \"$1\" -maxdepth 1 -type f 2>/dev/null | wc -l) $1\"' _ {} \\;"
           f" 2>/dev/null | sort -rn | head -n {limit}")
    subprocess.run(cmd, shell=True)
    logger.info(f"find_crowded elapsed time: {format_elapsed(start_time)}")
