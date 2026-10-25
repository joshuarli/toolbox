import functools
import re
import subprocess
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "generated"


@functools.cache
def ffmpeg_names(*args: str) -> frozenset[str]:
    """Names listed by `ffmpeg -encoders` / `ffmpeg -filters`; empty when ffmpeg is missing."""
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", *args], capture_output=True, text=True, timeout=30)
    except OSError:
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    names = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"^ [.A-Z]+ (\S+)", line)
        if match:
            names.add(match.group(1))
    return frozenset(names)
