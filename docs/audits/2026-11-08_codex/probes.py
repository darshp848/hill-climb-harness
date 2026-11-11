import sys, json, io
from pathlib import Path
sys.dont_write_bytecode=True
root=Path(__file__).resolve().parent.parent
repo=sys.argv[1]
sys.path.insert(0,str(root/repo))
from harness.plan import parse_plan, load_plan
from harness.runner import Runner
from harness.verifiers import MonitorVerdict, MonitorVerifier
class Clock:
    t=0.0
    def now(self): return self.t
    def sleep(self,s): self.t+=s
class Station:
    dry_run=False
    def __init__(self): self.stops=0; self.starts=0; self.running=False
    def status(self): return {'state':'running' if self.running else 'idle','arms':{'left':True,'right':True},'cameras':{'scene':0}}
    def start(self,*a): self.starts+=1; self.running=True; return self.status()
    def set_prompt(self,*a): return self.status()
    def stop(self): self.stops+=1; self.running=False; return self.status()
    def wait_for_state(self,*a): return self.status()
class Frames:
    n=0
    def latest(self):
        self.n+=1
        return {'scene':b'\xff\xd8\xff\xe0fake'+str(self.n).encode()+b'\xff\xd9'}
class Log:
    def __init__(self): self.events=[]
    def event(self,name,**kw): self.events.append((name,kw))
    def update_manifest(self,**kw): pass
    def write_summary(self,*a): pass
    def save_frames(self,*a): return []
    def segment(self,**kw): pass
class Sequence:
    def __init__(self,seq): self.seq=iter(seq); self.calls=0
    def check(self,*a):
        self.calls+=1
        done=next(self.seq,None)
        return MonitorVerdict(done,.99,str(done),status='ok',frames_sig=str(self.calls))
    check_now=check
def make(confirm=2,**stage):
    return parse_plan({'task':'probe','confirm':confirm,'check_every_s':1,'time_budget_s':60,'verifier':'monitor','stages':[dict(name='a',prompt='do a',success='done?',timeout_s=30,min_run_s=.01,on_fail='skip',**stage)]})
def runner(plan,v,log=None,st=None):
    c=Clock(); st=st or Station(); log=log or Log()
    r=Runner(plan,st,Frames(),{'monitor':v},log,clock=c.now,sleep=c.sleep)
    return r,c,st,log
out={}
p=make(confirm=3); v=Sequence([True,False,True]); r,c,st,l=runner(p,v); st.running=True
a=r._watch(p.stages[0],v,1)
out['confirm_3_true_false_true']={'ok':a.ok,'checks':v.calls,'seconds':c.t}
p=make(skip_if_done=True);v=Sequence([True]);r,c,st,l=runner(p,v)
a=r._run_stage(p.stages[0],True)
out['entry_confirm_2']={'ok':a.ok,'checks':v.calls,'starts':st.starts,'seconds':c.t}
p=make(skip_if_done=True);c=Clock();st=Station();st.running=True;l=Log();sent=[]
st.set_prompt=lambda *args:sent.append(c.t) or st.status()
class SlowEntry:
    def check_now(self,*a):c.sleep(61);return MonitorVerdict(False,.99,'not done',status='ok')
    def check(self,*a):return MonitorVerdict(None,0,'pending')
r=Runner(p,st,Frames(),{'monitor':SlowEntry()},l,clock=c.now,sleep=c.sleep)
try:r._run_stage(p.stages[0],False)
except Exception as e:out['late_entry_prompt']={'budget':p.time_budget_s,'prompt_sent_at':sent,'exception':type(e).__name__,'clock':c.t}
class BrokenLog(Log):
    def save_frames(self,*a): raise OSError('simulated disk full')
p=make();r,c,st,l=runner(p,Sequence([True]),BrokenLog())
try: r.run()
except Exception as e: out['disk_error']={'exception':type(e).__name__,'stops':st.stops,'station_running':st.running}
class StopLog(Log):
    def event(self,name,**kw):
        if name=='stop': raise OSError('simulated log write failure')
p=make();r,c,st,l=runner(p,Sequence([True]),StopLog());st.running=True
try:r._safe_stop('deadline')
except Exception as e:out['stop_log_error']={'exception':type(e).__name__,'stops':st.stops,'station_running':st.running}
class RefusingStation(Station):
    def status(self):return dict(super().status(),state='error' if self.starts else 'idle',last_error={'type':'DreamBadRequest','message':'DA-4000 unsafe instruction'})
p=parse_plan({'confirm':1,'check_every_s':1,'time_budget_s':60,'verifier':'monitor','stages':[{'name':'a','prompt':'same refused prompt','timeout_s':2,'max_attempts':3,'on_fail':'retry_in_place'}]})
r,c,st,l=runner(p,Sequence([]),st=RefusingStation());a=r.run()
out['refusal_retry']={'starts':st.starts,'reason':a.reason}
p=make();mon=MonitorVerifier(p,transport=lambda _: {})
payload=mon._build(p.stages[0],Frames().latest(),6)
payload2=mon._build(p.stages[0],Frames().latest(),9)
out['baseline_after_motion']={'first_capture_elapsed':mon._first['scene'][0],'second_request_labels':[x['text'] for x in payload2['messages'][0]['content'] if x.get('type')=='text' and 'overhead' in x.get('text','')]}
p=load_plan(root/repo/'plans/hill_climb_v4.yaml')
out['match_defaults']={'checkpoint':p.checkpoint,'confirm':p.confirm,'confirm_gap_s':getattr(p,'confirm_gap_s','unsupported'),'claude':p.claude_user,'tower_prompts':p.tower_prompts}
try:
    from PIL import Image,ImageDraw
    from harness.detect import TableDetector
    det=TableDetector.from_file(root/repo/'plans/table_colors.yaml')
    im=Image.new('RGB',(320,240),(185,160,130));ImageDraw.Draw(im).ellipse((125,115,195,185),fill=(250,250,250));b=io.BytesIO();im.save(b,format='JPEG')
    out['neutral_white_ball_guard']=det.guard({'wrist_r':b.getvalue()})
except ImportError as e:out['detector_unavailable']=str(e)
if repo=='harness-cloud-sims':
    from harness.live_guard import LiveGuard
    c=Clock(); count=[0]
    def source():count[0]+=1;return {'wrist_l':str(count[0]).encode()}
    lg=LiveGuard(source,lambda _:(False,'left clear'),lambda:None,clock=c.now);lg.arm('ball_1')
    for i in range(30):c.sleep(1);lg.step()
    out['missing_right_wrist']={'elapsed':c.t,'last_answer_t':lg.last_answer_t,'tripped':lg.tripped,'roles_seen':list(lg._sig)}
    out['home_reference_zero_distance']={}
    from harness.start_pose import StartPoses
    sp=StartPoses.load(root/repo/'plans/start_poses.json')
    for prompt in sp.by_prompt:
        chk=sp.check(prompt,p.start_pose.home_pose,.35)
        out['home_reference_zero_distance'][prompt]={'status':chk.status,'distance':chk.distance}
print(json.dumps(out,indent=2))
