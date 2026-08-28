#!/usr/bin/env python3
"""A stand-in ffprobe used by tests: reads a canned-response plan from the
FAKE_FFPROBE_PLAN env var (a JSON file) and returns the entry matching the
-title/-playlist value on the command line, so disc.py's real subprocess +
JSON-parsing code paths get exercised without needing a real disc or a
real ffprobe build with libdvdread/libbluray."""

import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    plan = json.loads(open(os.environ["FAKE_FFPROBE_PLAN"], encoding="utf-8").read())

    key = None
    if "-title" in argv:
        key = f"title:{argv[argv.index('-title') + 1]}"
    elif "-playlist" in argv:
        key = f"playlist:{argv[argv.index('-playlist') + 1]}"

    entry = plan.get(key, plan.get("default", {"exit": 1, "stdout": "", "stderr": ""}))
    sys.stdout.write(entry.get("stdout", ""))
    sys.stderr.write(entry.get("stderr", ""))
    return entry.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
