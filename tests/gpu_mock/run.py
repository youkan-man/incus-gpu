#!/usr/bin/env python3
"""Run the real incus-gpu CLI against the stateful GPU boundary model.

No root, Incus daemon, real GPU, real /sys writes, or shell function overrides.
Requires Linux/glibc, Bash, Python >=3.8 and a C compiler.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback
from model import Model, GPU, AUDIO, VM, PROJECT

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def check(condition, message):
    if not condition: raise AssertionError(message)


class Case:
    def __init__(self, name, output, library, tool):
        self.name, self.output, self.library, self.tool = name, output / name, library, tool
        self.output.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='igm-')
        self.model = None
        self.commands = []

    def setup(self, **options):
        self.model = Model(self.tmp.name, **options).start()
        self.model.env['LD_PRELOAD'] = str(self.library)
        self.before = self.model.snapshot()
        return self.model

    def command(self, *args, env=None):
        n = len(self.commands) + 1
        report = self.output / ('run-%02d' % n); report.mkdir(mode=0o700)
        childenv = dict(self.model.env, INCUS_GPU_RUN_DIR=str(report))
        if env: childenv.update(env)
        argv = ['bash', str(self.tool)] + list(args)
        start = time.monotonic()
        result = subprocess.run(argv, env=childenv, capture_output=True, text=True, timeout=42)
        elapsed = time.monotonic() - start
        (self.output / ('run-%02d.stdout.txt' % n)).write_text(result.stdout)
        (self.output / ('run-%02d.stderr.txt' % n)).write_text(result.stderr)
        self.commands.append(dict(argv=argv, exit=result.returncode, elapsed=round(elapsed, 3), report=report.name))
        self.last, self.report = result, report
        return result

    def attach(self, *extra, env=None, selector=GPU):
        return self.command('attach', VM, selector, '--project', PROJECT,
                            '--restart', '--prebind-vfio', '--vfio-timeout', '1', *extra, env=env)

    def inspect(self, *extra):
        return self.command('inspect', VM, GPU, '--project', PROJECT, '--timeout', '1', *extra)

    def rc(self, code=0):
        check(self.last.returncode == code, 'exit %s != %s\n%s' % (self.last.returncode, code, self.last.stderr))

    def failed(self):
        check(self.last.returncode != 0, 'unexpected success\n' + self.last.stderr)

    def event(self, name): return [e for e in self.model.events if e['event'] == name]

    def writes(self):
        return [e for e in self.event('request') if e['op'] == 'write']

    def calls(self, contains):
        return [e for e in self.event('request') if e['op'] == 'cli' and contains in ' '.join(e['args'])]

    def restored(self, vm_status='Running'):
        state = self.model.snapshot()
        check(state['devices'] == self.before['devices'], 'PCI state not restored')
        check(state['vm']['status'] == vm_status, 'VM not restored')

    def bound_and_running(self):
        s = self.model.snapshot()
        check(s['vm']['status'] == 'Running', 'VM did not run')
        check(set(s['guest_devices']) == {GPU, AUDIO}, 'mock guest did not receive both functions')
        check(all(s['devices'][b]['driver'] == 'vfio-pci' for b in (GPU, AUDIO)), 'partial bind')
        check(all(s['devices'][b] == self.before['devices'][b] for b in s['devices'] if b not in (GPU, AUDIO)), 'topology bridge mutated')

    def no_mutation(self):
        check(not self.writes(), 'unexpected PCI write')
        check(not self.event('vm-stop') and not self.event('vm-start') and not self.event('vm-config'), 'unexpected VM mutation')

    def fenced(self):
        self.rc(124)
        check((self.report / 'unsafe.json').exists(), 'missing uncertainty report')
        check((Path(self.tmp.name) / 'state/uncertain.json').exists(), 'missing persistent uncertainty')
        fault = self.event('fault')[-1]['seq']
        check(not [e for e in self.writes() if e['seq'] > fault], 'write after uncertain operation')
        check(not [e for e in self.event('vm-start') if e['seq'] > fault], 'restart after uncertain operation')
        check(self.model.instance['status'] == 'Stopped', 'unsafe VM restart')

    def close(self):
        if self.model:
            (self.output / 'before.json').write_text(json.dumps(self.before, indent=2))
            (self.output / 'after.json').write_text(json.dumps(self.model.snapshot(), indent=2))
            self.model.close()
            events = Path(self.tmp.name) / 'model-events.jsonl'
            if events.exists(): shutil.copyfile(events, self.output / 'model-events.jsonl')
        (self.output / 'commands.json').write_text(json.dumps(self.commands, indent=2))
        self.tmp.cleanup()


SCENARIOS = {}
def scenario(fn): SCENARIOS[fn.__name__] = fn; return fn

@scenario
def reported_partial_binding(c):
    c.setup(); c.attach(selector='1'); c.rc(); c.bound_and_running()
    check(not [e for e in c.writes() if e['value'] == AUDIO], 'already-bound audio was unnecessarily rebound')

@scenario
def host_drivers_to_vfio(c):
    c.setup(gpu_driver='amdgpu', audio_driver='snd_hda_intel')
    c.attach(); c.rc(); c.bound_and_running()
    for b in (GPU, AUDIO):
        override = next(e['seq'] for e in c.writes() if b + '/driver_override' in e['path'])
        unbind = next(e['seq'] for e in c.writes() if e['path'].endswith('/unbind') and e['value'] == b)
        check(override < unbind, 'override must precede unbind')

@scenario
def separate_audio_group(c):
    c.setup(gpu_driver='amdgpu', audio_driver='snd_hda_intel', separate_group=True)
    c.attach(); c.rc(); c.bound_and_running()

@scenario
def snapshot_second_function_blocks_before_stop(c):
    m=c.setup(); m.fault('read', AUDIO+'/driver_override', action='hold')
    c.attach(); c.failed(); c.no_mutation()
    check(m.instance['status']=='Running', 'snapshot failure stopped VM')

@scenario
def override_write_blocks(c):
    m=c.setup(); m.fault('write', GPU+'/driver_override', action='hold')
    c.attach(); c.fenced()
    count=len(m.events); c.attach(); c.rc(125)
    check(len(m.events)==count, 'retry accessed hardware despite unresolved mutation')

@scenario
def gpu_bind_blocks(c):
    m=c.setup(); m.fault('write','drivers/vfio-pci/bind',value=GPU,action='hold')
    c.attach(); c.fenced()
    check(not m.instance['devices'], 'GPU config added after stalled bind')

@scenario
def host_unbind_blocks(c):
    m=c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    m.fault('write','drivers/amdgpu/unbind',value=GPU,action='hold')
    c.attach(); c.fenced()


def rejected_gpu(c, error):
    m=c.setup(); m.fault('write','drivers/vfio-pci/bind',value=GPU,error=error)
    c.attach(); c.failed(); c.restored()
    check('errno=%d'%error in c.last.stderr, 'kernel errno lost')
    check(not m.instance['devices'], 'config added after rejected bind')

@scenario
def gpu_bind_einval(c): rejected_gpu(c,errno.EINVAL)
@scenario
def gpu_bind_ebusy(c): rejected_gpu(c,errno.EBUSY)
@scenario
def gpu_bind_enodev(c): rejected_gpu(c,errno.ENODEV)
@scenario
def gpu_bind_eio(c): rejected_gpu(c,errno.EIO)

@scenario
def audio_bind_rejected_restores_both(c):
    m=c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    m.fault('write','drivers/vfio-pci/bind',value=AUDIO,error=errno.EINVAL)
    c.attach(); c.failed(); c.restored()
    check(any(e['path'].endswith('/amdgpu/bind') for e in c.writes()), 'first GPU was not restored')

@scenario
def restore_original_driver_with_different_override(c):
    m=c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    m.override(GPU,'some-future-driver'); c.before=m.snapshot()
    m.fault('write','drivers/vfio-pci/bind',value=AUDIO)
    c.attach(); c.failed(); c.restored()

@scenario
def restore_failure_no_restart(c):
    m=c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    m.fault('write','drivers/vfio-pci/bind',value=AUDIO)
    m.fault('write','drivers/amdgpu/bind',value=GPU,error=errno.EIO)
    c.attach(); c.failed()
    check(m.instance['status']=='Stopped','VM restarted after failed PCI restore')
    check(not c.event('vm-start'),'extra startup attempted')

@scenario
def incus_add_failure_rolls_back(c):
    m=c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    m.fault('cli','config device add')
    c.attach(); c.failed(); c.restored()
    check(not m.instance['devices'],'config left behind')

@scenario
def incus_start_failure_rolls_back(c):
    m=c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    m.fault('cli','start '+VM)
    c.attach(); c.failed(); c.restored()
    check(not m.instance['devices'],'config left behind')
    check(len(c.calls('start '+VM))==2,'original VM not recovered exactly once')

@scenario
def rollback_remove_failure_no_restart(c):
    m=c.setup(); m.fault('cli','start '+VM); m.fault('cli','config device remove')
    c.attach(); c.failed()
    check(m.instance['status']=='Stopped' and m.instance['devices'],'wrong rollback state')
    check(len(c.calls('start '+VM))==1,'bad configuration restarted')

@scenario
def stopped_vm_restart_starts(c):
    c.setup(vm_status='Stopped'); c.attach(); c.rc(); c.bound_and_running()
    check(not c.event('vm-stop'),'already stopped VM stopped again')

@scenario
def existing_configuration_retry(c):
    c.setup(vm_status='Stopped',existing=True); c.attach(); c.rc(); c.bound_and_running()
    check(not c.calls('config device add'),'duplicate config created')

@scenario
def existing_failure_keeps_configuration(c):
    m=c.setup(existing=True,gpu_driver='vfio-pci'); m.fault('cli','start '+VM)
    c.attach(); c.failed()
    check(m.instance['devices']==c.before['vm']['devices'],'existing GPU config removed')
    check(not c.calls('config device remove'),'existing config deletion attempted')
    check(len(c.calls('start '+VM))==1,'startup retried without authorization')

@scenario
def inspect_is_non_mutating(c):
    m=c.setup(); c.inspect(); c.rc(); c.no_mutation()
    s=json.loads((c.report/'summary.json').read_text())
    check(not s['passthrough_verified'],'read-only inspection claims passthrough success')
    check(any(f['code']=='PARTIAL_BINDING' for f in s['findings']),'partial state not diagnosed')
    c.restored()

@scenario
def inspect_read_stall_is_bounded(c):
    m=c.setup(); m.fault('read',GPU+'/driver_override',action='hold')
    c.inspect(); c.rc(124); c.no_mutation()
    s=json.loads((c.report/'summary.json').read_text())
    check(s['result']=='INCOMPLETE' and not s['passthrough_verified'],'false diagnosis success')
    check(any('driver_override' in name for name in s['timed_out_operations']),'missing blocked operation')

@scenario
def explicit_probe_restores_state(c):
    c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    c.inspect('--probe'); c.rc(); c.restored()
    check(not c.event('vm-config'),'probe modifies VM configuration')

@scenario
def release_busy_audio_group(c):
    m=c.setup(gpu_driver='vfio-pci',separate_group=True,vm_status='Stopped'); m.users.add('3')
    c.command('release-vfio',GPU,'--force'); c.failed(); c.no_mutation()

@scenario
def release_busy_cdev(c):
    m=c.setup(gpu_driver='vfio-pci',vm_status='Stopped'); m.users.add('vfio1')
    c.command('release-vfio',GPU,'--force'); c.failed(); c.no_mutation()

@scenario
def release_both_host_functions(c):
    m=c.setup(gpu_driver='vfio-pci',vm_status='Stopped')
    c.command('release-vfio',GPU); c.rc()
    check(m.devices[GPU]['driver']=='amdgpu' and m.devices[AUDIO]['driver']=='snd_hda_intel','host reprobe did not occur')
    check(not m.devices[GPU]['override'] and not m.devices[AUDIO]['override'],'override not cleared')

@scenario
def release_partial_unbound_override(c):
    m=c.setup(vm_status='Stopped'); m.override(GPU,'vfio-pci'); c.before=m.snapshot()
    c.command('release-vfio',GPU); c.rc()
    check(not m.devices[GPU]['override'],'unbound GPU keeps stale vfio-pci override despite successful release')
    check(m.devices[GPU]['driver']=='amdgpu','unbound VFIO-selected GPU not reprobed to host')

@scenario
def foreign_endpoint_audio_group_blocks_before_stop(c):
    m=c.setup(separate_group=True); m.add_device('0000:0b:00.0','0x020000','3','e1000e'); c.before=m.snapshot()
    c.attach(); c.failed(); c.no_mutation()

@scenario
def vm_stop_failure_does_not_bind(c):
    m=c.setup(); m.fault('cli','stop '+VM)
    c.attach(); c.failed(); c.no_mutation(); c.restored()

@scenario
def delayed_bind_within_budget(c):
    m=c.setup(); m.fault('write','drivers/vfio-pci/bind',value=GPU,action='delay',delay=.2)
    c.attach(); c.rc(); c.bound_and_running()

@scenario
def accepted_probe_without_driver_link_is_failure(c):
    m=c.setup(); m.fault('write','drivers/vfio-pci/bind',value=GPU,action='noop')
    c.attach(); c.failed(); c.restored()

@scenario
def idempotent_attach_does_not_mutate(c):
    m=c.setup(); c.attach(); c.rc(); c.bound_and_running()
    before=m.snapshot(); first=len(m.events)
    c.command('attach',VM,GPU,'--project',PROJECT); c.rc()
    check(m.snapshot()==before,'idempotent attach changed state')
    check(not [e for e in m.events[first:] if e['event'] in ('state','vm-config','vm-start','vm-stop')],'idempotency mutation')

@scenario
def attach_detach_release_roundtrip(c):
    m=c.setup(gpu_driver='amdgpu',audio_driver='snd_hda_intel')
    c.attach(); c.rc(); c.bound_and_running()
    c.command('detach',VM,GPU,'--project',PROJECT,'--restart'); c.rc()
    check(m.instance['status']=='Running' and not m.instance['devices'] and not m.guest_devices,'detach failed')
    c.command('release-vfio',GPU); c.rc(); c.restored()

@scenario
def concurrent_attach_excluded(c):
    m=c.setup(); m.fault('write','drivers/vfio-pci/bind',value=GPU,action='hold')
    report=c.output/'parallel-run'; report.mkdir(mode=0o700)
    env=dict(m.env,INCUS_GPU_RUN_DIR=str(report))
    cmd=['bash',str(c.tool),'attach',VM,GPU,'--project',PROJECT,'--restart','--prebind-vfio','--vfio-timeout','4']
    with open(c.output/'parallel.stdout.txt','w') as out, open(c.output/'parallel.stderr.txt','w') as err:
        p=subprocess.Popen(cmd,env=env,stdout=out,stderr=err)
        try:
            deadline=time.monotonic()+15
            while not c.event('fault') and time.monotonic()<deadline: time.sleep(.02)
            check(c.event('fault'),'first operation did not reach blocked bind')
            before=len(m.events); c.attach(); c.rc(125)
            check(len(m.events)==before,'concurrent operation accessed hardware')
            check(p.wait(timeout=10)==124,'first operation lost timeout result')
        finally:
            if p.poll() is None: p.kill(); p.wait(timeout=3)

@scenario
def remote_start_completes_after_client_timeout(c):
    m=c.setup(); m.fault('cli','start '+VM,action='delay',delay=16)
    c.attach(env={'INCUS_GPU_TOTAL_TIMEOUT':'9'}); c.rc(124)
    pending=Path(c.tmp.name)/'state/uncertain.json'
    check(pending.exists(),'remote mutation uncertainty not persisted')
    check(m.inflight,'server operation did not outlive killed CLI')
    check(not c.calls('config device remove'),'config removed during uncertain remote start')
    c.inspect(); c.rc()
    check(pending.exists(),'inspection cleared an active server operation')
    deadline=time.monotonic()+18
    while m.inflight and time.monotonic()<deadline: time.sleep(.05)
    check(not m.inflight,'late server completion did not happen')
    c.bound_and_running()
    c.inspect(); c.rc()
    check(not pending.exists(),'completed remote operation not reconciled')



@scenario
def release_preserves_unrelated_selection(c):
    m=c.setup(audio_driver='snd_hda_intel',vm_status='Stopped')
    m.override(GPU,'amdgpu'); c.before=m.snapshot()
    c.command('release-vfio',GPU); c.rc(); c.no_mutation(); c.restored('Stopped')

@scenario
def release_override_read_failure_prevents_changes(c):
    m=c.setup(gpu_driver='vfio-pci',vm_status='Stopped')
    m.fault('read',GPU+'/driver_override',error=errno.EIO)
    c.command('release-vfio',GPU); c.failed(); c.no_mutation(); c.restored('Stopped')

@scenario
def boot_vga_requires_explicit_override(c):
    m=c.setup(); (m.sys/'bus/pci/devices'/GPU/'boot_vga').write_text('1\n')
    c.attach(); c.failed(); c.no_mutation()

@scenario
def ownership_query_timeout_is_not_unused(c):
    m=c.setup(gpu_driver='vfio-pci',vm_status='Stopped')
    m.fault('cli','fuser',action='hold')
    c.command('release-vfio',GPU,'--force'); c.failed(); c.no_mutation(); c.restored('Stopped')

@scenario
def shim_errno_preserves_old_value_and_unrelated_io(c):
    m=c.setup(gpu_driver='amdgpu'); m.override(GPU,'amdgpu'); c.before=m.snapshot()
    m.fault('write',GPU+'/driver_override',error=errno.EPERM)
    ordinary=Path(c.tmp.name)/'ordinary-file'; ordinary.write_text('ordinary')
    path=m.sys/'bus/pci/devices'/GPU/'driver_override'
    code="""import os,sys,errno
