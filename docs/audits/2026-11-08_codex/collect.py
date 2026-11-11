from pathlib import Path
import subprocess, re, json
root=Path(__file__).resolve().parent.parent
raw=root/'out/raw'
def git(*args):
    return subprocess.check_output(['git','-C',str(root/'harness'),*args],text=True,encoding='utf-8',errors='replace')
def clean(s):
    s=re.sub(r'(?i)(sk-[a-z0-9_-]{16,}|sk-ant-[a-z0-9_-]+)', '[REDACTED]',s)
    s=re.sub(r'(?im)^([^\n]*(?:api_key|access_token|secret_key)\s*=\s*)[\x22\x27][^\n\x22\x27]{16,}[\x22\x27]',r'\1[REDACTED]',s)
    return s
refs=[x.strip() for x in git('branch','-r').splitlines() if '->' not in x]
meta=[]
for ref in refs:
    name=ref.removeprefix('origin/'); tag=name.replace('/','__')
    files=git('ls-tree','-r','--name-only',ref).splitlines()
    docs=[p for p in files if p.endswith('.md') and (p in ('README.md','TELEOP_PLAN.md') or re.search('STRATEG|FINAL_HARNESS|HYBRID|DECISIONS|HOW_THE|DESIGN|OPERATOR',p,re.I))]
    doc=''
    for p in docs:
        doc+=f'\n===== {ref}:{p} =====\n'+git('show',f'{ref}:{p}')
    (raw/f'docs_{tag}.txt').write_text(clean(doc),encoding='utf-8')
    for base in ['main','cloud-sims']:
        diff=git('diff',f'origin/{base}...{ref}','--','*.py','*.yaml','*.toml','*.md',':!*.env*',':!*lock*')
        (raw/f'diff_{base}_to_{tag}.txt').write_text(clean(diff),encoding='utf-8')
    meta.append({'branch':name,'head':git('rev-parse','--short',ref).strip(),'diff_main':git('diff','--stat','origin/main',ref),'log':git('log','--oneline','-12',ref)})
(raw/'branches.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
for repo in ['harness','harness-cloud-sims','task_simulation']:
    files=subprocess.check_output(['git','-C',str(root/repo),'ls-files'],text=True).splitlines()
    (raw/f'inventory_{repo}.txt').write_text('\n'.join(files),encoding='utf-8')
    status=subprocess.check_output(['git','-C',str(root/repo),'status','--porcelain'],text=True)
    (raw/f'status_before_{repo}.txt').write_text(status,encoding='utf-8')
for p in ['docs/LAST_NIGHT_2026-11-08.md']:
    (raw/Path(p).name).write_text((root/'harness'/p).read_text(encoding='utf-8'),encoding='utf-8')
pdf='docs/Hill_Climb_Harness_Explained.pdf'
(raw/'Hill_Climb_Harness_Explained.pdf').write_bytes(subprocess.check_output(['git','-C',str(root/'harness'),'show',f'origin/docs/how-it-works:{pdf}']))
try:
    import pymupdf
    d=pymupdf.open(raw/'Hill_Climb_Harness_Explained.pdf')
    (raw/'explained_pdf.txt').write_text('\n'.join(p.get_text() for p in d),encoding='utf-8')
except ImportError:
    from pypdf import PdfReader
    (raw/'explained_pdf.txt').write_text('\n'.join(p.extract_text() for p in PdfReader(raw/'Hill_Climb_Harness_Explained.pdf').pages),encoding='utf-8')
print('Collected branch docs, diffs, inventory, initial status, PDF text.')
