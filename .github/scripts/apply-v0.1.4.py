#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one literal match, got {count}: {old[:100]!r}")
    write(path, text.replace(old, new, 1))


def regex_once(path: str, pattern: str, replacement: str) -> None:
    text = read(path)
    new_text, count = re.subn(pattern, lambda _match: replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"{path}: expected one regex match, got {count}: {pattern!r}")
    write(path, new_text)


replace_once("incus-gpu", 'VERSION="0.1.3"', 'VERSION="0.1.4"')

replace_once(
    "incus-gpu",
    r'''sysfs_write() {
    local value=$1 path=$2
    if (( DRY_RUN )); then
        printf '+ write %q > %q\n' "$value" "$path" >&2
        return 0
    fi
    [[ -e "$path" ]] || {
        warn "sysfsパスがありません: $path"
        return 1
    }
    printf '%s\n' "$value" > "$path"
}

load_vfio_pci_driver() {''',
    r'''sysfs_write() {
    local value=$1 path=$2
    if (( DRY_RUN )); then
        printf '+ write %q > %q\n' "$value" "$path" >&2
        return 0
    fi
    [[ -e "$path" ]] || {
        warn "sysfsパスがありません: $path"
        return 1
    }
    printf '%s\n' "$value" > "$path"
}

sysfs_write_checked() {
    local value=$1 path=$2 label=${3:-sysfs-write}
    local err_file rc err
    if (( DRY_RUN )); then
        printf '+ write %q > %q  # %s\n' "$value" "$path" "$label" >&2
        return 0
    fi
    [[ -e "$path" ]] || {
        warn "$label: sysfsパスがありません: $path"
        return 1
    }

    err_file=$(mktemp)
    if printf '%s\n' "$value" >"$path" 2>"$err_file"; then
        rm -f "$err_file"
        return 0
    else
        rc=$?
        err=$(tr '\n' ' ' <"$err_file" 2>/dev/null || true)
        rm -f "$err_file"
        warn "$label に失敗しました (exit=$rc, path=$path): ${err:-kernel rejected the write}"
        return "$rc"
    fi
}

load_vfio_pci_driver() {''',
)

regex_once(
    "incus-gpu",
    r'''require_sysfs_write_access\(\) \{\n.*?\n\}\n\nsysfs_write\(\) \{''',
    r'''require_sysfs_write_access() {
    (( DRY_RUN )) && return 0
    if [[ "$SYSFS_ROOT" == "/sys" ]] && (( EUID != 0 )); then
        warn "VFIOの事前バインドはroot権限が必要です。sudoで実行してください。"
        return 1
    fi
    if [[ ! -w "$SYSFS_ROOT/bus/pci/drivers_probe" ]]; then
        warn "PCI drivers_probeへ書き込めません: $SYSFS_ROOT/bus/pci/drivers_probe"
        return 1
    fi
}

sysfs_write() {''',
)

