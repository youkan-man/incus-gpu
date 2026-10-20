#!/usr/bin/env python3
"""Bounded Linux I/O and evidence collection for incus-gpu (Python >= 3.8).

No thread timeout, PIPE communicate(), or unbounded wait() is used. A worker
blocked in an uninterruptible syscall may outlive the CLI; this is reported,
not described as a cancelled kernel operation. Reports are private local files.
"""
import argparse
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = "0.2.0"
SELF = str(Path(__file__).resolve())
LIMIT = 2 * 1024 * 1024


def positive(value):
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 3600:
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 3600]")
    return number


def text(path, limit=LIMIT):
    with open(path, "rb", buffering=0) as handle:
        return handle.read(limit).decode("utf-8", "replace")


def atomic_json(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with open(temporary, "x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(str(temporary), str(path))


def birth(pid):
    try:
        # /proc/PID/stat contains a parenthesized comm that may include spaces.
        return text("/proc/%d/stat" % pid, 8192).rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def alive(pid, started):
    return started is not None and birth(pid) == started


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise RuntimeError("unsafe runtime directory: " + str(path))
    return path


class Report:
    def __init__(self, root=None):
        if root is None:
            root = os.environ.get("INCUS_GPU_RUN_DIR")
        self.root = private_dir(Path(root)) if root else Path(tempfile.mkdtemp(prefix="incus-gpu-"))
        os.chmod(str(self.root), 0o700)

    def event(self, record):
        record = dict(record, time=time.time())
        raw = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        fd = os.open(str(self.root / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)

    def fence(self, reason):
        atomic_json(self.root / "unsafe.json", reason)


def worker(mode, arguments):
    try:
        if mode == "read":
            # Open/read occurs only in the disposable worker, never the supervisor.
            sys.stdout.write(text(arguments[0]))
        elif mode == "write":
            # Do not create missing sysfs paths, unlike shell redirection.
            fd = os.open(arguments[0], os.O_WRONLY | os.O_TRUNC)
            try:
                data = (arguments[1] + "\n").encode()
                if os.write(fd, data) != len(data):
                    raise OSError(errno.EIO, "short sysfs write")
            finally:
                os.close(fd)
        elif mode == "processes":
            excluded = set(map(int, arguments)) | {os.getpid()}
            rows = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit() or int(entry.name) in excluded:
                    continue
                try:
                    status = text(entry / "status", 16384)
                    state = re.search(r"^State:\s+(\S+)", status, re.M).group(1)
                    cmd = text(entry / "cmdline", 8192).replace("\0", " ")
                    if state == "D" or "incus-gpu" in cmd or "vfio" in cmd:
                        rows.append({"pid": int(entry.name), "state": state,
                                     "command": cmd[:2048], "starttime": birth(int(entry.name))})
                except (OSError, AttributeError):
                    continue
                if len(rows) >= 96:
                    break
            print(json.dumps(rows))
        else:
            raise ValueError("unknown worker")
        return 0
    except OSError as exc:
        print("errno=%s (%s): %s" % (exc.errno, errno.errorcode.get(exc.errno, "UNKNOWN"), exc), file=sys.stderr)
        return 1


def command_worker(mode, *args):
    return [sys.executable, "-S", SELF, "_worker", mode] + list(map(str, args))


def bounded(argv, timeout, report, label, mutation=False, stream=False, diagnose=True):
    """Run one phase; never wait for a killed-but-still-running kernel task."""
    token = uuid.uuid4().hex
    out_path, err_path = report.root / (token + ".out"), report.root / (token + ".err")
    active_path = report.root / (token + ".active.json")
    start = time.monotonic()
    offsets = [0, 0]
    info = {"label": label, "command": argv, "mutation": mutation,
            "stdout_file": out_path.name, "stderr_file": err_path.name}
    inherited = os.environ.get("INCUS_GPU_LOCK_FD")
    passed = (int(inherited),) if inherited and inherited.isdigit() else ()
    if mutation and (report.root / "unsafe.json").exists():
        return dict(info, rc=125, timed_out=False, blocked=True, stdout="", stderr="previous operation is uncertain")
    with open(out_path, "xb", buffering=0) as out, open(err_path, "xb", buffering=0) as err:
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                     start_new_session=True, close_fds=True, pass_fds=passed)
        except OSError as exc:
            result = dict(info, rc=127, timed_out=False, stdout="", stderr=str(exc))
            report.event(result)
            return result
        info.update(pid=child.pid, starttime=birth(child.pid))
        atomic_json(active_path, info)
        report.event(dict(info, event="start"))
        if stream:
            print("[PHASE] " + label, file=sys.stderr, flush=True)
        timed_out, interrupted = False, False
        try:
            while child.poll() is None:
                if stream:
                    for index, path in enumerate((out_path, err_path)):
                        with open(path, "rb") as src:
                            src.seek(offsets[index]); chunk = src.read(65536)
                            offsets[index] += len(chunk)
                        if chunk:
                            destination = sys.stdout if index == 0 else sys.stderr
                            destination.write(chunk.decode("utf-8", "replace")); destination.flush()
                if time.monotonic() - start >= timeout:
                    timed_out = True
                    break
                time.sleep(0.025)
        except KeyboardInterrupt:
            interrupted = True
        snapshot = {}
        if timed_out or interrupted:
            # Get the stack BEFORE signals, including killable lock waits.
            if diagnose:
                for field in ("status", "wchan", "syscall", "stack"):
                    res = bounded(command_worker("read", "/proc/%d/%s" % (child.pid, field)),
                                  0.4, report, "worker-" + field, diagnose=False)
                    snapshot[field] = res["stdout"] or res["stderr"] or "unavailable"
            # Do not reap the leader between signals: its PID must remain
            # reserved even if TERM exits the leader before its descendants.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    time.sleep(0.15)
            deadline = time.monotonic() + 0.25
            while child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
        rc = child.poll()
        survivor = rc is None
        result = dict(info, rc=130 if interrupted else 124 if timed_out else rc,
                      timed_out=timed_out, interrupted=interrupted, survivor=survivor,
                      elapsed=round(time.monotonic() - start, 3), process=snapshot)
        if (timed_out or interrupted) and mutation:
            report.fence(result)
        if not survivor:
            active_path.unlink(missing_ok=True)
        # If alive, leave its identity and evidence. Do not wait()/communicate().
    for index, path in enumerate((out_path, err_path)):
        data = text(path)
        result["stdout" if index == 0 else "stderr"] = data
        if stream:
            with open(path, "rb") as src:
                src.seek(offsets[index]); rest = src.read(LIMIT)
            destination = sys.stdout if index == 0 else sys.stderr
            destination.write(rest.decode("utf-8", "replace")); destination.flush()
    report.event({k: v for k, v in result.items() if k not in ("stdout", "stderr")})
    if timed_out or interrupted:
        print("[WARN] %s: %s pid=%d (kernel operation cancellation is NOT guaranteed)" %
              (label, "timeout" if timed_out else "interrupted", child.pid), file=sys.stderr, flush=True)
    return result


def ancestors():
    result = {os.getpid()}
    pid = os.getppid()
    while pid > 0 and pid not in result:
        result.add(pid)
        try:
            match = re.search(r"^PPid:\s+(\d+)", text("/proc/%d/status" % pid, 8192), re.M)
            pid = int(match.group(1)) if match else 0
        except OSError:
            break
    return result


def cleanup_owned(report):
    for path in report.root.glob("*.active.json"):
        try:
            record = json.loads(text(path))
            if alive(record["pid"], record["starttime"]):
                os.killpg(record["pid"], signal.SIGKILL)
        except (OSError, KeyError, ValueError):
            continue


def state_directory():
    custom = os.environ.get("INCUS_GPU_STATE_DIR")
    if custom:
        return private_dir(Path(custom))
    if os.environ.get("INCUS_GPU_SYSFS_ROOT", "/sys") != "/sys":
        return private_dir(Path(os.environ["INCUS_GPU_SYSFS_ROOT"]).parent / "runtime-state")
    return private_dir(Path("/run/incus-gpu") if os.geteuid() == 0 else Path(tempfile.gettempdir()) / ("incus-gpu-state-%d" % os.geteuid()))


def supervise(script, args):
    for index, arg in enumerate(args):
        if arg in ("--timeout", "--vfio-timeout", "--stop-timeout"):
            if index + 1 == len(args):
                raise ValueError(arg + " requires a value")
            positive(args[index + 1])
    report = Report()
    env_run = str(report.root)
    os.environ["INCUS_GPU_RUN_DIR"] = env_run
    os.environ["INCUS_GPU_SUPERVISED"] = "1"
    print("[INFO] incus-gpu %s / report=%s" % (VERSION, env_run), file=sys.stderr, flush=True)
    readonly = args and args[0] in ("list", "ls", "doctor", "check", "status", "show")
    dry = "--dry-run" in args
    lock = None
    try:
        if not readonly and not dry:
            state = state_directory()
            lock = open(state / "operation.lock", "a+")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("[FAIL] another incus-gpu PCI operation is still active; use inspect for evidence", file=sys.stderr)
                return 125
            os.environ["INCUS_GPU_LOCK_FD"] = str(lock.fileno())
            if os.environ.get("INCUS_GPU_SYSFS_ROOT", "/sys") == "/sys":
                existing = bounded(command_worker("processes", *ancestors()), 3, report, "existing processes")
                try:
                    rows = json.loads(existing["stdout"])
                except ValueError:
                    rows = []
                conflicts = [row for row in rows if "incus-gpu" in row["command"]
                             and alive(row["pid"], row["starttime"])]
                if conflicts:
                    atomic_json(report.root / "existing-processes.json", conflicts)
                    print("[FAIL] an earlier incus-gpu process is still active; inspect will collect its stack", file=sys.stderr)
                    return 125
                if existing["rc"] != 0:
                    print("[FAIL] cannot verify existing operation state", file=sys.stderr)
                    return 125
            # A timeout is not proof that a remote Incus operation was cancelled.
            pending = state / "uncertain.json"
            if pending.exists():
                try:
                    old = json.loads(text(pending))
                    boot = text("/proc/sys/kernel/random/boot_id").strip()
                    if old.get("boot_id") == boot:
                        print("[FAIL] previous PCI/Incus mutation is uncertain; inspect report: " + old.get("report", "unknown"), file=sys.stderr)
                        return 125
                    pending.unlink()
                except (OSError, ValueError):
                    print("[FAIL] cannot verify previous mutation state", file=sys.stderr)
                    return 125
        budget = positive(os.environ.get("INCUS_GPU_TOTAL_TIMEOUT", "180"))
        result = bounded(["bash", script] + args, budget, report, "incus-gpu " + (args[0] if args else ""), stream=True)
        if result.get("timed_out") or result.get("interrupted"):
            cleanup_owned(report)
            if not readonly and not dry:
                report.fence(result)
        if (report.root / "unsafe.json").exists() and lock is not None:
            atomic_json(state_directory() / "uncertain.json", {
                "report": env_run, "boot_id": text("/proc/sys/kernel/random/boot_id").strip(),
                "reason": json.loads(text(report.root / "unsafe.json"))})
        uncertain = (report.root / "unsafe.json").exists()
        exit_code = 130 if result.get("interrupted") else 124 if uncertain else result["rc"]
        summary = {"version": VERSION, "command": args, "exit": exit_code,
                   "child_exit": result["rc"],
                   "result": "UNCERTAIN" if uncertain else "TIMEOUT" if result["timed_out"] else "OK" if result["rc"] == 0 else "FAIL",
                   "report": env_run, "unsafe": uncertain}
        atomic_json(report.root / "operation-summary.json", summary)
        print("[INFO] report=%s exit=%s" % (env_run, result["rc"]), file=sys.stderr)
        return exit_code
    finally:
        if lock is not None:
            lock.close()


def inspect(script, arguments):
    parser = argparse.ArgumentParser(prog="incus-gpu inspect")
    parser.add_argument("vm")
    parser.add_argument("gpu")
    parser.add_argument("--project", default=os.environ.get("INCUS_PROJECT", "default"))
    parser.add_argument("--timeout", "--vfio-timeout", type=positive, default=3.0)
    parser.add_argument("--probe", action="store_true", help="explicitly stop VM and test VFIO binding")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-stop", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(arguments)
    report = Report()
    os.environ["INCUS_GPU_RUN_DIR"] = str(report.root)
    print("=== incus-gpu inspect %s (read-only evidence collection) ===" % VERSION, flush=True)
    print("report=%s" % report.root, flush=True)
    sysroot = Path(os.environ.get("INCUS_GPU_SYSFS_ROOT", "/sys"))
    procroot = Path(os.environ.get("INCUS_GPU_PROC_ROOT", "/proc"))
    devroot = Path(os.environ.get("INCUS_GPU_DEV_ROOT", "/dev"))
    deadline = time.monotonic() + 75
    results, notes, cache = [], [], {}
    cancelled = False

    def run(label, argv):
        nonlocal cancelled, deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            res = {"label": label, "rc": 124, "timed_out": True, "stdout": "", "stderr": "collection budget exhausted"}
        else:
            res = bounded(argv, min(options.timeout, remaining), report, label)
        if res.get("interrupted"):
            cancelled = True
            deadline = 0
        results.append(res)
        print("[%s] %s" % ("OK" if res["rc"] == 0 else "TIMEOUT" if res.get("timed_out") else "UNAVAILABLE", label), flush=True)
        return res

    def read(path):
        key = str(path)
        if key not in cache:
            cache[key] = run("read " + key, command_worker("read", key))
        return cache[key]["stdout"].strip() if cache[key]["rc"] == 0 else None

    processes = []
    if str(sysroot) == "/sys" and str(procroot) == "/proc":
        res = run("existing PCI-related processes", command_worker("processes", *ancestors()))
        try:
            processes = json.loads(res["stdout"])
        except ValueError:
            pass
        for proc in processes[:24]:
            for field in ("status", "wchan", "syscall", "stack"):
                read(procroot / str(proc["pid"]) / field)
    read(procroot / "version")
    read(procroot / "cmdline")
    read(procroot / "sys/kernel/tainted")
    bdf = options.gpu.lower()
    if re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", bdf):
        bdf = "0000:" + bdf
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", bdf):
        inventory = []
        for path in sorted((sysroot / "bus/pci/devices").glob("*")):
            value = read(path / "class")
            if value and value.lower().startswith("0x03"):
                inventory.append(path.name)
        if bdf.isdecimal() and 1 <= int(bdf) <= len(inventory):
            bdf = inventory[int(bdf)-1]
        else:
            notes.append("GPU selector is not an unambiguous BDF/index; no PCI mutation attempted")
            bdf = ""
    functions, groups, device_states = [], set(), {}
    if bdf:
        functions = sorted((sysroot / "bus/pci/devices").glob(bdf.rsplit(".", 1)[0] + ".*"))
        for path in functions:
            # Enumerate slot functions independently of IOMMU groups.
            driver = os.path.basename(os.readlink(str(path / "driver"))) if (path / "driver").is_symlink() else "-"
            group = os.path.basename(os.readlink(str(path / "iommu_group"))) if (path / "iommu_group").is_symlink() else "-"
            groups.add(group)
            entry = {"driver": driver, "group": group}
            for field in ("vendor", "device", "subsystem_vendor", "subsystem_device", "class",
                          "boot_vga", "driver_override", "power_state", "power/control", "power/runtime_status",
                          "d3cold_allowed", "reset_method", "resource", "current_link_speed", "current_link_width"):
                entry[field] = read(path / field)
            device_states[path.name] = entry
            run("lspci " + path.name, [os.environ.get("INCUS_GPU_LSPCI_BIN", "lspci"), "-Dvvnnk", "-s", path.name])
        for group in sorted(groups - {"-"}):
            for member in sorted((sysroot / "kernel/iommu_groups" / group / "devices").glob("*")):
                read(member / "class")
            node = devroot / "vfio" / group
            if node.exists():
                run("VFIO group users " + group, ["fuser", "-v", str(node)])
        notes.append("A VFIO group node or bound audio function does not prove that the GPU itself is bound")
    incus = os.environ.get("INCUS_GPU_INCUS_BIN", "incus")
    base = [incus, "--force-local", "--project", options.project]
    run("Incus version", base + ["version"])
    vmres = run("VM state", base + ["list", options.vm, "--format=json"])
    vmstate = "unknown"
    try:
        data = json.loads(vmres["stdout"])
        if isinstance(data, dict):
            data = data["metadata"]
        vmstate = next(item["status"] for item in data if item["name"] == options.vm)
    except (ValueError, KeyError, StopIteration, TypeError):
        notes.append("VM state could not be verified")
    run("Incus VM log", base + ["info", options.vm, "--show-log"])
    operations = run("Incus operations", base + ["query", "/1.0/operations?recursion=1"])
    remote_idle = False
    try:
        ops = json.loads(operations["stdout"])
        if isinstance(ops, dict):
            ops = ops.get("metadata", ops)
        allowed = {"running", "pending", "success", "failure", "cancelled"}
        remote_idle = isinstance(ops, dict) and set(ops).issubset(allowed) and not ops.get("running") and not ops.get("pending")
    except (ValueError, TypeError):
        pass
    if str(sysroot) == "/sys" and str(procroot) == "/proc":
        for field in ("board_vendor", "board_name", "bios_version", "bios_date"):
            read(sysroot / "class/dmi/id" / field)
        # Keep whole bounded log including Call Trace, not just grep matches.
        kres = run("kernel log", ["journalctl", "-k", "-b", "-n", "2000", "--no-pager", "-o", "short-monotonic"])
        if kres["rc"] != 0 or not kres["stdout"].strip() or kres["stdout"].strip() == "-- No entries --":
            run("kernel ring buffer", ["dmesg", "--color=never"])
    timeouts = [r["label"] for r in results if r.get("timed_out")]
    # Reconcile only our own uncertainty marker, not hardware or VM settings.
    # A fresh explicit operation will take a new snapshot after reconciliation.
    reconciled = False
    state = state_directory()
    pending = state / "uncertain.json"
    if pending.exists() and not timeouts and vmstate != "unknown" and device_states and all(
            item.get("driver_override") is not None for item in device_states.values()):
        with open(state / "operation.lock", "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                old = json.loads(text(pending))
                reason = old.get("reason", {})
                local_io = "_worker" in reason.get("command", [])
                still_alive = alive(reason.get("pid", -1), reason.get("starttime"))
                conflicting = any("incus-gpu" in row["command"] and alive(row["pid"], row["starttime"]) for row in processes)
                if not still_alive and not conflicting and (local_io or remote_idle):
                    pending.unlink()
                    reconciled = True
            except (OSError, ValueError):
                pass
    findings = []
    target = device_states.get(bdf, {})
    if bdf and not functions:
        findings.append({"code": "GPU_NOT_FOUND", "evidence": bdf})
    if target and target.get("driver") != "vfio-pci":
        findings.append({"code": "GPU_NOT_BOUND", "evidence": "driver=" + target["driver"]})
    if target and target.get("driver") != "vfio-pci" and any(
            item["driver"] == "vfio-pci" for name, item in device_states.items() if name != bdf):
        findings.append({"code": "PARTIAL_BINDING", "evidence": "a sibling is bound but the target GPU is not"})
    if timeouts:
        findings.append({"code": "IO_TIMEOUT", "evidence": timeouts})
    blocked = [row["pid"] for row in processes if row["state"] == "D"]
    if blocked:
        findings.append({"code": "D_STATE_OBSERVED", "evidence": blocked,
                         "note": "GPU correlation must be checked in the saved stack; D alone is not a root cause"})
    kernel_text = "\n".join(r["stdout"] for r in results if r["label"] in ("kernel log", "kernel ring buffer"))
    if re.search(r"BUG:|Oops:|general protection fault|Call Trace:", kernel_text):
        findings.append({"code": "KERNEL_TRACE_PRESENT", "evidence": "see complete kernel log in report.txt"})
    summary = {"version": VERSION, "result": "INTERRUPTED" if cancelled else "INCOMPLETE" if timeouts else "COLLECTED",
               "tool_path": script, "runtime_path": SELF, "findings": findings,
               "read_only": not options.probe, "gpu": bdf, "vm": options.vm,
               "project": options.project, "vm_status": vmstate, "devices": device_states,
               "processes": processes, "timed_out_operations": timeouts, "notes": notes,
               "passthrough_verified": False, "previous_uncertainty_reconciled": reconciled,
               "report": str(report.root)}
    atomic_json(report.root / "summary.json", summary)
    with open(report.root / "report.txt", "w", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        for result in results:
            handle.write("\n--- %s (exit=%s) ---\n" % (result["label"], result["rc"]))
            handle.write(result["stdout"] + result["stderr"] + "\n")
            if result.get("process"):
                handle.write("Worker at timeout:\n" + json.dumps(result["process"], ensure_ascii=False, indent=2) + "\n")
    print("=== inspection summary ===\nresult=%s\nvm=%s status=%s\ngpu=%s\nreport=%s/report.txt" %
          (summary["result"], options.vm, vmstate, bdf, report.root), flush=True)
    for finding in findings:
        print("finding=%s evidence=%s" % (finding["code"], finding["evidence"]), flush=True)
    if cancelled:
        return 130
    if options.probe:
        if timeouts or any(p["state"] == "D" or "incus-gpu" in p["command"] for p in processes):
            print("[FAIL] outstanding I/O or another tool process; active probe skipped", file=sys.stderr)
            return 124
        args = ["_inspect-probe", options.vm, bdf, "--project", options.project,
                "--timeout", str(max(1, int(options.timeout))) ]
        for flag in ("force", "force_stop", "dry_run"):
            if getattr(options, flag):
                args.append("--" + flag.replace("_", "-"))
        return supervise(script, args)
    return 124 if timeouts else 0


def main():
    os.umask(0o077)
    mode = sys.argv[1]
    if mode == "_worker":
        return worker(sys.argv[2], sys.argv[3:])
    if mode == "supervise":
        return supervise(sys.argv[2], sys.argv[3:])
    if mode == "inspect":
        return inspect(sys.argv[2], sys.argv[3:])
    if mode in ("read", "write", "exec"):
        timeout, label = positive(sys.argv[2]), sys.argv[3]
        report = Report()
        args = sys.argv[4:]
        mutation = mode == "write" or (mode == "exec" and args[0] == "--mutation")
        if mode == "exec":
            if args and args[0] in ("--mutation", "--readonly"):
                args = args[1:]
            argv = args
        else:
            argv = command_worker(mode, *args)
        result = bounded(argv, timeout, report, label, mutation=mutation)
        sys.stdout.write(result["stdout"])
        sys.stderr.write(result["stderr"])
        if result.get("timed_out"):
            print("[FAIL] %s: timeout; report=%s" % (label, report.root), file=sys.stderr)
        return result["rc"]
    raise ValueError("unknown runtime command")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, argparse.ArgumentTypeError) as exc:
        print("[FAIL] " + str(exc), file=sys.stderr)
        sys.exit(1)