p=sys.argv[1]
fd=os.open(p,os.O_WRONLY|os.O_TRUNC)
try:
    os.write(fd,b'vfio-pci\\n')
except OSError as e:
    assert e.errno == errno.EPERM
else:
    raise AssertionError('shim did not surface errno')
finally:
    os.close(fd)
with open(sys.argv[2],'w') as f:f.write('replaced')
assert open(sys.argv[2]).read()=='replaced'
"""
    r=subprocess.run(['python3','-S','-c',code,str(path),str(ordinary)],env=m.env,capture_output=True,text=True,timeout=5)
    check(r.returncode==0,r.stderr)
    check(path.read_text()=='amdgpu\n','O_TRUNC destroyed sysfs before failed write')
    check(ordinary.read_text()=='replaced','unrelated file I/O altered by shim')
    check(not c.event('state'),'failed write changed GPU state')
    check(not any(str(ordinary) in e.get('path','') for e in m.events),'unrelated I/O was routed to mock')


def run_case(name, output, library, tool):
    c=Case(name,output,library,tool); start=time.monotonic()
    record={'name':name,'status':'PASS'}
    try:
        SCENARIOS[name](c)
    except Exception:
        record['status']='FAIL'; record['error']=traceback.format_exc()
        (c.output/'failure.txt').write_text(record['error'])
    finally:
        c.close()
    record['elapsed']=round(time.monotonic()-start,3)
    record['commands']=c.commands
    (c.output/'result.json').write_text(json.dumps(record,indent=2))
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'gpu-mock-results')
    parser.add_argument('--tool',type=Path,default=ROOT/'incus-gpu')
    parser.add_argument('--jobs',type=int,default=4)
    parser.add_argument('--case',action='append',dest='cases')
    args=parser.parse_args()
    if not 1 <= args.jobs <= 8: parser.error('--jobs must be 1..8')
    out=args.output.resolve(); out.mkdir(parents=True,exist_ok=True)
    library=out/'libgpumock.so'
    compiler=os.environ.get('CC','cc')
    subprocess.run([compiler,'-shared','-fPIC','-O2','-Wall','-Wextra','-Werror',str(HERE/'io_shim.c'),'-o',str(library)],check=True)
    selected=args.cases or sorted(SCENARIOS)
    for name in selected:
        if name not in SCENARIOS: parser.error('unknown scenario: '+name)
    start=time.monotonic(); results=[]
    before_hash=hashlib.sha256(args.tool.read_bytes()).hexdigest()
    runtime_file=args.tool.parent/'lib/incus_gpu_runtime.py'
    before_runtime_hash=hashlib.sha256(runtime_file.read_bytes()).hexdigest()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures=[pool.submit(run_case,name,out,library,args.tool.resolve()) for name in selected]
        for future in as_completed(futures):
            record=future.result(); results.append(record)
            print('%s %-55s %6.2fs'%(record['status'],record['name'],record['elapsed']),flush=True)
            if record.get('error'): print(record['error'],flush=True)
    report={'mock':'userspace stateful sysfs/VFIO and Incus boundary model',
            'real_gpu_verified':False,'tool_sha256':hashlib.sha256(args.tool.read_bytes()).hexdigest(),
            'runtime_sha256':hashlib.sha256((args.tool.parent/'lib/incus_gpu_runtime.py').read_bytes()).hexdigest(),
            'passed':sum(r['status']=='PASS' for r in results),'failed':sum(r['status']=='FAIL' for r in results),
            'elapsed':round(time.monotonic()-start,3),'scenarios':sorted(results,key=lambda r:r['name'])}
    report['sources_unchanged']=(report['tool_sha256']==before_hash and report['runtime_sha256']==before_runtime_hash)
    if not report['sources_unchanged']: report['failed'] += 1
    (out/'summary.json').write_text(json.dumps(report,indent=2))
    print('\n%d passed, %d failed. Evidence: %s'%(report['passed'],report['failed'],out),flush=True)
    return int(report['failed']>0)

if __name__=='__main__': raise SystemExit(main())