new_bind_function = r'''bind_pci_function_to_vfio() {
    local bdf=$1 timeout=$2 current override_path bind_path probe_path bind_epoch
    current=$(pci_driver "$bdf")
    if [[ "$current" == "vfio-pci" ]]; then
        info "$bdf は既にvfio-pciへバインドされています。"
        return 0
    fi

    info "$bdf を${current}からvfio-pciへ切り替えます。"
    unbind_pci_function "$bdf" || return 1

    override_path="$(pci_path "$bdf")/driver_override"
    sysfs_write_checked 'vfio-pci' "$override_path" "driver_override設定 ($bdf)" || return 1

    bind_path="$SYSFS_ROOT/bus/pci/drivers/vfio-pci/bind"
    probe_path="$SYSFS_ROOT/bus/pci/drivers_probe"
    bind_epoch=$(date +%s)

    # Writing directly to vfio-pci/bind returns the kernel errno to the caller.
    # drivers_probe often hides that detail and only leaves the device unbound.
    if [[ -e "$bind_path" ]] || (( DRY_RUN )); then
        if ! sysfs_write_checked "$bdf" "$bind_path" "vfio-pci direct bind ($bdf)"; then
            warn "$bdf のvfio-pci probeがカーネルに拒否されました。"
            print_pci_diagnostics "$bdf"
            print_kernel_vfio_log "$bdf" "$bind_epoch"
            return 1
        fi
    else
        warn "vfio-pci/bindが無いためdrivers_probeへフォールバックします。"
        if ! sysfs_write_checked "$bdf" "$probe_path" "PCI drivers_probe ($bdf)"; then
            print_pci_diagnostics "$bdf"
            print_kernel_vfio_log "$bdf" "$bind_epoch"
            return 1
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

regex_once(
    "incus-gpu",
    r'''bind_pci_function_to_vfio\(\) \{\n.*?\n\}\n\nrestore_pci_function\(\) \{''',
    new_bind_function + "\n\nrestore_pci_function() {",
)

new_prebind_function = r'''prebind_vfio_functions() {
    local bdf=$1 timeout=$2 function original_driver original_override group current
    local vfio_count=0 host_count=0
    local -a functions=()
    PREBOUND_FUNCTIONS=()
    PREBIND_ORIGINAL_DRIVER=()
    PREBIND_ORIGINAL_OVERRIDE=()

    require_sysfs_write_access || return 1
    load_vfio_pci_driver || return 1
    pci_slot_functions functions "$bdf"

    for function in "${functions[@]}"; do
        current=$(pci_driver "$function")
        if [[ "$current" == "vfio-pci" ]]; then
            ((vfio_count += 1))
        else
            ((host_count += 1))
        fi
    done

    if (( vfio_count > 0 && host_count > 0 )); then
        warn "GPU機能が部分的にvfio-pciへバインドされています。前回失敗の残留状態の可能性があります。"
        print_vfio_state "$bdf"
        warn "完全に戻してやり直す場合: sudo ${PROG} release-vfio $bdf --force"
    fi

    group=$(pci_iommu_group "$bdf")
    if [[ "$group" != "-" && -e "$DEV_ROOT/vfio/$group" ]] && \
       command -v fuser >/dev/null 2>&1 && \
       fuser -s "$DEV_ROOT/vfio/$group" 2>/dev/null; then
        warn "VFIOグループ $group はプロセスが使用中です: $DEV_ROOT/vfio/$group"
        fuser -v "$DEV_ROOT/vfio/$group" 2>&1 | sed 's/^/  /' >&2 || true
        (( FORCE )) || return 1
    fi

    for function in "${functions[@]}"; do
        original_driver=$(pci_driver "$function")
        original_override=$(pci_driver_override "$function")
        if [[ "$original_driver" == "vfio-pci" ]]; then
            info "$function は既にvfio-pciへバインドされています。"
            continue
        fi

        PREBOUND_FUNCTIONS+=("$function")
        PREBIND_ORIGINAL_DRIVER["$function"]=$original_driver
        PREBIND_ORIGINAL_OVERRIDE["$function"]=$original_override

        if ! bind_pci_function_to_vfio "$function" "$timeout"; then
            warn "VFIO事前バインドに失敗しました: $function"
            print_vfio_state "$bdf"
            restore_prebound_vfio || true
            return 1
        fi
    done

    print_vfio_state "$bdf"
}'''

regex_once(
    "incus-gpu",
    r'''prebind_vfio_functions\(\) \{\n.*?\n\}\n\nrelease_vfio_functions\(\) \{''',
    new_prebind_function + "\n\nrelease_vfio_functions() {",
)

replace_once(
    "incus-gpu",
    '''    require_sysfs_write_access
    group=$(pci_iommu_group "$bdf")
''',
    '''    require_sysfs_write_access || return 1
    group=$(pci_iommu_group "$bdf")
''',
)

diagnostic_helpers = r'''
print_pci_diagnostics() {
    local bdf=$1 group function field value group_type
    local -a functions=()
    local -a fields=(
        vendor device subsystem_vendor subsystem_device class revision
        enable numa_node boot_vga current_link_speed current_link_width
        max_link_speed max_link_width power_state d3cold_allowed reset_method
        modalias
    )

    group=$(pci_iommu_group "$bdf")
    pci_slot_functions functions "$bdf"

    printf '%s\n' '--- PCI/VFIO diagnostic detail ---' >&2
    printf 'Kernel: %s\n' "$(uname -a 2>/dev/null || printf unknown)" >&2
    if [[ -r "$PROC_ROOT/cmdline" ]]; then
        printf 'Kernel cmdline: %s\n' "$(cat "$PROC_ROOT/cmdline")" >&2
    fi
    if [[ "$group" != "-" && -r "$SYSFS_ROOT/kernel/iommu_groups/$group/type" ]]; then
        group_type=$(cat "$SYSFS_ROOT/kernel/iommu_groups/$group/type" 2>/dev/null || true)
        printf 'IOMMU group %s type: %s\n' "$group" "${group_type:-unknown}" >&2
    fi

    if [[ "$group" != "-" && -e "$DEV_ROOT/vfio/$group" ]] && command -v fuser >/dev/null 2>&1; then
        if fuser -s "$DEV_ROOT/vfio/$group" 2>/dev/null; then
            printf 'VFIO group users (%s):\n' "$DEV_ROOT/vfio/$group" >&2
            fuser -v "$DEV_ROOT/vfio/$group" 2>&1 | sed 's/^/  /' >&2 || true
        else
            printf 'VFIO group users: none detected\n' >&2
        fi
    fi

    for function in "${functions[@]}"; do
        printf '\n[%s] driver=%s override=%s iommu=%s\n' \
            "$function" "$(pci_driver "$function")" \
            "$(pci_driver_override "$function")" "$(pci_iommu_group "$function")" >&2

        for field in "${fields[@]}"; do
            if [[ -r "$(pci_path "$function")/$field" ]]; then
                value=$(tr '\000\r\n' ' ' <"$(pci_path "$function")/$field" 2>/dev/null || true)
                printf '  %-20s %s\n' "$field:" "${value:-<empty>}" >&2
            fi
        done

        if [[ -r "$(pci_path "$function")/power/control" ]]; then
            value=$(tr '\000\r\n' ' ' <"$(pci_path "$function")/power/control" 2>/dev/null || true)
            printf '  %-20s %s\n' 'power/control:' "${value:-<empty>}" >&2
        fi

        if [[ -r "$(pci_path "$function")/resource" ]]; then
            printf '  resource:\n' >&2
            sed 's/^/    /' "$(pci_path "$function")/resource" >&2 2>/dev/null || true
        fi

        if command -v "$LSPCI_BIN" >/dev/null 2>&1; then
            printf '  lspci -vvnnk:\n' >&2
            "$LSPCI_BIN" -vvnnk -s "$function" 2>&1 | sed 's/^/    /' >&2 || true
        fi
    done
}

'''

replace_once(
    "incus-gpu",
    "print_vfio_state() {\n",
    diagnostic_helpers + "print_vfio_state() {\n",
)

replace_once(
    "incus-gpu",
    "            relation='unbound-bridge'\n",
    "            relation='bridge-topology'\n",
)

new_kernel_log = r'''print_kernel_vfio_log() {
    local bdf=$1 since_epoch=${2:-0} short_bdf pattern output='' filtered=''

    # Tests and alternate sysfs fixtures must never read the real host log.
    if [[ "$SYSFS_ROOT" != "/sys" || "$PROC_ROOT" != "/proc" ]]; then
        return 0
    fi

    short_bdf=${bdf#0000:}
    pattern="${bdf}|${short_bdf}|vfio[-_]pci|vfio|iommu|AMD-Vi|amdgpu|probe.*fail|BAR|resource|vgaarb|aperture"

    if command -v journalctl >/dev/null 2>&1; then
        if (( since_epoch > 0 )); then
            output=$(journalctl -k -b --since "@${since_epoch}" --no-pager -o short-monotonic 2>/dev/null || true)
        else
            output=$(journalctl -k -b -n 1200 --no-pager -o short-monotonic 2>/dev/null || true)
        fi
    fi

    if [[ -z "$output" ]] && command -v dmesg >/dev/null 2>&1; then
        output=$(dmesg --color=never 2>/dev/null | tail -n 1200 || true)
    fi

    filtered=$(printf '%s\n' "$output" | grep -Ei -C 3 "$pattern" | tail -n 220 || true)
    if [[ -n "$filtered" ]]; then
        printf '%s\n' '--- Relevant kernel log ---' >&2
        printf '%s\n' "$filtered" >&2
    else
        warn "関連するカーネルログを取得できませんでした。必要なら sudo journalctl -k -b で確認してください。"
    fi

    if (( since_epoch > 0 )) && [[ -n "$output" ]]; then
        printf '%s\n' '--- Kernel log tail after VFIO operation ---' >&2
        printf '%s\n' "$output" | tail -n 100 >&2
    fi
}'''

regex_once(
    "incus-gpu",
    r'''print_kernel_vfio_log\(\) \{\n.*?\n\}\n\nprint_project_log_command\(\) \{''',
    new_kernel_log + "\n\nprint_project_log_command() {",
)

old_prebind_attach = '''    if (( prebind_requested )); then
        info "Incus起動前にGPU関連PCI機能をvfio-pciへ事前バインドします。"
        prebind_vfio_functions "$bdf" "$vfio_timeout" ||             die "VFIO事前バインドに失敗したため、Incusデバイスは追加しません。"
    fi
'''
new_prebind_attach = '''    if (( prebind_requested )); then
        info "Incus起動前にGPU関連PCI機能をvfio-pciへ事前バインドします。"
        if ! prebind_vfio_functions "$bdf" "$vfio_timeout"; then
            if (( VM_ORIGINALLY_RUNNING && ! DRY_RUN )); then
                warn "事前バインド失敗のため、元の構成でVM '$vm' を再起動します。"
                incus_read start "$vm" || warn "元の構成でもVMを再起動できませんでした。"
            fi
            die "VFIO事前バインドに失敗したため、Incusデバイスは追加しません。"
        fi
    fi
'''
replace_once("incus-gpu", old_prebind_attach, new_prebind_attach)

replace_once(
    "incus-gpu",
    '''    if ! incus_mutate config device add "$vm" "$device" gpu gputype=physical "pci=$bdf"; then
        (( prebind_requested )) && restore_prebound_vfio || true
        die "Incus GPUデバイスを追加できませんでした。"
    fi
''',
    '''    if ! incus_mutate config device add "$vm" "$device" gpu gputype=physical "pci=$bdf"; then
        (( prebind_requested )) && restore_prebound_vfio || true
        if (( VM_ORIGINALLY_RUNNING && ! DRY_RUN )); then
            warn "デバイス追加失敗のため、元の構成でVM '$vm' を再起動します。"
            incus_read start "$vm" || warn "元の構成でもVMを再起動できませんでした。"
        fi
        die "Incus GPUデバイスを追加できませんでした。"
    fi
''',
)

replace_once(
    "incus-gpu",
    "  --vfio-timeout SEC    事前バインドの待機秒数（既定: 15）\n",
    "  --vfio-timeout SEC    事前バインドの待機秒数（既定: 15）\n"
    "                       失敗時は直接bindのerrno、PCI/BAR、カーネルログを表示\n",
)

replace_once(
    "tests/test.sh",
    '''    assert_contains "$out" 'vfio-pci' && \\
        assert_contains "$out" '0000:03:00.0/driver_override' && \\
''',
    '''    assert_contains "$out" 'vfio-pci' && \\
        assert_contains "$out" 'drivers/vfio-pci/bind' && \\
        assert_contains "$out" '0000:03:00.0/driver_override' && \\
''',
)

replace_once(
    "tests/test.sh",
    '''        assert_contains "$out" '0000:03:00.0/driver_override' && \\
        assert_contains "$out" 'config device add'
''',
    '''        assert_contains "$out" '0000:03:00.0/driver_override' && \\
        assert_contains "$out" 'drivers/vfio-pci/bind' && \\
        assert_contains "$out" 'config device add'
''',
)

replace_once(
    "README.md",
    "sudo incus-gpu attach ai-vm 0000:41:00.0   --restart   --prebind-vfio   --vfio-timeout 15",
    '''sudo incus-gpu attach ai-vm 0000:41:00.0 \\
  --restart \\
  --prebind-vfio \\
  --vfio-timeout 15''',
)

replace_once(
    "README.md",
    '''- VM起動失敗時、VFIOドライバー、IOMMUグループ、`driver_override`、カーネルログを自動採取
- Incusの短いdriver probe待機を回避する、明示的な`vfio-pci`事前バインド
''',
    '''- VM起動失敗時、VFIOドライバー、IOMMUグループ、`driver_override`、カーネルログを自動採取
- Incusの短いdriver probe待機を回避する、明示的な`vfio-pci`事前バインド
- `vfio-pci/bind`へ直接書き込み、`EINVAL`・`EBUSY`等のカーネル拒否理由を表示
- probe失敗時にPCI BAR、電源状態、リンク状態、IOMMUグループ利用プロセスを採取
- VM停止後の事前バインド・デバイス追加失敗時に、元々稼働中だったVMを自動復旧
''',
)

replace_once(
    "README.md",
    "## ゲスト側\n",
    r'''### 事前バインドが失敗する場合

`bind-vfio`と`attach --prebind-vfio`は、`driver_override`設定後に
`/sys/bus/pci/drivers/vfio-pci/bind`へ直接書き込みます。これにより、
単に「時間内にバインドされなかった」と表示するのではなく、シェルが受け取った
`Invalid argument`、`Device or resource busy`等のエラーも表示します。

失敗時には次も自動採取します。

- GPU本体と同一スロットの関連PCI機能
- PCI vendor/device/subsystem/class/revision
- BARリソース
- PCIeリンク速度と幅
- 電源状態、D3cold、reset method
- `lspci -vvnnk`
- `/dev/vfio/<group>`を使用するプロセス
- 操作直後のカーネルログ

一部の機能だけが既に`vfio-pci`へバインドされている場合は、残留状態として警告します。
VMがGPUを使用していないことを確認したうえで完全に戻す場合:

```bash
sudo incus-gpu release-vfio 0000:41:00.0 --force
```

その後、もう一度単独診断します。

```bash
sudo incus-gpu bind-vfio 0000:41:00.0 --timeout 15
```

## ゲスト側
''',
)

print("v0.1.4 transformation prepared")
