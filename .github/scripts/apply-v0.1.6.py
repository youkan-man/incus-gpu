from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def replace_between(text: str, start: str, end: str, replacement: str, label: str) -> str:
    start_pos = text.find(start)
    if start_pos < 0:
        raise SystemExit(f"{label}: start marker not found")
    end_pos = text.find(end, start_pos + len(start))
    if end_pos < 0:
        raise SystemExit(f"{label}: end marker not found")
    return text[:start_pos] + replacement.rstrip() + "\n\n" + text[end_pos + 1:]


tool_path = Path("incus-gpu")
tool = tool_path.read_text()

tool = replace_once(tool, 'VERSION="0.1.5"', 'VERSION="0.1.6"', "version")
tool = replace_once(
    tool,
    "  ${PROG} doctor [--vm VM] [--gpu GPU] [--kernel-log]\n  ${PROG} attach VM GPU [オプション]",
    "  ${PROG} doctor [--vm VM] [--gpu GPU] [--kernel-log]\n"
    "  sudo ${PROG} inspect VM GPU [--project NAME] [--timeout SEC]\n"
    "  ${PROG} attach VM GPU [オプション]",
    "usage inspect",
)
tool = replace_once(
    tool,
    "doctorオプション:\n  --vm VM               対象VMを確認\n  --gpu GPU             対象GPUとIOMMU/VFIO状態を確認\n  --project NAME        Incusプロジェクト\n  --kernel-log          対象GPU、VFIO、IOMMUに関係するカーネルログも表示\n\n例:",
    "doctorオプション:\n  --vm VM               対象VMを確認\n  --gpu GPU             対象GPUとIOMMU/VFIO状態を確認\n  --project NAME        Incusプロジェクト\n  --kernel-log          対象GPU、VFIO、IOMMUに関係するカーネルログも表示\n\n"
    "inspectオプション:\n  --project NAME        Incusプロジェクト\n  --timeout SEC         各PCI sysfs操作の監視秒数（既定: 15）\n  --force-stop          VMの通常停止失敗時に強制停止\n  --force               保護判定を解除して精査\n  --dry-run             停止・PCI操作・復旧コマンドだけを表示\n\n"
    "例:",
    "usage inspect options",
)
tool = replace_once(
    tool,
    "  ${PROG} doctor --vm ai-vm --gpu 1 --kernel-log\n  ${PROG} attach ai-vm 0000:41:00.0 --restart",
    "  ${PROG} doctor --vm ai-vm --gpu 1 --kernel-log\n"
    "  sudo ${PROG} inspect ai-vm 0000:41:00.0 --project default\n"
    "  ${PROG} attach ai-vm 0000:41:00.0 --restart",
    "usage inspect example",
)

new_unbind = r'''unbind_pci_function() {
    local bdf=$1 timeout=${2:-$VFIO_BIND_TIMEOUT} driver unbind_path rc
    driver=$(pci_driver "$bdf")
    [[ "$driver" == "-" ]] && return 0
    unbind_path="$SYSFS_ROOT/bus/pci/drivers/$driver/unbind"

    if sysfs_write_watchdog "$bdf" "$unbind_path" \
        "PCI driver unbind ($bdf from $driver)" "$timeout"; then
        return 0
    else
        rc=$?
        return "$rc"
    fi
}'''
tool = replace_between(
    tool,
    "unbind_pci_function() {\n",
    "\nbind_pci_function_to_vfio() {\n",
    new_unbind,
    "unbind_pci_function",
)

