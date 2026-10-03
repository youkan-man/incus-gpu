#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TOOL="$ROOT_DIR/incus-gpu"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SYS="$TMP/sys"
PROC="$TMP/proc"
ETC="$TMP/etc"
DEV="$TMP/dev"
BIN="$TMP/bin"
STATE="$TMP/state.json"
LOG="$TMP/incus.log"
mkdir -p "$SYS/bus/pci/devices" "$SYS/bus/pci/drivers/nvidia" "$SYS/bus/pci/drivers/snd_hda_intel" \
         "$SYS/bus/pci/drivers/amdgpu" "$SYS/kernel/iommu_groups/7/devices" \
         "$SYS/kernel/iommu_groups/8/devices" "$SYS/kernel/iommu_groups/9/devices" \
         "$SYS/kernel/iommu_groups/10/devices" "$SYS/module/vfio_pci" \
         "$SYS/bus/pci/drivers/vfio-pci" "$DEV/vfio" \
         "$PROC" "$ETC" "$DEV" "$BIN"
touch "$DEV/kvm"

make_pci() {
    local bdf=$1 class=$2 vendor=$3 device=$4 driver=$5 group=$6 boot=${7:-0}
    local path="$SYS/bus/pci/devices/$bdf"
    mkdir -p "$path"
    printf '%s\n' "$class" > "$path/class"
    printf '0x%s\n' "$vendor" > "$path/vendor"
    printf '0x%s\n' "$device" > "$path/device"
    printf '%s\n' "$boot" > "$path/boot_vga"
    printf '(null)\n' > "$path/driver_override"
    if [[ "$driver" != "-" ]]; then
        mkdir -p "$SYS/bus/pci/drivers/$driver"
        ln -s "$SYS/bus/pci/drivers/$driver" "$path/driver"
    fi
    ln -s "$SYS/kernel/iommu_groups/$group" "$path/iommu_group"
    ln -s "$path" "$SYS/kernel/iommu_groups/$group/devices/$bdf"
}

make_pci 0000:01:00.0 0x030000 10de 2684 nvidia 7 0
make_pci 0000:01:00.1 0x040300 10de 22ba snd_hda_intel 7 0
make_pci 0000:02:00.0 0x030200 1002 73bf amdgpu 8 1
# AMD-style topology: an unbound host bridge shares the GPU IOMMU group.
make_pci 0000:00:03.0 0x060000 1022 14da - 9 0
make_pci 0000:03:00.0 0x030000 1002 7590 amdgpu 9 0
make_pci 0000:03:00.1 0x040300 1002 1640 snd_hda_intel 9 0
# A true non-GPU endpoint in the group must still be rejected.
make_pci 0000:04:00.0 0x030000 1002 744c amdgpu 10 0
make_pci 0000:05:00.0 0x020000 8086 15f3 - 10 0

cat > "$PROC/cpuinfo" <<'EOF_CPU'
processor : 0
vendor_id : GenuineIntel
EOF_CPU
cat > "$PROC/cmdline" <<'EOF_CMDLINE'
BOOT_IMAGE=/vmlinuz root=/dev/test ro quiet
EOF_CMDLINE
cat > "$PROC/modules" <<'EOF_MODULES'
vfio_pci 65536 0 - Live 0x0
vfio_iommu_type1 45056 0 - Live 0x0
vfio 65536 2 vfio_pci,vfio_iommu_type1, Live 0x0
EOF_MODULES

cat > "$STATE" <<'EOF_STATE'
[
  {
    "name": "vm-stopped",
    "type": "virtual-machine",
    "status": "Stopped",
    "location": "node1",
    "devices": {},
    "expanded_devices": {}
  },
  {
    "name": "vm-running",
    "type": "virtual-machine",
    "status": "Running",
    "location": "node1",
    "devices": {},
    "expanded_devices": {}
  },
  {
    "name": "vm-fail",
    "type": "virtual-machine",
    "status": "Running",
    "location": "node1",
    "devices": {},
    "expanded_devices": {}
  },
  {
    "name": "vm-host-bridge",
    "type": "virtual-machine",
    "status": "Stopped",
    "location": "node1",
    "devices": {},
    "expanded_devices": {}
  },
  {
    "name": "vm-foreign-endpoint",
    "type": "virtual-machine",
    "status": "Stopped",
    "location": "node1",
    "devices": {},
    "expanded_devices": {}
  },
  {
    "name": "container-one",
    "type": "container",
    "status": "Running",
    "location": "node1",
    "devices": {},
    "expanded_devices": {}
  }
]
EOF_STATE

