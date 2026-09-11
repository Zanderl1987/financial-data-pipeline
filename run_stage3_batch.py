#!/usr/bin/env python3
"""Detached launcher for stage3 batch — handles own logging, no shell redirection."""
import subprocess
import sys
import os
from datetime import datetime

LOG_DIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "opencode")
os.makedirs(LOG_DIR, exist_ok=True)

OUT_LOG = os.path.join(LOG_DIR, "stage3_remaining17.out.log")
ERR_LOG = os.path.join(LOG_DIR, "stage3_remaining17.err.log")

# Reinitialize logs
with open(OUT_LOG, "w", encoding="utf-8") as f:
    f.write(f"[launcher {datetime.now().strftime('%H:%M:%S')}] detached run\n")
with open(ERR_LOG, "w", encoding="utf-8") as f:
    pass

cmd = [
    sys.executable, "-u",
    "strategies\\stage3.py",
    "--only-slugs", "remaining_17.txt",
    "--n-perm", "100",
    "--workers", "4"
]

print(f"Launching: {' '.join(cmd)}")
print(f"Logs: {OUT_LOG}, {ERR_LOG}")

with open(OUT_LOG, "a", encoding="utf-8") as out_f, \
     open(ERR_LOG, "a", encoding="utf-8") as err_f:
    proc = subprocess.Popen(
        cmd,
        cwd=os.path.dirname(__file__),
        stdout=out_f,
        stderr=err_f,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    )
    print(f"Launched PID {proc.pid}")
    # Don't wait — fully detached