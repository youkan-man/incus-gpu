"""Real blocking-I/O regressions. A FIFO does not emulate a kernel D task;
that distinct survivor path has a separate injected unkillable process test.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'lib/incus_gpu_runtime.py'
TOOL = ROOT / 'incus-gpu'
spec = importlib.util.spec_from_file_location('runtime', RUNTIME)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.report = runtime.Report(self.root / 'report')
        self.oldmask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.oldmask)
        self.tmp.cleanup()

    def test_normal_io_and_errno(self):
        p = self.root / 'attribute'
        p.write_text('initial-value')
        r = runtime.bounded(runtime.command_worker('write', p, 'vfio-pci'), 2, self.report, 'write', mutation=True)
        self.assertEqual(r['rc'], 0)
        r = runtime.bounded(runtime.command_worker('read', p), 2, self.report, 'read')
        self.assertEqual(r['stdout'], 'vfio-pci\n')
        r = runtime.bounded(runtime.command_worker('write', self.root / 'missing', 'x'), 2, self.report, 'missing', mutation=True)
        self.assertEqual(r['rc'], 1)
        self.assertIn('errno=2 (ENOENT)', r['stderr'])
        self.assertFalse((self.root / 'missing').exists())

    def test_real_blocked_read(self):
        p = self.root / 'driver_override'
        os.mkfifo(p)
        start = time.monotonic()
        r = runtime.bounded(runtime.command_worker('read', p), .2, self.report, 'read driver_override')
        self.assertEqual(r['rc'], 124)
        self.assertLess(time.monotonic() - start, 3)
        self.assertIn('stack', r['process'])
        self.assertFalse((self.report.root / 'unsafe.json').exists())

    def test_real_blocked_write_fences_later_mutations(self):
        p = self.root / 'bind'
        os.mkfifo(p)
        r = runtime.bounded(runtime.command_worker('write', p, '0000:0a:00.0'), .2, self.report, 'bind', mutation=True)
        self.assertEqual(r['rc'], 124)
        self.assertTrue((self.report.root / 'unsafe.json').exists())
        out = self.root / 'unchanged'
        out.write_text('original')
        r = runtime.bounded(runtime.command_worker('write', out, 'bad'), .2, self.report, 'rollback', mutation=True)
        self.assertEqual(r['rc'], 125)
        self.assertEqual(out.read_text(), 'original')

    def test_descendant_cannot_hold_callers_output_pipe_open(self):
        # Parent shell exits but its child inherits stdout. Using communicate()
        # would wait for pipe EOF from the child; our regular file does not.
        start = time.monotonic()
        r = runtime.bounded(['bash', '-c', 'sleep 2 & echo done'], 1, self.report, 'descendant')
        self.assertEqual(r['rc'], 0)
        self.assertIn('done', r['stdout'])
        self.assertLess(time.monotonic() - start, 1)
        try:
            os.killpg(r['pid'], 9)
        except ProcessLookupError:
            pass

    def test_term_ignored_escalates_without_waiting_forever(self):
        r = runtime.bounded(['bash', '-c', 'trap "" TERM; while :; do sleep 1; done'], .2, self.report, 'ignore TERM')
        self.assertEqual(r['rc'], 124)
        self.assertFalse(r['survivor'])

    def test_unkillable_worker_is_reported_not_waited(self):
        class Unkillable:
            pid = 99999999
            def poll(self): return None
            def wait(self, *a, **k): raise AssertionError('unbounded wait')
            def communicate(self, *a, **k): raise AssertionError('pipe wait')
        with mock.patch.object(runtime.subprocess, 'Popen', return_value=Unkillable()), \
             mock.patch.object(runtime.os, 'killpg'), mock.patch.object(runtime, 'birth', return_value='123'):
            start = time.monotonic()
            r = runtime.bounded(['mock-kernel-task'], .05, self.report, 'D-state simulation', mutation=True, diagnose=False)
        self.assertEqual(r['rc'], 124)
        self.assertTrue(r['survivor'])
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(list(self.report.root.glob('*.active.json')))

    def test_invalid_timeout(self):
        for value in ['0', '-1', 'nan', 'inf', '3601']:
            with self.assertRaises(Exception): runtime.positive(value)

    def test_report_permissions(self):
        r = runtime.bounded(['true'], 1, self.report, 'permissions')
        self.assertEqual(r['rc'], 0)
        self.assertEqual(self.report.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.report.root / 'events.jsonl').stat().st_mode & 0o777, 0o600)

    def test_exec_failure_is_not_hardware_timeout(self):
        r = runtime.bounded(['/no/such/executable'], .2, self.report, 'missing exec')
        self.assertEqual(r['rc'], 127)
        self.assertFalse(r['timed_out'])

    def test_supervisor_total_deadline_and_uncertainty_persists(self):
        script = self.root / 'blocked.sh'
        script.write_text('sleep 30\n')
        env = dict(os.environ, INCUS_GPU_SYSFS_ROOT=str(self.root / 'sys'),
                   INCUS_GPU_STATE_DIR=str(self.root / 'state'), INCUS_GPU_TOTAL_TIMEOUT='.2')
        env.pop('INCUS_GPU_RUN_DIR', None)
        cmd = [sys.executable, "-S", str(RUNTIME), 'supervise', str(script), 'attach']
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=4)
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertTrue((self.root / 'state/uncertain.json').exists())
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(r.returncode, 125)
        self.assertIn('previous PCI/Incus mutation is uncertain', r.stderr)

    def fixture(self, block=False):
        sysroot, procroot, binroot = [self.root / name for name in ('sys', 'proc', 'bin')]
        for p in (sysroot / 'bus/pci/devices', procroot / 'sys/kernel', binroot):
            p.mkdir(parents=True)
        (procroot / 'version').write_text('fixture kernel\n')
        (procroot / 'cmdline').write_text('fixture\n')
        (procroot / 'sys/kernel/tainted').write_text('0\n')
        for bdf, group, cls in [('0000:0a:00.0', '2', '0x030000'), ('0000:0a:00.1', '3', '0x040300')]:
            p = sysroot / 'bus/pci/devices' / bdf
            p.mkdir()
            g = sysroot / 'kernel/iommu_groups' / group / 'devices'
            g.mkdir(parents=True)
            (p / 'iommu_group').symlink_to(g.parent)
            (g / bdf).symlink_to(p)
            for field, val in {'class': cls, 'vendor': '0x1002', 'device': '0x7590',
                               'boot_vga': '0', 'driver_override': '(null)'}.items():
                (p / field).write_text(val + '\n')
        if block:
            p = sysroot / 'bus/pci/devices/0000:0a:00.0/driver_override'
            p.unlink(); os.mkfifo(p)
        log = self.root / 'incus-calls'
        incus = binroot / 'incus'
        incus.write_text('#!/usr/bin/env -S python3 -S\nimport json,sys\n'
                         + 'with open(' + repr(str(log)) + ',"a") as f: f.write(" ".join(sys.argv[1:])+"\\n")\n'
                         + 'print(json.dumps([{"name":"test-vm","status":"Running","type":"virtual-machine","devices":{}}]))\n')
        incus.chmod(0o755)
        lspci = binroot / 'lspci'; lspci.write_text('#!/bin/sh\necho fixture GPU\n'); lspci.chmod(0o755)
        env = dict(os.environ, INCUS_GPU_SYSFS_ROOT=str(sysroot), INCUS_GPU_PROC_ROOT=str(procroot),
                   INCUS_GPU_DEV_ROOT=str(self.root / 'dev'), INCUS_GPU_INCUS_BIN=str(incus),
                   INCUS_GPU_LSPCI_BIN=str(lspci), INCUS_GPU_RUN_DIR=str(self.report.root))
        return env, log, sysroot

    def test_inspect_is_readonly_with_blocked_read_and_keeps_evidence(self):
        env, log, sysroot = self.fixture(block=True)
        r = subprocess.run(['bash', str(TOOL), 'inspect', 'test-vm', '0000:0a:00.0',
                            '--project', 'app-deploy', '--timeout', '.2'], env=env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 124, r.stdout + r.stderr)
        self.assertNotRegex(log.read_text(), r'\b(stop|start|config)\b')
        summary = json.loads((self.report.root / 'summary.json').read_text())
        self.assertEqual(summary['vm_status'], 'Running')
        self.assertFalse(summary['passthrough_verified'])
        self.assertIn('0000:0a:00.1', summary['devices'])
        self.assertEqual(summary['devices']['0000:0a:00.1']['group'], '3')
        self.assertTrue((self.report.root / 'report.txt').exists())
        self.assertEqual(len(summary['timed_out_operations']), 1)

    def test_inspect_no_false_passthrough_pass(self):
        env, log, sysroot = self.fixture()
        r = subprocess.run(['bash', str(TOOL), 'inspect', 'test-vm', '0000:0a:00.0'], env=env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('result=COLLECTED', r.stdout)
        self.assertNotIn('result=PASS', r.stdout)
        self.assertNotRegex(log.read_text(), r'\b(stop|start|config)\b')

    def test_separate_iommu_groups_are_included_in_slot(self):
        env, _, _ = self.fixture()
        cmd = 'source "$1" --version >/dev/null; pci_slot_functions found 0000:0a:00.0; printf "%s\\n" "${found[@]}"'
        r = subprocess.run(['bash', '-c', cmd, 'test', str(TOOL)], env=env,
                           capture_output=True, text=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['0000:0a:00.0', '0000:0a:00.1'])

    def test_driver_override_write_actually_blocks(self):
        p = self.root / 'driver_override'
        os.mkfifo(p)
        r = runtime.bounded(runtime.command_worker('write', p, 'vfio-pci'), .2, self.report,
                            'driver_override write', mutation=True)
        self.assertEqual(r['rc'], 124)
        self.assertEqual(r['label'], 'driver_override write')

    def test_bind_and_restore_preserves_driver_and_different_override(self):
        env, _, sysroot = self.fixture()
        env['INCUS_GPU_IO_TIMEOUT'] = '1'
        drivers = sysroot / 'bus/pci/drivers'
        drivers.mkdir(parents=True)
        (sysroot / 'bus/pci/drivers_probe').write_text('')
        for drv in ('amdgpu', 'snd_hda_intel', 'vfio-pci'):
            (drivers / drv).mkdir()
            (drivers / drv / 'bind').touch()
            (drivers / drv / 'unbind').touch()
        p0 = sysroot / 'bus/pci/devices/0000:0a:00.0'
        p1 = sysroot / 'bus/pci/devices/0000:0a:00.1'
        (p0 / 'driver').symlink_to(drivers / 'amdgpu')
        (p1 / 'driver').symlink_to(drivers / 'snd_hda_intel')
        (p0 / 'driver_override').write_text('some-other-driver\n')
        code = r'''
source "$1" --version >/dev/null
runtime_exec() { return 0; }
sysfs_write_watchdog() {
    local value=$1 path=$2 drv
    case "$path" in
        */driver_override) printf '%s\n' "$value" > "$path" ;;
        */unbind) rm -f "$SYSFS_ROOT/bus/pci/devices/$value/driver" ;;
        */bind)
            drv=${path%/bind}
            ln -snf "$drv" "$SYSFS_ROOT/bus/pci/devices/$value/driver"
            ;;
        *) return 90 ;;
    esac
}
capture_prebind_state 0000:0a:00.0
prebind_vfio_functions 0000:0a:00.0 1
[[ $(pci_driver 0000:0a:00.0) == vfio-pci ]]
[[ $(pci_driver 0000:0a:00.1) == vfio-pci ]]
restore_prebound_vfio
[[ $(pci_driver 0000:0a:00.0) == amdgpu ]]
[[ $(pci_driver 0000:0a:00.1) == snd_hda_intel ]]
[[ $(pci_driver_override 0000:0a:00.0) == some-other-driver ]]
[[ $(pci_driver_override 0000:0a:00.1) == - ]]
'''
        r = subprocess.run(['bash', '-c', code, 'test', str(TOOL)], env=env,
                           text=True, capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_snapshot_of_second_function_fails_without_mutating_first(self):
        env, _, sysroot = self.fixture()
        env['INCUS_GPU_IO_TIMEOUT'] = '.2'
        (sysroot / 'bus/pci/drivers_probe').write_text('')
        p1 = sysroot / 'bus/pci/devices/0000:0a:00.1/driver_override'
        p1.unlink(); os.mkfifo(p1)
        code = r'''
source "$1" --version >/dev/null
sysfs_write_watchdog() { echo UNEXPECTED_MUTATION; return 99; }
capture_prebind_state 0000:0a:00.0
'''
        r = subprocess.run(['bash', '-c', code, 'test', str(TOOL)], env=env,
                           text=True, capture_output=True, timeout=5)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('UNEXPECTED_MUTATION', r.stdout)
        self.assertEqual((sysroot / 'bus/pci/devices/0000:0a:00.0/driver_override').read_text(), '(null)\n')

    def test_restore_failure_remains_failure(self):
        env, _, _ = self.fixture()
        code = r'''
source "$1" --version >/dev/null
PREBOUND_FUNCTIONS=(0000:0a:00.0)
PREBIND_ORIGINAL_DRIVER[0000:0a:00.0]=amdgpu
PREBIND_ORIGINAL_OVERRIDE[0000:0a:00.0]=-
restore_pci_function() { return 42; }
if restore_prebound_vfio; then exit 99; fi
[[ $PREBIND_RESTORE_FAILED == 1 ]]
[[ ${#PREBOUND_FUNCTIONS[@]} == 1 ]]
'''
        r = subprocess.run(['bash', '-c', code, 'test', str(TOOL)], env=env,
                           text=True, capture_output=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_staged_install_contains_runtime_and_inspect_works(self):
        env, _, _ = self.fixture()
        stage = self.root / 'installation'
        install_env = dict(env, DESTDIR=str(stage), PREFIX='/opt/incus-tools')
        r = subprocess.run(['bash', str(ROOT / 'install.sh')], env=install_env,
                           text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        installed = stage / 'opt/incus-tools/sbin/incus-gpu'
        self.assertTrue((stage / 'opt/incus-tools/libexec/incus-gpu/runtime.py').exists())
        r = subprocess.run([str(installed), 'inspect', 'test-vm', '0000:0a:00.0'], env=env,
                           text=True, capture_output=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


    def test_inspect_reconciles_only_completed_local_operation(self):
        env, _, _ = self.fixture()
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        env['INCUS_GPU_STATE_DIR'] = str(state)
        pending = state / 'uncertain.json'
        pending.write_text(json.dumps({'reason': {'pid': 99999999, 'starttime': 'old',
                                                'command': ['python3', '_worker', 'write']}}))
        r = subprocess.run(['bash', str(TOOL), 'inspect', 'test-vm', '0000:0a:00.0'], env=env,
                           text=True, capture_output=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(pending.exists())
        self.assertTrue(json.loads((self.report.root / 'summary.json').read_text())['previous_uncertainty_reconciled'])

if __name__ == '__main__':
    unittest.main()