cat > "$BIN/lspci" <<'EOF_LSPCI'
#!/usr/bin/env bash
set -euo pipefail
bdf=""
while (($#)); do
    case "$1" in
        -s) bdf=${2#0000:}; shift 2 ;;
        *) shift ;;
    esac
done
case "$bdf" in
    01:00.0) echo '01:00.0 VGA compatible controller: NVIDIA Corporation Test GPU [10de:2684]' ;;
    01:00.1) echo '01:00.1 Audio device: NVIDIA Corporation Test Audio [10de:22ba]' ;;
    02:00.0) echo '02:00.0 3D controller: Advanced Micro Devices, Inc. Test Radeon [1002:73bf]' ;;
    00:03.0) echo '00:03.0 Host bridge: Advanced Micro Devices, Inc. Test Host Bridge [1022:14da]' ;;
    03:00.0) echo '03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. Test GPU [1002:7590]' ;;
    03:00.1) echo '03:00.1 Audio device: Advanced Micro Devices, Inc. Test Audio [1002:1640]' ;;
    04:00.0) echo '04:00.0 VGA compatible controller: Advanced Micro Devices, Inc. Foreign Group GPU [1002:744c]' ;;
    05:00.0) echo '05:00.0 Ethernet controller: Intel Corporation Test Ethernet [8086:15f3]' ;;
    *) exit 1 ;;
esac
EOF_LSPCI
chmod +x "$BIN/lspci"

cat > "$BIN/incus" <<'EOF_INCUS'
#!/usr/bin/env bash
set -Eeuo pipefail
STATE=${MOCK_INCUS_STATE:?}
LOG=${MOCK_INCUS_LOG:?}
printf '%q ' "$@" >> "$LOG"
printf '\n' >> "$LOG"

args=()
while (($#)); do
    case "$1" in
        --force-local) shift ;;
        --project) shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
set -- "${args[@]}"

update_status() {
    python3 - "$STATE" "$1" "$2" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
name, status = sys.argv[2], sys.argv[3]
data = json.loads(path.read_text())
for inst in data:
    if inst["name"] == name:
        inst["status"] = status
        break
else:
    raise SystemExit(2)
path.write_text(json.dumps(data, indent=2))
PY
}

case "${1:-}" in
    version)
        echo 'Client version: 7.0.1'
        echo 'Server version: 7.0.1'
        ;;
    query)
        python3 - <<'PY_QUERY'
import json, os
print(json.dumps({"environment": {
    "server_clustered": os.environ.get("MOCK_CLUSTERED", "false").lower() == "true",
    "server_name": os.environ.get("MOCK_SERVER_NAME", "node1"),
}}))
PY_QUERY
        ;;
    list)
        cat "$STATE"
        ;;
    stop)
        update_status "$2" Stopped
        ;;
    start)
        if [[ "${MOCK_FAIL_START_VM:-}" == "$2" && ! -e "${MOCK_FAIL_MARKER:-/nonexistent}" ]]; then
            : > "${MOCK_FAIL_MARKER:?}"
            exit 42
        fi
        update_status "$2" Running
        ;;
    config)
        [[ "$2" == device ]] || exit 64
        action=$3
        vm=$4
        dev=$5
        case "$action" in
            add)
                type=$6
                shift 6
                python3 - "$STATE" "$vm" "$dev" "$type" "$@" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
vm, name, typ = sys.argv[2:5]
props = {"type": typ}
for item in sys.argv[5:]:
    if "=" in item:
        k, v = item.split("=", 1)
        props[k] = v
data = json.loads(path.read_text())
for inst in data:
    if inst["name"] == vm:
        if name in inst.setdefault("expanded_devices", {}):
            raise SystemExit(3)
        inst.setdefault("devices", {})[name] = props
        inst.setdefault("expanded_devices", {})[name] = props.copy()
        break
else:
    raise SystemExit(2)
path.write_text(json.dumps(data, indent=2))
PY
                ;;
            remove)
                python3 - "$STATE" "$vm" "$dev" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
vm, name = sys.argv[2:4]
data = json.loads(path.read_text())
for inst in data:
    if inst["name"] == vm:
        inst.setdefault("devices", {}).pop(name, None)
        inst.setdefault("expanded_devices", {}).pop(name, None)
        break
else:
    raise SystemExit(2)
path.write_text(json.dumps(data, indent=2))
PY
                ;;
            *) exit 64 ;;
        esac
        ;;
    *)
        echo "mock incus: unsupported command: $*" >&2
        exit 64
        ;;
