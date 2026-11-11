import os, sys, socket
from pathlib import Path
root=Path(__file__).resolve().parent.parent
repo=sys.argv[1]
os.environ['PYTHONDONTWRITEBYTECODE']='1'
sys.dont_write_bytecode=True
for key in list(os.environ):
    if any(s in key.upper() for s in ['API_KEY','ACCESS_TOKEN','SECRET','AUTH_TOKEN']):
        del os.environ[key]
os.environ['HARNESS_CLAUDE_BACKEND']='api'
os.environ['TEMP']=os.environ['TMP']=str(root/'out'/'tmp')
Path(os.environ['TEMP']).mkdir(exist_ok=True)
real_connect=socket.socket.connect
real_connect_ex=socket.socket.connect_ex
def allowed(address):
    return isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost') and int(address[1])>=10000
def connect(self,address):
    if not allowed(address):
        raise RuntimeError('AUDIT BLOCKED network connection outside ephemeral local test servers')
    return real_connect(self,address)
def connect_ex(self,address):
    if not allowed(address):
        raise RuntimeError('AUDIT BLOCKED network connection outside ephemeral local test servers')
    return real_connect_ex(self,address)
socket.socket.connect=connect
socket.socket.connect_ex=connect_ex
def audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes)):
        p=Path(os.fsdecode(args[0])).absolute()
        if '.dreamlabs' in p.parts or p.name=='.env':
            raise RuntimeError('AUDIT BLOCKED credential file read')
        mode=args[1]
        flags=args[2]
        write=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT))
        if write and not p.is_relative_to(root/'out'):
            raise RuntimeError('AUDIT BLOCKED write outside out')
    if event=='subprocess.Popen':
        exe=str(args[0]).lower()
        if Path(exe).name in ['dreamcli','dreamcli.exe','gcloud','gcloud.cmd','ssh','claude','claude.exe','claude.cmd'] and not Path(exe).absolute().is_relative_to(root/'out'):
            raise RuntimeError('AUDIT BLOCKED external service CLI')
sys.addaudithook(audit)
os.chdir(root/'out')
sys.path.insert(0,str(root/repo))
import pytest
extra=sys.argv[2:]
target=str(root/repo/'tests')
if '--audit-target' in extra:
    pos=extra.index('--audit-target')
    target=extra[pos+1]
    extra=extra[:pos]+extra[pos+2:]
raise SystemExit(pytest.main([target,'-ra','-q','-p','no:cacheprovider','--basetemp',str(root/'out'/'tmp'/repo),*extra]))