new_bind = r'''bind_pci_function_to_vfio() {
    local bdf=$1 timeout=$2 current override_path bind_path probe_path bind_epoch rc
    current=$(pci_driver "$bdf")
    if [[ "$current" == "vfio-pci" ]]; then
        info "$bdf は既にvfio-pciへバインドされています。"
        return 0
    fi

    info "$bdf を${current}からvfio-pciへ切り替えます。"
    if unbind_pci_function "$bdf" "$timeout"; then
        :
    else
        rc=$?
        [[ -n "$SYSFS_WRITE_STUCK_PID" ]] && return 124
        warn "$bdf のホストドライバー解除に失敗しました。"
        print_pci_diagnostics "$bdf"
        return "$rc"
    fi

    override_path="$(pci_path "$bdf")/driver_override"
    if sysfs_write_watchdog 'vfio-pci' "$override_path" \
        "driver_override設定 ($bdf)" "$timeout"; then
        :
    else
        rc=$?
        [[ -n "$SYSFS_WRITE_STUCK_PID" ]] && return 124
        warn "$bdf のdriver_override設定が完了しませんでした。"
        print_pci_diagnostics "$bdf"
        return "$rc"
    fi

    bind_path="$SYSFS_ROOT/bus/pci/drivers/vfio-pci/bind"
    probe_path="$SYSFS_ROOT/bus/pci/drivers_probe"
    bind_epoch=$(date +%s)

    # PCI driver probeはカーネル内で長時間停止することがあるため、
    # メインシェルでは実行せず監視付きワーカーへ分離する。
    if [[ -e "$bind_path" ]] || (( DRY_RUN )); then
        if sysfs_write_watchdog "$bdf" "$bind_path" \
            "vfio-pci direct bind ($bdf)" "$timeout"; then
            :
        else
            rc=$?
            [[ -n "$SYSFS_WRITE_STUCK_PID" ]] && return 124
            warn "$bdf のvfio-pci probeが完了しませんでした。"
            print_pci_diagnostics "$bdf"
            print_kernel_vfio_log "$bdf" "$bind_epoch"
            return "$rc"
        fi
    else
        warn "vfio-pci/bindが無いためdrivers_probeへフォールバックします。"
        if sysfs_write_watchdog "$bdf" "$probe_path" \
            "PCI drivers_probe ($bdf)" "$timeout"; then
            :
        else
            rc=$?
            [[ -n "$SYSFS_WRITE_STUCK_PID" ]] && return 124
            print_pci_diagnostics "$bdf"
            print_kernel_vfio_log "$bdf" "$bind_epoch"
            return "$rc"
        fi
    fi

    if ! wait_for_pci_driver "$bdf" vfio-pci "$timeout"; then
        warn "$bdf が${timeout}秒以内にvfio-pciへバインドされませんでした。"
        print_pci_diagnostics "$bdf"
        print_kernel_vfio_log "$bdf" "$bind_epoch"
        return 1
    fi
    ok "$bdf -> vfio-pci"
}'''
tool = replace_between(
    tool,
    "bind_pci_function_to_vfio() {\n",
    "\nrestore_pci_function() {\n",
    new_bind,
    "bind_pci_function_to_vfio",
)

new_restore = r'''restore_pci_function() {
    local bdf=$1 original_driver=$2 original_override=$3 timeout=${4:-$VFIO_BIND_TIMEOUT}
    local current override_path restore_override rc
    current=$(pci_driver "$bdf")
    if [[ "$current" != "-" ]]; then
        if unbind_pci_function "$bdf" "$timeout"; then
            :
        else
            rc=$?
            return "$rc"
        fi
    fi

    override_path="$(pci_path "$bdf")/driver_override"
    if [[ "$original_override" == "-" ]]; then
        restore_override=''
    else
        restore_override=$original_override
    fi

    if sysfs_write_watchdog "$restore_override" "$override_path" \
        "driver_override復元 ($bdf)" "$timeout"; then
        :
    else
        rc=$?
        return "$rc"
    fi

    if [[ "$original_driver" != "-" ]]; then
        if command -v modprobe >/dev/null 2>&1; then
            modprobe "$original_driver" >/dev/null 2>&1 || true
        fi
        if sysfs_write_watchdog "$bdf" "$SYSFS_ROOT/bus/pci/drivers_probe" \
            "PCI driver復元probe ($bdf -> $original_driver)" "$timeout"; then
            :
        else
            rc=$?
            return "$rc"
        fi
        if ! wait_for_pci_driver "$bdf" "$original_driver" "$timeout"; then
            warn "$bdf を元のドライバー $original_driver へ戻せませんでした。"
            return 1
        fi
    fi
}'''
tool = replace_between(
    tool,
    "restore_pci_function() {\n",
    "\nrestore_prebound_vfio() {\n",
    new_restore,
    "restore_pci_function",
)