esac
EOF_INCUS
chmod +x "$BIN/incus"

export INCUS_GPU_SYSFS_ROOT="$SYS"
export INCUS_GPU_PROC_ROOT="$PROC"
export INCUS_GPU_ETC_ROOT="$ETC"
export INCUS_GPU_DEV_ROOT="$DEV"
export INCUS_GPU_INCUS_BIN="$BIN/incus"
export INCUS_GPU_LSPCI_BIN="$BIN/lspci"
export MOCK_INCUS_STATE="$STATE"
export MOCK_INCUS_LOG="$LOG"
export NO_COLOR=1

pass=0
fail=0

run_test() {
    local name=$1
    shift
    if "$@"; then
        printf 'ok - %s\n' "$name"
        ((pass += 1))
    else
        printf 'not ok - %s\n' "$name" >&2
        ((fail += 1))
    fi
}

assert_contains() {
    local haystack=$1 needle=$2
    [[ "$haystack" == *"$needle"* ]]
}

state_has_device() {
    local vm=$1 dev=$2 expected_pci=${3:-}
    python3 - "$STATE" "$vm" "$dev" "$expected_pci" <<'PY'
import json, sys
state, vm, dev, expected = sys.argv[1:5]
data = json.load(open(state))
for inst in data:
    if inst["name"] == vm:
        item = inst.get("devices", {}).get(dev)
        if not item:
            raise SystemExit(1)
        if expected and item.get("pci") != expected:
            raise SystemExit(1)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

state_lacks_device() {
    ! state_has_device "$@"
}

state_is() {
    local vm=$1 expected=$2
    python3 - "$STATE" "$vm" "$expected" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for inst in data:
    if inst["name"] == sys.argv[2]:
        raise SystemExit(0 if inst["status"] == sys.argv[3] else 1)
raise SystemExit(1)
PY
}

test_list() {
    local out
    out=$($TOOL list)
    assert_contains "$out" '01:00.0' && assert_contains "$out" 'Test GPU' && assert_contains "$out" '02:00.0'
}

test_attach_stopped() {
    $TOOL attach vm-stopped 1 --device test-gpu >/dev/null
    state_has_device vm-stopped test-gpu 0000:01:00.0
}

test_attach_idempotent() {
    local before after
    before=$(python3 -c 'import json,sys; print(len(next(x for x in json.load(open(sys.argv[1])) if x["name"]=="vm-stopped")["devices"]))' "$STATE")
    $TOOL attach vm-stopped 01:00.0 >/dev/null
    after=$(python3 -c 'import json,sys; print(len(next(x for x in json.load(open(sys.argv[1])) if x["name"]=="vm-stopped")["devices"]))' "$STATE")
    [[ "$before" == "$after" ]]
}

test_status() {
    local out
    out=$($TOOL status vm-stopped)
    assert_contains "$out" 'test-gpu' && assert_contains "$out" 'pci=0000:01:00.0'
}

test_detach() {
    $TOOL detach vm-stopped --device test-gpu >/dev/null
    state_lacks_device vm-stopped test-gpu
}

test_running_requires_mode() {
    if $TOOL attach vm-running 1 --device should-not-exist >/dev/null 2>&1; then
        return 1
    fi
    state_lacks_device vm-running should-not-exist
}

test_running_restart() {
    $TOOL attach vm-running 1 --device running-gpu --restart >/dev/null
    state_has_device vm-running running-gpu 0000:01:00.0 && state_is vm-running Running
}

test_duplicate_blocked() {
    if $TOOL attach vm-stopped 1 --device duplicate >/dev/null 2>&1; then
        return 1
    fi
    state_lacks_device vm-stopped duplicate
}

test_boot_vga_blocked() {
    if $TOOL attach vm-stopped 2 --device boot-gpu >/dev/null 2>&1; then
        return 1
    fi
    state_lacks_device vm-stopped boot-gpu
}

test_unbound_host_bridge_is_ignored() {
    local out
    out=$($TOOL attach vm-host-bridge 0000:03:00.0 --device host-bridge-gpu 2>&1)
    state_has_device vm-host-bridge host-bridge-gpu 0000:03:00.0 &&         [[ "$out" != *"GPU基板外と思われるPCIエンドポイント"* ]]
}

test_foreign_endpoint_is_blocked() {
    local out_file="$TMP/foreign-endpoint.out" out
    if $TOOL attach vm-foreign-endpoint 0000:04:00.0 --device foreign-group-gpu >"$out_file" 2>&1; then
        return 1
    fi
    out=$(cat "$out_file")
    state_lacks_device vm-foreign-endpoint foreign-group-gpu && \
        assert_contains "$out" '0000:05:00.0(class=0x020000,driver=-)'
}

test_bind_vfio_dry_run() {
    local out
    out=$($TOOL bind-vfio 0000:03:00.0 --force --dry-run 2>&1)
    assert_contains "$out" 'vfio-pci' && \
        assert_contains "$out" 'drivers/vfio-pci/bind' && \
        assert_contains "$out" '0000:03:00.0/driver_override' && \
        assert_contains "$out" '0000:03:00.1/driver_override' && \
        [[ "$out" != *"0000:00:03.0/driver_override"* ]]
}

test_attach_prebind_dry_run() {
    local out
    out=$($TOOL attach vm-host-bridge 0000:03:00.0 \
        --device dry-prebind --prebind-vfio --vfio-timeout 20 --dry-run 2>&1)
    state_lacks_device vm-host-bridge dry-prebind && \
        assert_contains "$out" '0000:03:00.0/driver_override' && \
        assert_contains "$out" 'drivers/vfio-pci/bind' && \
        assert_contains "$out" 'config device add'
}

test_prepare_dry_run() {
    local out
    out=$($TOOL prepare-host --dry-run --yes 2>&1)
    assert_contains "$out" 'intel_iommu=on' && assert_contains "$out" 'update-initramfs' && [[ ! -e "$ETC/default/grub.d/99-incus-gpu-passthrough.cfg" ]]
}

test_doctor() {
    local out
    out=$($TOOL doctor --vm vm-running --gpu 1 2>&1)
    assert_contains "$out" '[OK] Host IOMMU groups are present' && \
        assert_contains "$out" 'vfio-pci driver: present' && \
        assert_contains "$out" 'VM: vm-running'
}

test_start_failure_rolls_back() {
    local out_file="$TMP/start-failure.out" out
    export MOCK_FAIL_START_VM=vm-fail
    export MOCK_FAIL_MARKER="$TMP/start-failed-once"
    rm -f "$MOCK_FAIL_MARKER"
    if $TOOL attach vm-fail 2 --device rollback-gpu --restart --force \
        --project app-deploy >"$out_file" 2>&1; then
        unset MOCK_FAIL_START_VM MOCK_FAIL_MARKER
        return 1
    fi
    unset MOCK_FAIL_START_VM MOCK_FAIL_MARKER
    out=$(cat "$out_file")
    state_lacks_device vm-fail rollback-gpu && state_is vm-fail Running && \
        assert_contains "$out" '--- VFIO host state ---' && \
        assert_contains "$out" 'incus info --show-log vm-fail --project app-deploy'
}

test_cluster_member_mismatch() {
    export MOCK_CLUSTERED=true
    export MOCK_SERVER_NAME=node2
    if $TOOL status vm-stopped >/dev/null 2>&1; then
        unset MOCK_CLUSTERED MOCK_SERVER_NAME
        return 1
    fi
    unset MOCK_CLUSTERED MOCK_SERVER_NAME
}

run_test 'list GPUs' test_list
run_test 'attach to stopped VM' test_attach_stopped
run_test 'attach is idempotent for same VM/GPU' test_attach_idempotent
run_test 'status shows attached GPU' test_status
run_test 'detach GPU' test_detach
run_test 'running VM requires --stop/--restart' test_running_requires_mode
run_test 'running VM stops and restarts' test_running_restart
run_test 'duplicate assignment is blocked' test_duplicate_blocked
run_test 'boot VGA is blocked without --force' test_boot_vga_blocked
run_test 'bind-vfio dry-run targets GPU functions only' test_bind_vfio_dry_run
run_test 'attach supports VFIO prebind dry-run' test_attach_prebind_dry_run
run_test 'unbound host bridge in GPU group is ignored' test_unbound_host_bridge_is_ignored
run_test 'foreign endpoint in GPU group is still blocked' test_foreign_endpoint_is_blocked
run_test 'prepare-host dry-run changes nothing' test_prepare_dry_run
run_test 'doctor validates fixture' test_doctor
run_test 'start failure rolls back GPU and restores VM' test_start_failure_rolls_back
run_test 'cluster member mismatch is blocked' test_cluster_member_mismatch

printf '\n%d passed, %d failed\n' "$pass" "$fail"
((fail == 0))
