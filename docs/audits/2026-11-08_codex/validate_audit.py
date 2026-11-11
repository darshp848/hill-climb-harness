from pathlib import Path
import json, subprocess, hashlib, re, os
root=Path(__file__).resolve().parent.parent
raw=root/'out/raw'
env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'}
def git(*args):return subprocess.check_output(['git','-C',str(root/'harness'),*args],text=True,encoding='utf-8',errors='replace',env=env)
refs=[s.strip() for s in git('branch','-r').splitlines() if '->' not in s]
manifest=[]
seen={}
for ref in refs:
    files=git('ls-tree','-r','--name-only',ref).splitlines()
    selected=[p for p in files if p.endswith('.md') and (p in ('README.md','TELEOP_PLAN.md','GOAL.md','HARNESS_GUIDE.md','HILL_CLIMB_LOG.md','STATE_SPACE.md') or re.search('STRATEG|FINAL_HARNESS|DECISIONS|MATCH_DESIGN|STRESS_FINDINGS',p,re.I))]
    text=''
    for p in selected:
        s=git('show',f'{ref}:{p}')
        text+=f'\n===== {ref}:{p} =====\n'+s
        h=hashlib.sha256(s.encode()).hexdigest()
        manifest.append({'branch':ref,'file':p,'sha256':h,'same_as':seen.get(h)})
        seen.setdefault(h,f'{ref}:{p}')
    (raw/f'strategy_{ref.removeprefix("origin/").replace("/","__")}.txt').write_text(text,encoding='utf-8')
(raw/'documentation_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
findings=json.loads((root/'out/findings.json').read_text(encoding='utf-8'))
md=(root/'out/AUDIT_FINDINGS.md').read_text(encoding='utf-8')
assert len(refs)==14 and len(findings)==26
ids=set()
for f in findings:
    assert set(f)=={'id','severity','branches','file','line','title','failure_scenario','fix','confidence'},f['id']
    assert f['id'] not in ids
    ids.add(f['id'])
    assert f['severity'] in {'Critical','High','Medium','Low'}
    lines=(root/f['file']).read_text(encoding='utf-8').splitlines()
    assert 1<=f['line']<=len(lines)
    assert f'<a id="{f["id"].lower()}"></a>' in md
    assert f"`{f['file']}:{f['line']}`" in md
for ref in refs:
    branch=ref.removeprefix('origin/')
    assert f'### {branch} (' in md
for repo in ['harness','harness-cloud-sims','task_simulation']:
    status=subprocess.check_output(['git','-C',str(root/repo),'status','--porcelain'],text=True,env=env)
    (raw/f'status_after_{repo}.txt').write_text(status,encoding='utf-8')
    assert status==(raw/f'status_before_{repo}.txt').read_text(encoding='utf-8'),repo
    assert not status,repo
assert git('rev-parse','--short','HEAD').strip()=='b1f6e4b'
cloud_head=subprocess.check_output(['git','-C',str(root/'harness-cloud-sims'),'rev-parse','--short','HEAD'],text=True,env=env).strip()
assert cloud_head=='c6a3255'
for name in ['pytest_main_retry.txt','pytest_main_cli_recheck.txt','pytest_cloud-sims.txt','pytest_cloud_subset.txt','pytest_cloud_cli_recheck.txt','pytest_cloud_timing_recheck.txt','pytest_hedge_fake_only.txt','probes_main.json','probes_cloud.json','more_probes.json','source_evidence.txt','explained_pdf.txt']:
    assert (raw/name).stat().st_size>0,name
summary={'findings':len(findings),'branches':len(refs),'documentation_files':len(manifest),'unique_strategy_documents':len(seen),'all_repositories_clean':True,'candidate_heads':{'main':'b1f6e4b','cloud-sims':cloud_head},'markdown_bytes':(root/'out/AUDIT_FINDINGS.md').stat().st_size,'json_bytes':(root/'out/findings.json').stat().st_size}
(raw/'audit_validation.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps(summary,indent=2))
