#!/usr/bin/env python3
"""Differentially build/run mncs.test.digest::build_digest with Stage-0 and VM."""
import hashlib, json, subprocess, sys, tempfile, threading, time
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
TEST=Path('/workspace/mncs-test'); STDLIB=Path('/workspace/mncs-stdlib')
LANGUAGE=Path('/workspace/mncs-language'); VMREPO=Path('/workspace/mncs-vm')
ROOT_FILE=TEST/'native/mncs/test/digest.mncs'
FILES={'mncs.test.assertions':TEST/'native/mncs/test/assertions.mncs',
       'mncs.test.suite':TEST/'native/mncs/test/suite.mncs',
       'mncs.std.application.v1':STDLIB/'library/std/application.mncs'}
ENTRY='mncs.test.digest::build_digest'
ARTIFACT=ROOT/'.build/native-app-readiness/test-digest.vm.json'
EVIDENCE=ROOT/'evidence/native-app-readiness-test-digest.json'
sys.path.insert(0,str(ROOT/'tools'))
import test_vm_native_application as h

def sha(b): return hashlib.sha256(b).hexdigest()
def rev(p): return subprocess.check_output(['git','-C',str(p),'rev-parse','HEAD'],text=True).strip()
def status(p): return subprocess.check_output(['git','-C',str(p),'status','--short'],text=True).strip()
def u(n): return {'integer':{'type':{'bits':64,'signed':False},'value':n}}
def i(n): return {'integer':{'type':{'bits':64,'signed':True},'value':n}}
def b(s): return {'sequence':{'values':[{'byte':{'value':x}} for x in s.encode()]}}
def rec(name,fields,ids): return {'record':{'name':name,'type_identity':ids[name],'fields':[[k,v] for k,v in sorted(fields.items())]}}
def fin(interface,tname,vname):
 t=next(x for x in interface['types']['finite'] if x['name']==tname); v=next(x for x in t['variants'] if x['name']==vname)
 return {'finite':{'discriminant':v['discriminant'],'payload':[],'type_identity':t['identity'],'variant_identity':v['identity']}}
def testrow(interface,ids,name,verdict,kind='NoFailure',assertions=1,failures=0,expected=7,actual=7,code=0,line=1):
 return rec('DigestTest',{'qualified_name':b(name),'verdict':fin(interface,'Verdict',verdict),'failure_kind':fin(interface,'FailureKind',kind),'assertions':u(assertions),'failures':u(failures),'expected':i(expected),'actual':i(actual),'assertion_code':u(code),'source':b('native/mncs/test/digest.mncs'),'line':u(line),'column':u(3),'semantic_fingerprint':b('sha256:digest-contract')},ids)
def vm_call(arg,aid):
 with tempfile.TemporaryDirectory(prefix='digest-call-') as td:
  td=Path(td); ap=td/'args.json'; tp=td/'types.json'; ap.write_text(json.dumps([arg],separators=(',',':'))); tp.write_text('[]')
  cmd=[str(h.VM_BIN),'run','--artifact',str(ARTIFACT),'--callable',ENTRY,'--args',str(ap),'--type-args',str(tp)]
  start=time.monotonic(); p=subprocess.Popen(cmd,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True); first=h.process_sample(p.pid); samples=[first]
  def watch():
   while p.poll() is None: samples.append(h.process_sample(p.pid)); time.sleep(.01)
  watcher=threading.Thread(target=watch,daemon=True); watcher.start()
  try: out,err=p.communicate(timeout=45)
  except subprocess.TimeoutExpired:
   p.kill(); p.communicate(); watcher.join(timeout=1); raise TimeoutError('canonical VM exceeded 45 seconds')
  wall=time.monotonic()-start; watcher.join(timeout=1); samples.append(h.process_sample(p.pid))
  if p.returncode: raise AssertionError(f'VM exit {p.returncode}: {err[-3000:]}')
  doc=json.loads(out); row=doc['record']
  if row.get('artifact_id')!=aid: raise AssertionError('VM receipt artifact mismatch')
  metrics=h.summarize_samples(samples,wall,first['cpu_seconds']); metrics['process_count']=max((x.get('child_process_count',0)+1 for x in samples),default=1)
  return doc,metrics

