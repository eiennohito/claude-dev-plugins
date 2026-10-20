#!/usr/bin/env python3
"""`--diagnose`: print what the SessionStart probe detects and would inject
(used by /probe-tools-configure). Without it, print the hook payload."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import probe  # noqa: E402

if "--diagnose" in sys.argv[1:]:
    probe.diagnose()
else:
    probe.main()
