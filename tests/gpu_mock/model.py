"""Stateful userspace GPU/VFIO + Incus boundary model. Never accesses real PCI.

The compiled shim intercepts actual libc read/write calls ONLY under this
fixture's temporary sysfs root. No incus-gpu Bash/Python function is stubbed.
This is not a PCI device emulator, GPU instruction emulator, or real kernel.
"""
import copy
import errno
import json
import os
from pathlib import Path
import re
import socketserver
import threading
import time

GPU = '0000:0a:00.0'
AUDIO = '0000:0a:00.1'
VM = 'sb-436b44fcd4fe'
PROJECT = 'app-deploy'
BDF = re.compile(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]')
HERE = Path(__file__).resolve().parent


class Model:
    def __init__(self, root, *, gpu_driver='-', audio_driver='vfio-pci',
                 separate_group=False, vm_status='Running', existing=False):
        self.root = Path(root)
        self.sys = self.root / 'sys'
        self.proc = self.root / 'proc'
        self.dev = self.root / 'dev'
        self.socket = str(self.root / 'rpc.sock')
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.rules = []
        self.events = []
        self.inflight = {}
        self.devices = {}
        self.users = set()
        self.guest_devices = []
        self.clustered = False
        self.server_name = 'node1'
        self.instance = {'name': VM, 'type': 'virtual-machine', 'status': vm_status,
                         'location': 'node1', 'devices': {}, 'expanded_devices': {}}
        self.other_instances = []
        if existing:
            self.instance['devices']['gpu-existing'] = dict(type='gpu', gputype='physical', pci=GPU)
            self.instance['expanded_devices'] = copy.deepcopy(self.instance['devices'])
        for p in ('bus/pci/devices', 'kernel/iommu_groups', 'bus/pci/drivers',
                  'module/vfio_pci', 'module/vfio', 'module/vfio_iommu_type1'):
            (self.sys / p).mkdir(parents=True)
        for driver in ('vfio-pci', 'amdgpu', 'snd_hda_intel', 'pcieport', 'e1000e'):
            d = self.sys / 'bus/pci/drivers' / driver
            d.mkdir()
            (d / 'bind').touch(); (d / 'unbind').touch()
        (self.sys / 'bus/pci/drivers_probe').touch()
        for name in ('kvm', 'vfio/vfio'):
            p = self.dev / name; p.parent.mkdir(parents=True, exist_ok=True); p.touch()
        self.proc.mkdir()
        for name, value in {'version': 'GPU MOCK kernel (not a real PCI kernel)\n',
                            'cmdline': 'mock iommu=pt\n', 'cpuinfo': 'vendor_id : AuthenticAMD\n',
                            'modules': 'vfio_pci 65536 0 - Live 0x0\n',
                            'sys/kernel/tainted': '0\n'}.items():
            p = self.proc / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value)
        for name, cls, drv in [('0000:00:03.0', '0x060000', '-'),
                                ('0000:00:03.1', '0x060400', 'pcieport'),
                                ('0000:08:00.0', '0x060400', 'pcieport'),
                                ('0000:09:00.0', '0x060400', 'pcieport')]:
            self.add_device(name, cls, '2', drv)
        self.add_device(GPU, '0x030000', '2', gpu_driver, '0x7590')
        self.add_device(AUDIO, '0x040300', '3' if separate_group else '2', audio_driver, '0x1640')
        if audio_driver == 'vfio-pci':
            self.override(AUDIO, 'vfio-pci')
        if gpu_driver == 'vfio-pci':
            self.override(GPU, 'vfio-pci')
        if existing and vm_status == 'Running' and gpu_driver == 'vfio-pci' and audio_driver == 'vfio-pci':
            self.guest_devices = [GPU, AUDIO]
            self.users = {self.devices[b]['group'] for b in self.guest_devices}
        bindir = self.root / 'bin'; bindir.mkdir()
        for executable in ('incus', 'lspci', 'modprobe', 'fuser', 'journalctl', 'dmesg'):
            path = bindir / executable
            path.write_text('#!/usr/bin/env -S python3 -S\n' + (HERE / 'client.py').read_text().split('\n', 1)[1])
            path.chmod(0o755)
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith('INCUS_GPU_') or key.startswith('GPU_MOCK_') or key == 'LD_PRELOAD':
                self.env.pop(key)
        self.env.update(INCUS_GPU_SYSFS_ROOT=str(self.sys), INCUS_GPU_PROC_ROOT=str(self.proc),
                        INCUS_GPU_DEV_ROOT=str(self.dev), INCUS_GPU_ETC_ROOT=str(self.root / 'etc'),
                        INCUS_GPU_STATE_DIR=str(self.root / 'state'), INCUS_GPU_IO_TIMEOUT='1',
                        INCUS_GPU_TOTAL_TIMEOUT='35', INCUS_GPU_INCUS_BIN=str(bindir / 'incus'),
                        INCUS_GPU_LSPCI_BIN=str(bindir / 'lspci'),
                        GPU_MOCK_SYSFS=str(self.sys), GPU_MOCK_SOCKET=self.socket,
                        PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        self.events.clear()
        self._server = None

    def add_device(self, bdf, cls, group, driver, device='0x14da'):
        p = self.sys / 'bus/pci/devices' / bdf; p.mkdir()
        g = self.sys / 'kernel/iommu_groups' / group
        (g / 'devices').mkdir(parents=True, exist_ok=True)
        (g / 'type').write_text('DMA\n')
        (p / 'iommu_group').symlink_to(g)
        (g / 'devices' / bdf).symlink_to(p)
        vals = {'class': cls, 'vendor': '0x1002', 'device': device, 'boot_vga': '0',
                'driver_override': '(null)', 'revision': '0xc0', 'subsystem_vendor': '0x1002',
                'subsystem_device': '0x0000', 'enable': '1', 'numa_node': '0',
                'power_state': 'D0', 'd3cold_allowed': '1', 'reset_method': 'flr bus',
                'current_link_speed': '16.0 GT/s PCIe', 'current_link_width': '16',
                'max_link_speed': '32.0 GT/s PCIe', 'max_link_width': '16',
                'modalias': 'mock-pci-device', 'resource': '0x100000000 0x10fffffff 0x14220'}
        for field, value in vals.items():
            (p / field).write_text(value + '\n')
        (p / 'power').mkdir(); (p / 'power/control').write_text('auto\n')
        self.devices[bdf] = dict(driver='-', original=driver, override='', group=group, cls=cls)
        self.set_driver(bdf, driver)

    def override(self, bdf, value):
        self.devices[bdf]['override'] = value
        (self.sys / 'bus/pci/devices' / bdf / 'driver_override').write_text((value or '(null)') + '\n')

    def set_driver(self, bdf, driver):
        row = self.devices[bdf]
        p = self.sys / 'bus/pci/devices' / bdf
        old = row['driver']
        for link in (p / 'driver', self.sys / 'bus/pci/drivers' / old / bdf):
            if link.is_symlink(): link.unlink()
        vf = p / 'vfio-dev'
        if vf.exists():
            for child in vf.iterdir():
                node = self.dev / 'vfio/devices' / child.name
                node.unlink(missing_ok=True); child.unlink()
            vf.rmdir()
        row['driver'] = driver
        if driver != '-':
            d = self.sys / 'bus/pci/drivers' / driver
            (p / 'driver').symlink_to(d)
            (d / bdf).symlink_to(p)
        if driver == 'vfio-pci':
            vf.mkdir()
            number = int(bdf.rsplit('.', 1)[1])
            (vf / ('vfio%d' % number)).touch()
            node = self.dev / 'vfio/devices' / ('vfio%d' % number)
            node.parent.mkdir(exist_ok=True); node.touch()
        for group in {r['group'] for r in self.devices.values()}:
            node = self.dev / 'vfio' / group
            if any(r['group'] == group and r['driver'] == 'vfio-pci' for r in self.devices.values()):
                node.touch()
            else:
                node.unlink(missing_ok=True)

    def snapshot(self):
        with self.lock:
            return dict(devices=copy.deepcopy(self.devices), vm=copy.deepcopy(self.instance),
                        guest_devices=list(self.guest_devices), inflight=copy.deepcopy(self.inflight))

    def log(self, **record):
        with self.lock:
            record.update(seq=len(self.events), monotonic=time.monotonic())
            self.events.append(record)
            with open(self.root / 'model-events.jsonl', 'a') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def fault(self, op, match, *, action='error', error=errno.EINVAL, delay=0, nth=1, value=None):
        self.rules.append(dict(op=op, match=match, action=action, error=error,
                               delay=delay, nth=nth, seen=0, value=value))

    def select_fault(self, op, path, value):
        with self.lock:
            for rule in self.rules:
                if rule['op'] != op or rule['match'] not in path or (rule['value'] is not None and rule['value'] != value):
                    continue
                rule['seen'] += 1
                if rule['seen'] == rule['nth'] or rule['nth'] == 0:
                    return dict(rule)
        return None

    def handle(self, op, path, payload, pid):
        value = payload.decode('utf-8', 'replace').strip() if op != 'cli' else ''
        args = json.loads(payload) if op == 'cli' else None
        label = path + (' ' + ' '.join(args) if args else '')
        request = dict(op=op, path=path, value=value, pid=pid, args=args)
        self.log(event='request', **request)
        rule = self.select_fault(op, label, value)
        if rule:
            self.log(event='fault', rule=rule, **request)
            if rule['action'] in ('hold', 'delay', 'delay-error'):
                with self.lock:
                    self.inflight[pid] = dict(op=op, path=path, value=value)
                self.stop.wait(60 if rule['action'] == 'hold' else rule['delay'])
                with self.lock: self.inflight.pop(pid, None)
                if self.stop.is_set(): return errno.ECANCELED, None
            if rule['action'] in ('error', 'delay-error'):
                if op == 'cli':
                    return 0, dict(exit=42, stdout='', stderr='injected mock failure: ' + label + '\n')
                return rule['error'], None
            if rule['action'] == 'noop': return 0, None
        try:
            with self.lock:
                if op == 'read':
                    # The real libc read follows after this acknowledged request.
                    result = None
                elif op == 'write':
                    self.apply_write(path, value)
                    result = None
                elif op == 'cli':
                    result = self.cli(path, args)
                else:
                    raise OSError(errno.EINVAL, 'unknown operation')
            self.log(event='complete', **request)
            return 0, result
        except OSError as exc:
            self.log(event='rejected', errno=exc.errno, reason=str(exc), **request)
            if op == 'cli': return 0, dict(exit=1, stdout='', stderr=str(exc) + '\n')
            return exc.errno, None

    def apply_write(self, path, value):
        relative = str(Path(path).relative_to(self.sys))
        if relative.startswith('bus/pci/devices/') and relative.endswith('/driver_override'):
            bdf = relative.split('/')[3]
            self.override(bdf, value)
        elif relative == 'bus/pci/drivers_probe':
            row = self.devices[value]
            driver = row['override'] or (row['original'] if row['original'] not in ('-', 'vfio-pci') else ('amdgpu' if row['cls'].startswith('0x03') else 'snd_hda_intel'))
            self.bind(value, driver)
        elif relative.startswith('bus/pci/drivers/') and relative.rsplit('/', 1)[1] in ('bind', 'unbind'):
            driver, operation = relative.split('/')[-2:]
            if value not in self.devices: raise OSError(errno.ENODEV, value)
            if operation == 'bind':
                self.bind(value, driver)
            else:
                row = self.devices[value]
                if row['driver'] != driver: raise OSError(errno.ENODEV, 'wrong bound driver')
                if row['group'] in self.users: raise OSError(errno.EBUSY, 'VFIO group in use')
                self.set_driver(value, '-')
        else:
            raise OSError(errno.EPERM, 'unmodelled mutation rejected: ' + relative)
        self.log(event='state', path=relative, value=value, devices=copy.deepcopy(self.devices))

    def bind(self, bdf, driver):
        row = self.devices[bdf]
        if row['driver'] != '-': raise OSError(errno.EBUSY, 'device already has a driver')
        if row['override'] and row['override'] != driver:
            raise OSError(errno.ENODEV, 'driver_override does not match')
        if driver == 'pcieport' and row['cls'] != '0x060400':
            raise OSError(errno.ENODEV, 'class mismatch')
        if row['cls'].startswith('0x06'):
            raise OSError(errno.EPERM, 'test must not bind topology bridges')
        self.set_driver(bdf, driver)

    def cli(self, name, args):
        def ok(out=''): return dict(exit=0, stdout=out, stderr='')
        if name == 'modprobe': return ok()
        if name == 'fuser':
            node = Path(args[-1]).name
            return dict(exit=0 if node in self.users else 1, stdout='', stderr='')
        if name in ('journalctl', 'dmesg'): return ok('mock kernel log\n')
        if name == 'lspci':
            if '-s' in args:
                bdf = args[args.index('-s') + 1]
                if len(bdf) == 7: bdf = '0000:' + bdf
                if bdf not in self.devices: return dict(exit=1, stdout='', stderr='absent\n')
                row = self.devices[bdf]
                return ok('%s VGA compatible controller [0300]: AMD mock [1002:7590]\n\tKernel driver in use: %s\n' % (bdf, row['driver']))
            return ok('mock topology\n')
        if name != 'incus': raise OSError(errno.EINVAL, name)
        project = 'default'; a = []
        it = iter(args)
        for arg in it:
            if arg == '--force-local': continue
            if arg == '--project': project = next(it); continue
            a.append(arg)
        if a[0] == 'version': return ok('Client version: mock\nServer version: mock\n')
        if a[0] == 'query':
            if a[1] == '/1.0':
                return ok(json.dumps({'environment': {'server_clustered': self.clustered, 'server_name': self.server_name}}))
            if a[1].startswith('/1.0/operations'):
                return ok(json.dumps({'metadata': {'running': list(self.inflight.values())}}))
            return ok('{}')
        if project != PROJECT: raise OSError(errno.ENOENT, 'wrong project: ' + project)
        if a[0] == 'list': return ok(json.dumps([self.instance] + self.other_instances))
        if a[0] == 'info': return ok(json.dumps(self.instance))
        if a[0] == 'config':
            if a[1:2] == ['show']: return ok(json.dumps(self.instance))
            if a[1] != 'device': raise OSError(errno.EINVAL, 'unknown config')
            action, vm, device = a[2:5]
            if vm != VM: raise OSError(errno.ENOENT, vm)
            if self.instance['status'] == 'Running': raise OSError(errno.EBUSY, 'VM GPU is not hotpluggable')
            if action == 'add':
                if device in self.instance['devices']: raise OSError(errno.EEXIST, device)
                row = {'type': a[5]}
                row.update(x.split('=', 1) for x in a[6:])
                self.instance['devices'][device] = row
            elif action == 'remove':
                del self.instance['devices'][device]
            else: raise OSError(errno.EINVAL, action)
            self.instance['expanded_devices'] = copy.deepcopy(self.instance['devices'])
            self.log(event='vm-config', state=copy.deepcopy(self.instance))
            return ok()
        if a[0] == 'stop':
            if a[1] != VM: raise OSError(errno.ENOENT, a[1])
            self.instance['status'] = 'Stopped'; self.guest_devices = []
            self.users.clear()
            self.log(event='vm-stop')
            return ok()
        if a[0] == 'start':
            if a[1] != VM: raise OSError(errno.ENOENT, a[1])
            if self.instance['status'] == 'Running': raise OSError(errno.EBUSY, 'already running')
            guest = []
            for device in self.instance['devices'].values():
                if device['type'] != 'gpu': continue
                gpu = device['pci']
                functions = [b for b in self.devices if b.rsplit('.', 1)[0] == gpu.rsplit('.', 1)[0]]
                for bdf in functions:
                    row = self.devices[bdf]
                    # Model Incus's own GPU rebinding when prebind is not used.
                    if row['driver'] != 'vfio-pci':
                        self.override(bdf, 'vfio-pci'); self.set_driver(bdf, '-'); self.bind(bdf, 'vfio-pci')
                    for member in self.devices.values():
                        if member['group'] == row['group'] and not member['cls'].startswith('0x06') and member['driver'] not in ('-', 'vfio-pci'):
                            raise OSError(errno.EBUSY, 'IOMMU group is not viable')
                    guest.append(bdf)
            self.instance['status'] = 'Running'; self.guest_devices = guest
            for bdf in guest: self.users.add(self.devices[bdf]['group'])
            self.log(event='vm-start', guest_devices=guest)
            return ok()
        raise OSError(errno.EINVAL, 'unsupported incus command: ' + ' '.join(a))

    def start(self):
        model = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    op, path, size, pid = self.rfile.readline(8192).decode().rstrip('\n').split('\t')
                    n = int(size)
                    if n > 65536: return
                    err, result = model.handle(op, path, self.rfile.read(n), int(pid))
                    data = json.dumps(result).encode() if result is not None else b''
                    self.wfile.write(('%d %d\n' % (err, len(data))).encode() + data)
                except (BrokenPipeError, ConnectionResetError):
                    # Completion after client timeout is intentionally retained.
                    pass
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self._server = Server(self.socket, Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self.stop.set()
        if self._server:
            self._server.shutdown(); self._server.server_close(); self._thread.join(2)
