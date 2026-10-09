"""Clone gerber2gcode at a pinned commit into vendor/ and apply patches/*.patch.

gerber2gcode has no open-source licence, so it is fetched, not redistributed.
Requires git.  Run:  uv run fetch_gerber2gcode.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

URL = "https://github.com/gcappaert/gerber2gcode"
COMMIT = "73f72cd540d2c63b7732eb1850b8cabcf25364ac"
ROOT = Path(__file__).resolve().parent
DEST = ROOT / "vendor" / "gerber2gcode"


def git(*args, cwd=None):
    # autocrlf off: patches are LF; a CRLF checkout on Windows would reject them
    subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True)


def main():
    if shutil.which("git") is None:
        sys.exit("git not found — install it from https://git-scm.com")
    if DEST.exists():
        if "--force" not in sys.argv:
            sys.exit(f"{DEST} exists. Re-run with --force to re-fetch.")
        shutil.rmtree(DEST)
    DEST.parent.mkdir(exist_ok=True)
    git("clone", "-q", URL, str(DEST))
    git("checkout", "-q", COMMIT, cwd=DEST)
    for p in sorted((ROOT / "patches").glob("*.patch")):
        git("apply", str(p), cwd=DEST)
        print("applied", p.name)
    print("gerber2gcode ready:", DEST)


if __name__ == "__main__":
    main()