new_restore_all = r'''restore_prebound_vfio() {
    local index bdf failed=0 rc
    ((${#PREBOUND_FUNCTIONS[@]} > 0)) || return 0
    warn "事前バインドしたPCI機能を元の状態へ戻します。"
    for ((index=${#PREBOUND_FUNCTIONS[@]}-1; index>=0; index--)); do
        bdf=${PREBOUND_FUNCTIONS[index]}
        if restore_pci_function "$bdf" \
            "${PREBIND_ORIGINAL_DRIVER[$bdf]}" \
            "${PREBIND_ORIGINAL_OVERRIDE[$bdf]}" "$VFIO_BIND_TIMEOUT"; then
            :
        else
            rc=$?
            failed=1
            warn "$bdf のPCI状態を完全には復元できませんでした (exit=$rc)。"
            [[ -n "$SYSFS_WRITE_STUCK_PID" ]] && break
        fi
    done
    PREBOUND_FUNCTIONS=()
    PREBIND_ORIGINAL_DRIVER=()
    PREBIND_ORIGINAL_OVERRIDE=()
    return "$failed"
}'''
tool = replace_between(
    tool,
    "restore_prebound_vfio() {\n",
    "\nprebind_vfio_functions() {\n",
    new_restore_all,
    "restore_prebound_vfio",
)

new_release = r'''release_vfio_functions() {
    local bdf=$1 group function current changed=0 rc
    local -a functions=()
    require_sysfs_write_access || return 1
    group=$(pci_iommu_group "$bdf")

    if [[ "$group" != "-" && -e "$DEV_ROOT/vfio/$group" ]] && \
       command -v fuser >/dev/null 2>&1 && \
       fuser -s "$DEV_ROOT/vfio/$group" 2>/dev/null; then
        warn "VFIOグループ $group はプロセスが使用中です。"
        (( FORCE )) || die "VMを停止するか、内容を確認して --force を付けてください。"
    fi

    pci_slot_functions functions "$bdf"
    for function in "${functions[@]}"; do
        current=$(pci_driver "$function")
        [[ "$current" == "vfio-pci" ]] || continue
        changed=1
        info "$function をvfio-pciから解放してホストへ再probeします。"
        if unbind_pci_function "$function" "$VFIO_BIND_TIMEOUT"; then
            :
        else
            rc=$?
            return "$rc"
        fi
        if sysfs_write_watchdog '' "$(pci_path "$function")/driver_override" \
            "driver_override解除 ($function)" "$VFIO_BIND_TIMEOUT"; then
            :
        else
            rc=$?
            return "$rc"
        fi
        if sysfs_write_watchdog "$function" "$SYSFS_ROOT/bus/pci/drivers_probe" \
            "ホストドライバー再probe ($function)" "$VFIO_BIND_TIMEOUT"; then
            :
        else
            rc=$?
            return "$rc"
        fi
        if (( ! DRY_RUN )); then
            sleep 0.2
            info "$function の現在のドライバー: $(pci_driver "$function")"
        fi
    done

    (( changed )) || info "vfio-pciへバインドされた対象機能はありません。"
    print_vfio_state "$bdf"
}'''
tool = replace_between(
    tool,
    "release_vfio_functions() {\n",
    "\n\nprint_pci_diagnostics() {\n",
    new_release,
    "release_vfio_functions",
)

