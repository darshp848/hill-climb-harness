exec((__import__('pathlib').Path(__file__).parent/'collect.py').read_text().split('refs=')[0])
refs=[x.strip() for x in git('branch','-r').splitlines() if '->' not in x]
report=[]
for ref in refs:
    tag=ref.removeprefix('origin/').replace('/','__')
    for base in ['main','cloud-sims']:
        (raw/f'head_diff_{base}_to_{tag}.txt').write_text(clean(git('diff',f'origin/{base}',ref,'--','harness/','plans/','tests/','sim_station.py','sim_scenes/','station_addons/','README.md','TELEOP_PLAN.md','*.md')),encoding='utf-8')
    changed=git('diff','--name-only','origin/main',ref,'--','harness/','plans/','sim_station.py','sim_scenes/','station_addons/').splitlines()
    unique=git('log','--oneline',ref,'--not','origin/main','origin/cloud-sims')
    report.append({'branch':ref,'runtime_files_vs_main':changed,'commits_in_neither_candidate':unique})
(raw/'branch_runtime_summary.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
