"""NUC loader: pah_tuning first (opt-in PAH_* patches), then the live station's own ~/pah_judge/sitecustomize.py
(/arms, /frame, /pose, /move routes). Use PYTHONPATH=<this dir>:$HOME/pah_judge so pah_judge's imports resolve."""
import os
import runpy
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))  # station_addons, for pah_tuning
try:
    import pah_tuning

    pah_tuning.install()
except Exception as e:  # never stop Dream's station over this
    print(f"pah_tuning: not installed: {e}", file=sys.stderr)
_judge = os.path.expanduser("~/pah_judge/sitecustomize.py")
if os.path.exists(_judge):
    runpy.run_path(_judge, run_name="pah_judge_sitecustomize")