def main():
 if not h.PROBE.is_file() or not h.VM_BIN.is_file(): raise SystemExit('pinned probe or VM missing')
 modules={k:p.read_text() for k,p in FILES.items()}; root=ROOT_FILE.read_text()
 request={'schema_version':'mncs.compiler.application-build-request/1','target':'mncs-vm-0.1','root_source_id':'native/mncs/test/digest.mncs','root_source':root,'entry':{'module':'mncs.test.digest','function':'build_digest'},'modules':modules,'generic_seeds':[],'capabilities':[],'verification_policy':'stage0-semantic-check+canonical-vm-artifact/1'}
 cache_root=ROOT/'.build/native-app-readiness'; cache_root.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='digest-cold-',dir=cache_root) as cache:
  probe=h.Probe(Path(cache))
  try:
   built,bm=probe.send({'compile_application':request},timeout=90); iface=built.get('entry',{}).get('interface',{})
   if built.get('schema_version')!='mncs.compiler.application-build/4' or built.get('status')!='verified' or iface.get('status')!='verified' or built.get('unresolved_obligations')!=[]: raise AssertionError(json.dumps({'status':built.get('status'),'entry':built.get('entry'),'obligations':built.get('unresolved_obligations'),'diagnostics':built.get('diagnostics')},indent=2)[:8000])
   artifact=built['artifact']['value']; aid=built['artifact']['identity']
   if artifact.get('artifact_id')!=aid or artifact.get('schema_version')!='mncs.vm.artifact/1': raise AssertionError('wrong artifact identity/schema')
   if iface.get('types',{}).get('status')!='verified' or iface['types'].get('unresolved_references')!=[]: raise AssertionError('unresolved callable types')
   if iface['signature'].get('function_identity')!='mncs:0.2:function:'+ENTRY or iface['signature'].get('inputs')!=[{'name':'request','type':'DigestRequest'}] or iface['signature'].get('outputs')!=[{'name':'result','type':'DigestResult'}]: raise AssertionError('callable signature mismatch')
   iid=h.sha(json.dumps({'signature':iface['signature'],'types':iface['types']},sort_keys=True,separators=(',',':')).encode())
   if iface.get('identity')!=iid: raise AssertionError('interface identity mismatch')
   if built.get('cache',{}).get('status')!='miss' or built['cache'].get('publication')!='stored': raise AssertionError('cold cache experiment was not isolated')
   ids={x['name']:x['identity'] for x in iface['types']['records']}
   run=rec('DigestRun',{'run_id':b('run-digest-readiness-20261014'),'module_name':b('mncs.test.digest'),'source':b('native/mncs/test/digest.mncs'),'backend':b('mncs-vm-0.1'),'artifact_identity':b(aid)},ids)
   rows=[testrow(iface,ids,'digest-contract::pass','PASS',expected=42,actual=42),testrow(iface,ids,'digest-contract::first-failure','FAIL','Assertion',failures=1,expected=42,actual=41,code=2101,line=12),testrow(iface,ids,'digest-contract::skip','SKIP',assertions=0,expected=0,actual=0,line=20),testrow(iface,ids,'digest-contract::second-failure','FAIL','Assertion',failures=1,expected=8,actual=9,code=2102,line=24)]
   passes=[testrow(iface,ids,'digest-contract::pass-a','PASS',expected=1,actual=1),testrow(iface,ids,'digest-contract::pass-b','PASS',expected=2,actual=2,line=2)]
   cases=[('mixed-cap-one',1,rows),('mixed-cap-zero',0,rows),('all-pass',4,passes)]
   args=[rec('DigestRequest',{'schema_version':b('mncs.test-digest-request/1'),'max_failures':u(cap),'run':run,'tests':{'sequence':{'values':rs}}},ids) for _,cap,rs in cases]
   calls=[{'schema_version':'0.1','target':{'module':'mncs.test.digest','function':'build_digest'},'arguments':[x],'type_arguments':[],'step_budget':3000000} for x in args]
   oracle,om=probe.send({'project_oracle':{'root':root,'modules':modules,'calls':calls}},timeout=90)
   if not oracle.get('valid') or any(x.get('status')!='returned' for x in oracle.get('executions',[])): raise AssertionError(json.dumps({'valid':oracle.get('valid'),'diagnostics':oracle.get('diagnostics'),'executions':oracle.get('executions')},indent=2)[:8000])
  finally: probe.close()
 ARTIFACT.parent.mkdir(parents=True,exist_ok=True); artifact_bytes=json.dumps(artifact,sort_keys=True,separators=(',',':')).encode(); ARTIFACT.write_bytes(artifact_bytes)
 stage0=[[h.decode_wire(v) for v in x['returned']] for x in oracle['executions']]; vm=[]; vm_metrics=[]; receipts=[]
 for idx,arg in enumerate(args):
  print(f'canonical VM case {idx+1}/3',flush=True); doc,m=vm_call(arg,aid); recpt=doc['record']; receipts.append({'artifact_id':recpt.get('artifact_id'),'status':recpt.get('status'),'callable':recpt.get('callable')}); vm.append([h.decode_wire(h.vm_to_wire(v)) for v in recpt.get('returned',[])]); vm_metrics.append(m)
 if stage0!=vm: raise AssertionError(json.dumps({'stage0':stage0,'vm':vm},indent=2)[:12000])
 summaries=[]
 for (name,_,_),rs in zip(cases,stage0):
  out=rs[0]; s=out['summary']; summary={'case':name,'verdict':s['verdict']['$v'],'verdict_code':s['verdict_code'],'total':s['total'],'passed':s['passed'],'failed':s['failed'],'skipped':s['skipped'],'failure_rows':out['failure_rows'],'failures_omitted':out['failures_omitted'],'retained_names':[bytes(x['qualified_name']).decode() for x in out['failures']],'artifact_echo':bytes(out['run']['artifact_identity']).decode()}; summaries.append(summary)
 if summaries[0] != {'case':'mixed-cap-one','verdict':1,'verdict_code':1,'total':4,'passed':1,'failed':2,'skipped':1,'failure_rows':2,'failures_omitted':1,'retained_names':['digest-contract::first-failure'],'artifact_echo':aid}: raise AssertionError(f'cap-one state incorrect: {summaries[0]}')
 if (summaries[1]['failure_rows'],summaries[1]['failures_omitted'],summaries[1]['retained_names'])!=(2,2,[]): raise AssertionError('cap-zero truncation incorrect')
 if (summaries[2]['verdict'],summaries[2]['passed'],summaries[2]['failed'],summaries[2]['failures_omitted'])!=(0,2,0,0): raise AssertionError('all-pass state incorrect')
 sources=[{'module':k,'path':str(p),'sha256':sha(p.read_bytes())} for k,p in sorted(FILES.items())]; sources.append({'module':'mncs.test.digest','path':str(ROOT_FILE),'sha256':sha(ROOT_FILE.read_bytes())})
 producer={'compiler_revision':rev(ROOT),'compiler_status':status(ROOT),'language_revision':rev(LANGUAGE),'test_revision':rev(TEST),'test_status':status(TEST),'stdlib_revision':rev(STDLIB),'stdlib_status':status(STDLIB),'vm_revision':rev(VMREPO),'vm_status':status(VMREPO),'probe_path':str(h.PROBE),'probe_sha256':sha(h.PROBE.read_bytes()),'vm_path':str(h.VM_BIN),'vm_sha256':sha(h.VM_BIN.read_bytes())}
 evidence={'schema_version':'mncs.compiler.native-application-proof/1','created_at':datetime.now(timezone.utc).isoformat(),'objective':'Gate A: real mncs-test digest function agrees between Stage-0 and canonical VM','application':{'root_source_id':'native/mncs/test/digest.mncs','entry':ENTRY,'module_sources':sources,'target':'mncs-vm-0.1','capabilities':[]},'producer':producer,'build':{'request_schema':request['schema_version'],'result_schema':built['schema_version'],'status':built['status'],'entry_status':built['entry']['status'],'unresolved_obligations':built['unresolved_obligations'],'diagnostics':built.get('diagnostics',[]),'cache':built.get('cache'),'metrics':bm,'artifact_identity':aid,'artifact_schema':artifact['schema_version'],'artifact_path':str(ARTIFACT),'artifact_file_sha256':sha(artifact_bytes),'artifact_bytes':len(artifact_bytes),'callable_interface':iface,'callable_interface_identity':iface['identity']},'execution':{'inputs':args,'input_sha256':sha(json.dumps(args,sort_keys=True,separators=(',',':')).encode()),'stage0_metrics':om,'canonical_vm_metrics':vm_metrics,'vm_receipts':receipts,'stage0_results':stage0,'canonical_vm_results':vm,'exact_result_agreement':True,'case_summaries':summaries},'feature_evidence':['multiple imported modules','nominal nested records and finite values','bounded byte/test sequences and folds','pass/fail/skip state transitions','bounded failure selection with explicit omission count','structured source fields and exact artifact receipt'],'limitations':['This proof runs pure build_digest; digest_app structured capability effects are not exercised.','This path does not prove complete mncs-test conversion or Stage-1 succession.']}
 EVIDENCE.write_text(json.dumps(evidence,indent=2,sort_keys=True)+'\n')
 print(json.dumps({'status':'passed','evidence':str(EVIDENCE),'artifact_id':aid,'interface_id':iface['identity'],'build_metrics':bm,'stage0_metrics':om,'vm_metrics':vm_metrics,'case_summaries':summaries},separators=(',',':')))
if __name__=='__main__': main()
