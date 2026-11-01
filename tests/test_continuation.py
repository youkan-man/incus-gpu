"""State-transition regressions; no host PCI or Incus mutations are performed."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'incus-gpu'


class AttachTransitions(unittest.TestCase):
    def run_case(self, args, existing=False, status='Stopped', failure=''):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'calls'
            env = dict(os.environ, TEST_LOG=str(log), TEST_EXISTING=str(int(existing)),
                       TEST_STATUS=status, TEST_FAIL=failure)
            # Exercise the actual attach orchestration; replace the hardware/daemon boundary only.
            code = r'''
source "$1" --version >/dev/null
shift
resolve_gpu() { echo 0000:0a:00.0; }
load_instances() { :; }
load_vm() { VM_TYPE=virtual-machine; VM_STATUS=$TEST_STATUS; VM_LOCATION=local; }
check_cluster_location() { :; }
check_other_assignments() {
    if [[ $TEST_EXISTING == 1 ]]; then printf 'vm\tgpu-existing\t%s\n' "$TEST_STATUS"; fi
    return 0
}
preflight_gpu() { :; }
choose_device_name() { echo gpu-new; }
capture_prebind_state() { echo snapshot >> "$TEST_LOG"; PREBIND_CAPTURED=1; }
prebind_vfio_functions() { echo prebind >> "$TEST_LOG"; return 0; }
restore_prebound_vfio() { echo restore >> "$TEST_LOG"; return 0; }
diagnose_start_failure() { :; }
record_operation_target() { :; }
incus_read() {
    echo "$*" >> "$TEST_LOG"
    if [[ $1 == start && -n $TEST_FAIL ]]; then return 42; fi
    if [[ $1 == config && $3 == remove && $TEST_FAIL == remove ]]; then return 43; fi
    return 0
}
cmd_attach vm 0000:0a:00.0 "$@"
'''
            result = subprocess.run(['bash', '-c', code, 'test', str(TOOL)] + args,
                                    env=env, text=True, capture_output=True, timeout=5)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_existing_stopped_start_is_not_skipped(self):
        result, calls = self.run_case(['--start'], existing=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('start vm', calls)
        self.assertFalse(any('config device add' in c for c in calls))

    def test_existing_running_restart_uses_existing_device(self):
        result, calls = self.run_case(['--restart', '--prebind-vfio'], existing=True, status='Running')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stop vm --timeout 60', calls)
        self.assertIn('prebind', calls)
        self.assertIn('start vm', calls)
        self.assertLess(calls.index('snapshot'), calls.index('stop vm --timeout 60'))
        self.assertFalse(any('config device add' in c for c in calls))

    def test_retry_restart_starts_a_previously_stopped_vm(self):
        result, calls = self.run_case(['--restart'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('start vm', calls)

    def test_existing_config_without_action_is_idempotent(self):
        result, calls = self.run_case([], existing=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_existing_running_start_does_not_stop_or_restart(self):
        result, calls = self.run_case(['--start'], existing=True, status='Running')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_existing_failure_does_not_remove_prior_configuration(self):
        result, calls = self.run_case(['--restart', '--prebind-vfio'], existing=True,
                                      status='Running', failure='start')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.count('start vm'), 1)
        self.assertIn('restore', calls)
        self.assertFalse(any('config device remove' in c for c in calls))

    def test_failed_config_removal_does_not_restart_bad_configuration(self):
        result, calls = self.run_case(['--restart'], status='Running', failure='remove')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('config device remove vm gpu-new', calls)
        self.assertEqual(calls.count('start vm'), 1, calls)


class ReleaseOwnership(unittest.TestCase):
    def run_release(self, second_group_result, cdev=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev = root / 'dev/vfio'; dev.mkdir(parents=True)
            for group in ('2', '3'):
                (dev / group).touch()
            sysroot = root / 'sys'
            # release-vfio now checks unbound functions for stale VFIO selection.
            # Supply real readable override attributes in this ownership fixture.
            for bdf in ('0000:0a:00.0', '0000:0a:00.1'):
                device = sysroot / 'bus/pci/devices' / bdf
                device.mkdir(parents=True, exist_ok=True)
                (device / 'driver_override').write_text('vfio-pci\n')
            if cdev:
                (dev / '3').unlink()
                cdevpath = sysroot / 'bus/pci/devices/0000:0a:00.1/vfio-dev/vfio44'
                cdevpath.mkdir(parents=True)
                (dev / 'devices').mkdir()
                (dev / 'devices/vfio44').touch()
            log = root / 'calls'
            env = dict(os.environ, INCUS_GPU_SYSFS_ROOT=str(sysroot),
                       INCUS_GPU_DEV_ROOT=str(root / 'dev'), TEST_LOG=str(log),
                       TEST_FUSER_RC=str(second_group_result))
            code = r'''
source "$1" --version >/dev/null
FORCE=1
require_sysfs_write_access() { return 0; }
pci_slot_functions() { local -n result=$1; result=(0000:0a:00.0 0000:0a:00.1); }
pci_iommu_group() { [[ $1 == *.0 ]] && echo 2 || echo 3; }
pci_driver() { echo vfio-pci; }
print_vfio_state() { :; }
runtime_exec() {
    echo "check $1" >> "$TEST_LOG"
    [[ $1 == *3 || $1 == *vfio44 ]] && return "$TEST_FUSER_RC"
    return 1
}
unbind_pci_function() { echo "UNBIND $1" >> "$TEST_LOG"; }
sysfs_write_watchdog() { echo WRITE >> "$TEST_LOG"; }
release_vfio_functions 0000:0a:00.0
'''
            result = subprocess.run(['bash', '-c', code, 'test', str(TOOL)], env=env,
                                    capture_output=True, text=True, timeout=5)
            return result, log.read_text() if log.exists() else ''

    def test_busy_sibling_group_blocks_before_any_unbind(self):
        result, log = self.run_release(0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('check VFIO users 3', log)
        self.assertNotIn('UNBIND', log)

    def test_failed_ownership_check_is_not_treated_as_unused(self):
        result, log = self.run_release(124)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('UNBIND', log)


    def test_cdev_user_without_group_fd_is_detected(self):
        result, log = self.run_release(0, cdev=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('check VFIO users vfio44', log)
        self.assertNotIn('UNBIND', log)

    def test_unused_slot_can_be_released(self):
        result, log = self.run_release(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('UNBIND 0000:0a:00.0', log)
        self.assertIn('UNBIND 0000:0a:00.1', log)


import test_runtime as shared


class DiagnosticContinuation(unittest.TestCase):
    fixture = shared.RuntimeTests.fixture

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.report = shared.runtime.Report(self.root / 'report')

    def tearDown(self):
        self.tmp.cleanup()

    def inspect(self, env, vm='test-vm', gpu='0000:0a:00.0'):
        result = subprocess.run(['bash', str(TOOL), 'inspect', vm, gpu], env=env,
                                capture_output=True, text=True, timeout=20)
        return result, json.loads((self.report.root / 'summary.json').read_text())

    def test_unknown_gpu_is_not_collected_successfully(self):
        env, log, _ = self.fixture()
        result, summary = self.inspect(env, gpu='0000:ff:00.0')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(summary['result'], 'INVALID_TARGET')
        self.assertIn('GPU_NOT_FOUND', [x['code'] for x in summary['findings']])
        self.assertNotRegex(log.read_text(), r'\b(stop|start|config)\b')

    def test_wrong_vm_is_not_collected_successfully(self):
        env, log, _ = self.fixture()
        result, summary = self.inspect(env, vm='missing-vm')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(summary['vm_status'], 'unknown')
        self.assertEqual(summary['result'], 'INVALID_TARGET')
        self.assertNotRegex(log.read_text(), r'\b(stop|start|config)\b')

    def test_inspecting_different_project_cannot_clear_uncertainty(self):
        env, _, _ = self.fixture()
        state = self.root / 'state'; state.mkdir(mode=0o700)
        env['INCUS_GPU_STATE_DIR'] = str(state)
        marker = state / 'uncertain.json'
        data = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'reason': {'pid': 99999999, 'starttime': '123',
                           'command': ['python3', '_worker', 'write'],
                           'target': {'vm': 'test-vm', 'project': 'other-project',
                                      'gpu': '0000:0a:00.0',
                                      'sysfs_root': env['INCUS_GPU_SYSFS_ROOT']}}}
        marker.write_text(json.dumps(data))
        result, summary = self.inspect(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(summary['previous_uncertainty_reconciled'])
        self.assertEqual(json.loads(marker.read_text()), data)

    def test_target_match_requires_same_gpu_and_host(self):
        a = {'gpu': '0000:0a:00.0', 'vm': 'vm', 'project': 'p', 'sysfs_root': '/sys'}
        self.assertTrue(shared.runtime.same_target(a, a))
        for key, value in [('gpu', '0000:0b:00.0'), ('vm', 'other'),
                           ('project', 'other'), ('sysfs_root', '/different-host')]:
            self.assertFalse(shared.runtime.same_target(a, dict(a, **{key: value})))
        self.assertFalse(shared.runtime.same_target(None, a))

    def test_missing_boot_identity_is_not_a_reboot(self):
        state = self.root / 'state'; state.mkdir(mode=0o700)
        marker = state / 'uncertain.json'; marker.write_text('{}')
        script = self.root / 'noop.sh'; script.write_text('echo UNEXPECTED_OPERATION\n')
        env = dict(os.environ, INCUS_GPU_STATE_DIR=str(state),
                   INCUS_GPU_SYSFS_ROOT=str(self.root / 'sys'))
        env.pop('INCUS_GPU_RUN_DIR', None)
        result = subprocess.run([__import__('sys').executable, '-S', str(shared.RUNTIME),
                                 'supervise', str(script), 'attach'], env=env,
                                capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertTrue(marker.exists())
        self.assertNotIn('UNEXPECTED_OPERATION', result.stdout)

    def test_snapshot_is_saved_before_mutation(self):
        env, _, sysroot = self.fixture()
        (sysroot / 'bus/pci/drivers_probe').touch()
        code = r"""
