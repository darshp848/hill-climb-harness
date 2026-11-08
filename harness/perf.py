"""Thread caps for the match laptop (Apple Silicon MacBook Air: 4 performance + 4 efficiency cores, no fan).

The harness's numeric work is small arrays (a 640 x 360 mask, a 9-state model), where a BLAS/OpenMP thread pool only
adds wake-ups and, for OpenBLAS, idle threads that spin after each call. One thread per library keeps the cores free
for the guard, the fast loop and the HTTP threads, and keeps a fanless laptop cooler. It must run before numpy (or
OpenCV) is first imported; values already set in the environment win.
"""

from __future__ import annotations

import os
import sys

THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS",
               "NUMEXPR_NUM_THREADS")


def cap_threads(n: int = 1) -> dict[str, str]:
    """setdefault each thread variable to `n`; also caps OpenCV's pool if cv2 is already imported. Returns the
    values in force. Too late for a library already imported (numpy reads them at import), hence the warning flag."""
    for k in THREAD_VARS:
        os.environ.setdefault(k, str(n))
    cv2 = sys.modules.get("cv2")
    if cv2 is not None:
        cv2.setNumThreads(int(os.environ["OMP_NUM_THREADS"]))
    return {k: os.environ[k] for k in THREAD_VARS}


def cap_cv2() -> None:
    """Call after importing cv2 (harness.cable_end): OpenCV ignores the environment variables above."""
    cv2 = sys.modules.get("cv2")
    if cv2 is not None:
        cv2.setNumThreads(int(os.environ.get("OMP_NUM_THREADS", "1")))