inspect_function = r'''cmd_inspect() {
    local vm="" selector="" bdf="" existing_line=""
    local timeout=$VFIO_BIND_TIMEOUT rc=0 bind_ok=0 restore_ok=1 vm_restore_ok=1
    local original_status="" final_status="unknown"
    local -a positional=()

    while (($#)); do
        case "$1" in
            --vm) [[ $# -ge 2 ]] || die "--vmに値が必要です"; vm=$2; shift 2 ;;
            --gpu) [[ $# -ge 2 ]] || die "--gpuに値が必要です"; selector=$2; shift 2 ;;
            --project) [[ $# -ge 2 ]] || die "--projectに値が必要です"; PROJECT=$2; shift 2 ;;
            --timeout|--vfio-timeout) [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "$1には秒数が必要です"; timeout=$2; shift 2 ;;
            --stop-timeout) [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--stop-timeoutには秒数が必要です"; STOP_TIMEOUT=$2; shift 2 ;;
            --force-stop) FORCE_STOP=1; shift ;;
            --force) FORCE=1; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            -h|--help) usage; return 0 ;;
            -*) die "inspectの不明なオプションです: $1" ;;
            *) positional+=("$1"); shift ;;
        esac
    done

    [[ -n "$vm" ]] || vm=${positional[0]:-}
    [[ -n "$selector" ]] || selector=${positional[1]:-}
    ((${#positional[@]} <= 2)) || die "inspectの位置引数が多すぎます。"
    [[ -n "$vm" && -n "$selector" ]] || die "inspectにはVM名とGPU指定が必要です。"

    bdf=$(resolve_gpu "$selector")
    load_instances
    load_vm "$vm"
    check_cluster_location "$vm"
    existing_line=$(check_other_assignments "$vm" "$bdf")
    original_status=$VM_STATUS

    printf '=== incus-gpu inspect %s ===\n' "$VERSION"
    printf 'VM=%s project=%s original_status=%s\n' "$vm" "${PROJECT:-default}" "$original_status"
    printf 'GPU=%s timeout=%ss\n' "$bdf" "$timeout"
    [[ -n "$existing_line" ]] && printf 'Existing assignment: %s\n' "$existing_line"

    preflight_gpu "$bdf"
    printf '\n--- baseline ---\n'
    print_vfio_state "$bdf"
    print_pci_diagnostics "$bdf"

    # inspectは一発で精査するため、稼働中VMは自動停止し、精査後に元へ戻す。
    STOP_MODE="restart"
    stop_vm_for_change "$vm"

    printf '\n--- guarded VFIO probe ---\n'
    if prebind_vfio_functions "$bdf" "$timeout"; then
        bind_ok=1
        ok "GPU関連機能のvfio-pciバインド試験に成功しました。"
        if restore_prebound_vfio; then
            ok "PCI状態を精査前へ復元しました。"
        else
            rc=$?
            restore_ok=0
        fi
    else
        rc=$?
    fi

    if [[ -n "$SYSFS_WRITE_STUCK_PID" ]]; then
        printf '\n=== inspection summary ===\n'
        printf 'result=KERNEL_WAIT\n'
        printf 'vm=%s original_status=%s final_status=Stopped\n' "$vm" "$original_status"
        printf 'gpu=%s\n' "$bdf"
        printf 'blocked_operation=%s\n' "$SYSFS_WRITE_STUCK_LABEL"
        printf 'worker_pid=%s state=%s wchan=%s\n' \
            "$SYSFS_WRITE_STUCK_PID" "$SYSFS_WRITE_STUCK_STATE" "$SYSFS_WRITE_STUCK_WCHAN"
        printf 'action=host-reboot-required-if-state-D\n'
        return 124
    fi

    if (( VM_ORIGINALLY_RUNNING )); then
        info "精査完了後、VM '$vm' を元の稼働状態へ戻します。"
        if incus_mutate start "$vm"; then
            :
        else
            vm_restore_ok=0
            (( rc == 0 )) && rc=1
            warn "VM '$vm' を再起動できませんでした。"
        fi
    fi

    if (( ! DRY_RUN )); then
        load_instances
        load_vm "$vm"
        final_status=$VM_STATUS
    else
        final_status="dry-run"
    fi

    printf '\n--- final host state ---\n'
    print_vfio_state "$bdf"
    printf '\n=== inspection summary ===\n'
    if (( bind_ok && restore_ok && vm_restore_ok && rc == 0 )); then
        printf 'result=PASS\n'
    else
        printf 'result=FAIL\n'
    fi
    printf 'vm=%s original_status=%s final_status=%s\n' "$vm" "$original_status" "$final_status"
    printf 'gpu=%s bind_ok=%s restore_ok=%s vm_restore_ok=%s exit=%s\n' \
        "$bdf" "$bind_ok" "$restore_ok" "$vm_restore_ok" "$rc"

    return "$rc"
}'''
tool = replace_once(
    tool,
    "cmd_release_vfio() {\n",
    inspect_function + "\n\ncmd_release_vfio() {\n",
    "insert cmd_inspect",
)
tool = replace_once(
    tool,
    "        doctor|check) cmd_doctor \"$@\" ;;\n        attach|add) cmd_attach \"$@\" ;;",
    "        doctor|check) cmd_doctor \"$@\" ;;\n"
    "        inspect|probe|diagnose) cmd_inspect \"$@\" ;;\n"
    "        attach|add) cmd_attach \"$@\" ;;",
    "dispatch inspect",
)
tool_path.write_text(tool)


