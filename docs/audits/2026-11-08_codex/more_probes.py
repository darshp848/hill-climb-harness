import sys, json, time, threading, importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
sys.dont_write_bytecode=True
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root/'harness-cloud-sims'))
from harness.plan import load_plan, parse_plan, Stage
from harness.verifiers import MonitorVerifier
from harness.runner import Runner
out={}
# A losing hedged monitor call can contain the only guard YES.
p=parse_plan({'claude':{'hedge':2},'stages':[{'name':'ball_1','prompt':'ball','success':'done?'}]})
lock=threading.Lock(); calls=[0]; finished=[]
def transport(_):
    with lock:k=calls[0];calls[0]+=1
    time.sleep(.02 if k==0 else .08)
    finished.append(k)
    text=json.dumps({'stage_done':False,'status':'ok','confidence':1,'guard':k==1,'reason':'safe' if k==0 else 'illegal held'})
    return {'content':[{'type':'text','text':text}]}
m=MonitorVerifier(p,transport=transport)
m._run_async(m._gen,1,{},0)
time.sleep(.12)
out['hedged_guard_loss']={'finished_copies':finished,'winning_guard':m._result.guard,'guard_alert':m.take_guard_alert(),'guard_answers':m.guard_answers}
# A transient restart does not invalidate the monitor generation.
class C:
    t=0
    def now(self):return self.t
    def sleep(self,s):self.t+=s
class Log:
    def event(self,*a,**kw):pass
class Station:
    dry_run=False
    running=False
    def status(self):return {'state':'running' if self.running else 'error','last_error':{'type':'DreamServerError','message':'DA-6000 GATEWAY_FAULT'}}
    def start(self,*a):self.running=True
    def wait_for_state(self,*a):return self.status()
c=C();st=Station();r=Runner(p,st,NS(),{'monitor':m},Log(),clock=c.now,sleep=c.sleep);r._prompt='ball';before=m._gen;r._poll_health()
out['restart_generation']={'before':before,'after':m._gen,'old_result_still_available':m._result is not None}
# Sim referee functions need only synthetic bodies for these diagnostics, not MuJoCo.
from sim_scenes import hill_climb_table as h
ref=object.__new__(h.HillClimbReferee)
ref.id={'golf_0':0};data=NS(xpos=[[h.BIN_POS[0],h.BIN_POS[1],h.TABLE_Z-1]])
out['underground_ball_in_bin']=ref._in_bin(data,'golf_')
ref.groups={2:{1}};ref._model=NS(body_geomadr={1:0},body_geomnum={1:1});ref._half_height=lambda *_:.03
out['single_block_on_nonblock_support_height_m']=ref._tower_m(NS(geom_xpos=[[0,0,h.TABLE_Z+.2]]))
# New push stage is not implemented by the oracle.
from experiments.sim_match import OracleVerifier
oracle=OracleVerifier('NEVER_CALLED')
snap={'referee':{'tower_in':6,'unplugged':1,'golf_in_bin':0,'eight_in_bin':0},'bodies':{},'joints':{'left':[0]*6+[1],'right':[0]*6+[1]},'tcp':{'left':[0,0,0],'right':[0,0,0]}}
try:oracle.check(Stage('ball_push_white','push','white off?'),{},1,snapshot=snap)
except Exception as e:out['push_white_oracle_exception']=type(e).__name__+': '+str(e)
plan=load_plan(root/'harness-cloud-sims/plans/hybrid_v4.yaml')
out['hybrid_ft_defaults']={'checkpoint':plan.checkpoint,'rewrite_prompts':plan.rewrite_prompts,'prompt_technique':plan.prompt_technique,'extra_push_stage':any(s.name=='ball_push_white' for s in plan.stages)}
# Constructing the transport is safe with an explicit stub; patch subprocess.run, so no process is launched.
from harness.verifiers import claude_cli_transport
import subprocess
captured={};orig=subprocess.run
def fake_run(cmd,**kw):captured['command']=cmd;captured['stdin']=kw['input'];return NS(stdout=json.dumps({'type':'result','subtype':'success','result':'ok'}),stderr='',returncode=0)
subprocess.run=fake_run
try:claude_cli_transport(claude_bin='OFFLINE_STUB')({'model':'m','system':'CRITICAL_PLAN_SYSTEM_SENTINEL','messages':[{'role':'user','content':[{'type':'text','text':'q'}]}]})
finally:subprocess.run=orig
out['subscription_system_dropped']={'sentinel_in_command':any('CRITICAL_PLAN_SYSTEM_SENTINEL' in x for x in captured['command']),'sentinel_in_stdin':'CRITICAL_PLAN_SYSTEM_SENTINEL' in captured['stdin']}
from harness.cli import _parser, match_problems
args=_parser().parse_args(['plans/hill_climb_v4.yaml','--live','--match','--verifier','time','--skip','unplug','--skip','stack_','--skip','tower_pass'])
remaining=load_plan(root/'harness-cloud-sims/plans/hill_climb_v4.yaml')
remaining.stages=[s for s in remaining.stages if not any(s.name.startswith(x) for x in args.skip)]
out['match_accepts_time_and_removed_gates']={'problems':match_problems(args,remaining),'remaining_stages':[s.name for s in remaining.stages]}
print(json.dumps(out,indent=2))
