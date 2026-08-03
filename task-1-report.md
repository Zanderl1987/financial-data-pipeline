# Task 1 Fix Report

## Problem
Dash 4.4.1 import was failing with `NotImplementedError: Cannot` from `comm` package version 0.1.2.

Root cause: dash 4.4.1's `_jupyter.py` calls `create_comm()` unconditionally at import time. The environment's `comm` package is version 0.1.2 (anaconda-bundled), which raises `NotImplementedError` when there's no live Jupyter kernel registered. Newer `comm` versions (0.2.x) handle this gracefully outside Jupyter.

## Solution
Upgraded `comm` from 0.1.2 to 0.2.3 via `pip install -U comm`.

Added explicit floor pin to `requirements.txt`:
```
comm>=0.2.0                   # dash transitive dep -- <0.2.0 raises NotImplementedError on import outside Jupyter
```

## Verification
Fresh-process verification (new Python subprocess, no cached state):
```
C:\ProgramData\anaconda3\python.exe -c "import dash; from dash import dcc, html, Input, Output; print('ok', dash.__version__)"
ok 4.4.1
```

Result: PASS ✓
