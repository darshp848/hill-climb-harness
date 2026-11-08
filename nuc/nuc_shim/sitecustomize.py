import runpy, sys
sys.path.insert(0, "/home/pah/hill-climb-harness-tuning/station_addons")
try:
    import pah_tuning
    pah_tuning.install()
except Exception as e:
    print(f"pah_tuning: not installed: {e}", file=sys.stderr)
runpy.run_path("/home/pah/pah_judge/sitecustomize.py", run_name="pah_judge_sitecustomize")