source "$1" --version >/dev/null
PROJECT=app-deploy
record_operation_target test-vm 0000:0a:00.0
capture_prebind_state 0000:0a:00.0
"""
        result = subprocess.run(['bash', '-c', code, 'test', str(TOOL)], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads((self.report.root / 'pci-snapshot.json').read_text())
        self.assertEqual(set(snapshot['devices']), {'0000:0a:00.0', '0000:0a:00.1'})
        self.assertEqual(snapshot['devices']['0000:0a:00.0']['driver_override'], '-')
        self.assertEqual(snapshot['devices']['0000:0a:00.0']['driver'], '-')
        target = json.loads((self.report.root / 'operation-target.json').read_text())
        self.assertEqual(target['project'], 'app-deploy')
        self.report.fence({'label': 'simulated uncertainty'})
        self.assertEqual(json.loads((self.report.root / 'unsafe.json').read_text())['target'], target)

    def test_blocked_thread_found_when_daemon_leader_is_sleeping(self):
        proc = self.root / 'proc'; proc.mkdir()
        entry = proc / '1234'; (entry / 'task').mkdir(parents=True)
        (entry / 'cmdline').write_bytes(b'/usr/bin/incusd\0')
        def make_stat(path, pid, state):
            path.write_text('%s (incus worker) %s\n' % (pid, ' '.join([state] + ['0']*18 + ['12345'])))
        make_stat(entry / 'stat', 1234, 'S')
        for tid, state in [(1234, 'S'), (1235, 'D')]:
            task = entry / 'task' / str(tid); task.mkdir()
            (task / 'status').write_text('State:\t%s (fixture)\n' % state)
            make_stat(task / 'stat', tid, state)
        rows = shared.runtime.scan_processes(proc, set())
        self.assertEqual(rows[0]['tid'], 1235)
        self.assertEqual(rows[0]['pid'], 1234)
        self.assertEqual(rows[0]['state'], 'D')
        self.assertEqual(rows[0]['proc_path'], str(entry / 'task/1235'))
        self.assertEqual(rows[0]['thread_starttime'], '12345')
        self.assertEqual(shared.runtime.scan_processes(proc, {1234}), [])

    def test_foreign_endpoint_in_audio_group_detected_before_mutation(self):
        env, _, sysroot = self.fixture()
        endpoint = sysroot / 'bus/pci/devices/0000:0c:00.0'
        endpoint.mkdir()
        (endpoint / 'class').write_text('0x020000\n')
        (sysroot / 'kernel/iommu_groups/3/devices/0000:0c:00.0').symlink_to(endpoint)
        code = 'source "$1" --version >/dev/null; preflight_gpu 0000:0a:00.0'
        result = subprocess.run(['bash', '-c', code, 'test', str(TOOL)], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('0000:0c:00.0(class=0x020000', result.stderr)

if __name__ == '__main__':
    unittest.main()