test_path = Path("tests/test.sh")
tests = test_path.read_text()
new_tests = r'''test_driver_override_write_watchdog_returns() {
    local override="$SYS/bus/pci/devices/0000:03:00.0/driver_override"
    local out_file="$TMP/override-watchdog.out" out rc elapsed start helper

    rm -f "$SYS/bus/pci/devices/0000:03:00.0/driver"
    rm -f "$override"
    mkfifo "$override"
    (
        for _ in 1 2 3 4 5 6; do
            printf '(null)\n' > "$override" || exit 0
        done
    ) &
    helper=$!

    start=$SECONDS
    if timeout 10 "$TOOL" bind-vfio 0000:03:00.0 --force --timeout 1 \
        >"$out_file" 2>&1; then
        rc=0
    else
        rc=$?
    fi
    elapsed=$((SECONDS - start))

    kill "$helper" 2>/dev/null || true
    wait "$helper" 2>/dev/null || true
    rm -f "$override"
    printf '(null)\n' > "$override"
    out=$(cat "$out_file")

    (( rc != 0 && elapsed < 8 )) && \
        assert_contains "$out" 'driver_override設定 (0000:03:00.0)' && \
        assert_contains "$out" '監視ワーカーPID='
}

test_inspect_single_command_restores_vm() {
    local bind_path="$SYS/bus/pci/drivers/vfio-pci/bind"
    local out_file="$TMP/inspect-watchdog.out" out rc elapsed start

    rm -f "$SYS/bus/pci/devices/0000:03:00.0/driver"
    printf '(null)\n' > "$SYS/bus/pci/devices/0000:03:00.0/driver_override"
    rm -f "$bind_path"
    mkfifo "$bind_path"

    start=$SECONDS
    if timeout 10 "$TOOL" inspect vm-running 0000:03:00.0 \
        --project app-deploy --timeout 1 --force >"$out_file" 2>&1; then
        rc=0
    else
        rc=$?
    fi
    elapsed=$((SECONDS - start))
    rm -f "$bind_path"
    : > "$bind_path"
    out=$(cat "$out_file")

    (( rc != 0 && elapsed < 8 )) && \
        state_is vm-running Running && \
        assert_contains "$out" '=== inspection summary ===' && \
        assert_contains "$out" 'result=FAIL' && \
        assert_contains "$out" 'final_status=Running'
}

'''
tests = replace_once(
    tests,
    "test_prepare_dry_run() {\n",
    new_tests + "test_prepare_dry_run() {\n",
    "insert watchdog/inspect tests",
)
tests = replace_once(
    tests,
    "run_test 'blocking sysfs bind returns via watchdog' test_bind_write_watchdog_returns\n\nprintf",
    "run_test 'blocking driver_override returns via watchdog' test_driver_override_write_watchdog_returns\n"
    "run_test 'blocking sysfs bind returns via watchdog' test_bind_write_watchdog_returns\n"
    "run_test 'inspect is one command and restores VM' test_inspect_single_command_restores_vm\n\nprintf",
    "register tests",
)
test_path.write_text(tests)

readme_path = Path("README.md")
readme = readme_path.read_text()
if "## 単独コマンドでVFIO精査" not in readme:
    readme += r'''

## 単独コマンドでVFIO精査

VM停止、GPU関連機能のVFIOバインド試験、PCI/IOMMU/カーネル情報の採取、PCI状態の復元、元々稼働していたVMの再起動までを1回で実行します。

```bash
sudo incus-gpu inspect <VM> <GPU> --project <PROJECT>
```

例:

```bash
sudo incus-gpu inspect sb-436b44fcd4fe 1 --project app-deploy
```

既定では、unbind、`driver_override`、`vfio-pci/bind`、`drivers_probe`、復元処理の各sysfs書き込みを15秒監視します。カーネル内で書き込みが停止してもメインCLIは監視時間後に制御を戻します。

通常の失敗ではPCI状態を復元し、元々稼働していたVMを再起動します。ワーカーが割り込み不能な`D`状態で残った場合だけ、安全のためPCI復元とVM再起動を行わず、`result=KERNEL_WAIT`、PID、`wchan`を出力します。
'''
readme_path.write_text(readme)

print("v0.1.6 transformation prepared")
