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
        raise SystemExit(f"{path}: expected one match, got {count}: {old[:120]!r}")
    write(path, text.replace(old, new, 1))


def replace_function(path: str, name: str, body: str) -> None:
    text = read(path)
    pattern = re.compile(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise SystemExit(f"{path}: expected one function {name}, got {len(matches)}")
    match = matches[0]
    write(path, text[: match.start()] + body.rstrip() + "\n" + text[match.end() :])


replace_once("incus-gpu", 'VERSION="0.1.4"', 'VERSION="0.1.5"')

replace_once(
    "incus-gpu",
    '''VFIO_BIND_TIMEOUT=15
INCUS_LIST_JSON=""
''',
    '''VFIO_BIND_TIMEOUT=15
SYSFS_WRITE_STUCK_PID=""
SYSFS_WRITE_STUCK_PATH=""
SYSFS_WRITE_STUCK_LABEL=""
SYSFS_WRITE_STUCK_STATE=""
SYSFS_WRITE_STUCK_WCHAN=""
SYSFS_WRITE_STUCK_TMPDIR=""
INCUS_LIST_JSON=""
''',
)

replace_once(
    "incus-gpu",
    '''  --vfio-timeout SEC    事前バインドの待機秒数（既定: 15）
                       失敗時は直接bindのerrno、PCI/BAR、カーネルログを表示
''',
    '''  --vfio-timeout SEC    事前バインドの待機秒数（既定: 15）
                       bind/probe書込みも監視し、カーネル停止時でも制御を返す
''',
)

watchdog = r'''sysfs_write_watchdog() {
    local value=$1 path=$2 label=${3:-sysfs-write} timeout=${4:-$VFIO_BIND_TIMEOUT}
    local work_dir err_file rc_file worker_pid deadline rc err state wchan

    if (( DRY_RUN )); then
        printf '+ write %q > %q  # %s (watchdog=%ss)\n' \
            "$value" "$path" "$label" "$timeout" >&2
        return 0
    fi

    [[ -e "$path" ]] || {
        warn "$label: sysfsパスがありません: $path"
        return 1
    }

    SYSFS_WRITE_STUCK_PID=""
    SYSFS_WRITE_STUCK_PATH=""
    SYSFS_WRITE_STUCK_LABEL=""
    SYSFS_WRITE_STUCK_STATE=""
    SYSFS_WRITE_STUCK_WCHAN=""
    SYSFS_WRITE_STUCK_TMPDIR=""

    work_dir=$(mktemp -d)
    err_file="$work_dir/stderr"
    rc_file="$work_dir/rc"

    (
        set +e
        # stderrを先にリダイレクトし、sysfsのopen/writeエラーも採取する。
        printf '%s\n' "$value" 2>"$err_file" >"$path"
        rc=$?
        printf '%s\n' "$rc" >"$rc_file"
        exit "$rc"
    ) &
    worker_pid=$!
    deadline=$((SECONDS + timeout))

    while [[ ! -s "$rc_file" ]]; do
        if ! kill -0 "$worker_pid" 2>/dev/null; then
            break
        fi

        if (( SECONDS >= deadline )); then
            warn "$label が${timeout}秒を超えて応答しません。監視ワーカーPID=$worker_pid"
            kill -TERM "$worker_pid" 2>/dev/null || true
            sleep 0.5
            kill -KILL "$worker_pid" 2>/dev/null || true
            sleep 0.5

            state=$(ps -o stat= -p "$worker_pid" 2>/dev/null | awk '{print $1}' || true)
            if [[ "$state" == Z* || -z "$state" ]]; then
                wait "$worker_pid" 2>/dev/null || true
                err=$(tr '\n' ' ' <"$err_file" 2>/dev/null || true)
                rm -rf "$work_dir"
                warn "$label をタイムアウトで中断しました。${err:+詳細: $err}"
                return 124
            fi

            wchan=$(cat "/proc/$worker_pid/wchan" 2>/dev/null || true)
            SYSFS_WRITE_STUCK_PID=$worker_pid
            SYSFS_WRITE_STUCK_PATH=$path
            SYSFS_WRITE_STUCK_LABEL=$label
            SYSFS_WRITE_STUCK_STATE=$state
            SYSFS_WRITE_STUCK_WCHAN=${wchan:-unknown}
            SYSFS_WRITE_STUCK_TMPDIR=$work_dir
            disown "$worker_pid" 2>/dev/null || true

            warn "sysfsワーカーがカーネル待ちのまま残っています: pid=$worker_pid state=$state wchan=${wchan:-unknown}"
            warn "このPCI操作はまだ進行中の可能性があるため、復元・VM再起動を自動実行しません。"
            warn "確認: ps -o pid,ppid,stat,wchan:32,cmd -p $worker_pid"
            warn "state=Dのままなら、通常のkillでは解消せずホスト再起動が必要です。"
            return 124
        fi

        sleep 0.1
    done

    if [[ -s "$rc_file" ]]; then
        rc=$(cat "$rc_file" 2>/dev/null || printf '1')
    else
        wait "$worker_pid" 2>/dev/null
        rc=$?
    fi
    wait "$worker_pid" 2>/dev/null || true
    err=$(tr '\n' ' ' <"$err_file" 2>/dev/null || true)
    rm -rf "$work_dir"

    if (( rc == 0 )); then
        return 0
    fi

    warn "$label に失敗しました (exit=$rc, path=$path): ${err:-kernel rejected the write}"
    return "$rc"
}'''

replace_once(
    "incus-gpu",
    '''sysfs_write_checked() {
    local value=$1 path=$2 label=${3:-sysfs-write}
    local err_file rc err
    if (( DRY_RUN )); then
        printf '+ write %q > %q  # %s\\n' "$value" "$path" "$label" >&2
        return 0
    fi
    [[ -e "$path" ]] || {
        warn "$label: sysfsパスがありません: $path"
        return 1
    }

    err_file=$(mktemp)
    if printf '%s\\n' "$value" >"$path" 2>"$err_file"; then
        rm -f "$err_file"
        return 0
    else
        rc=$?
        err=$(tr '\\n' ' ' <"$err_file" 2>/dev/null || true)
        rm -f "$err_file"
        warn "$label に失敗しました (exit=$rc, path=$path): ${err:-kernel rejected the write}"
        return "$rc"
    fi
}
''',
    '''sysfs_write_checked() {
    local value=$1 path=$2 label=${3:-sysfs-write}
    local err_file rc err
    if (( DRY_RUN )); then
        printf '+ write %q > %q  # %s\\n' "$value" "$path" "$label" >&2
        return 0
    fi
    [[ -e "$path" ]] || {
        warn "$label: sysfsパスがありません: $path"
        return 1
    }

    err_file=$(mktemp)
    if printf '%s\\n' "$value" 2>"$err_file" >"$path"; then
        rm -f "$err_file"
        return 0
    else
        rc=$?
        err=$(tr '\\n' ' ' <"$err_file" 2>/dev/null || true)
        rm -f "$err_file"
        warn "$label に失敗しました (exit=$rc, path=$path): ${err:-kernel rejected the write}"
        return "$rc"
    fi
}

''' + watchdog + "\n",
)

replace_function(
    "incus-gpu",
    "bind_pci_function_to_vfio",
    r'''bind_pci_function_to_vfio() {
    local bdf=$1 timeout=$2 current override_path bind_path probe_path bind_epoch rc
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

    # PCI driver probeはカーネル内で長時間停止することがあるため、
    # メインシェルでは実行せず監視付きワーカーへ分離する。
    if [[ -e "$bind_path" ]] || (( DRY_RUN )); then
        if sysfs_write_watchdog "$bdf" "$bind_path" \
            "vfio-pci direct bind ($bdf)" "$timeout"; then
            :
        else
            rc=$?
            if [[ -n "$SYSFS_WRITE_STUCK_PID" ]]; then
                return 124
            fi
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
            if [[ -n "$SYSFS_WRITE_STUCK_PID" ]]; then
                return 124
            fi
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
}''',
)

replace_function(
    "incus-gpu",
    "prebind_vfio_functions",
    r'''prebind_vfio_functions() {
    local bdf=$1 timeout=$2 function original_driver original_override group current rc
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

        if bind_pci_function_to_vfio "$function" "$timeout"; then
            continue
        else
            rc=$?
        fi

        warn "VFIO事前バインドに失敗しました: $function"
        if [[ -n "$SYSFS_WRITE_STUCK_PID" ]]; then
            warn "カーネル内のbind/probeが終了していないため、PCI状態の復元を省略します。"
            warn "対象: $SYSFS_WRITE_STUCK_LABEL path=$SYSFS_WRITE_STUCK_PATH"
            warn "worker: pid=$SYSFS_WRITE_STUCK_PID state=$SYSFS_WRITE_STUCK_STATE wchan=$SYSFS_WRITE_STUCK_WCHAN"
            return 124
        fi

        print_vfio_state "$bdf"
        restore_prebound_vfio || true
        return "$rc"
    done

    print_vfio_state "$bdf"
}''',
)

replace_once(
    "incus-gpu",
    r'''    if (( prebind_requested )); then
        info "Incus起動前にGPU関連PCI機能をvfio-pciへ事前バインドします。"
        if ! prebind_vfio_functions "$bdf" "$vfio_timeout"; then
            if (( VM_ORIGINALLY_RUNNING && ! DRY_RUN )); then
                warn "事前バインド失敗のため、元の構成でVM '$vm' を再起動します。"
                incus_read start "$vm" || warn "元の構成でもVMを再起動できませんでした。"
            fi
            die "VFIO事前バインドに失敗したため、Incusデバイスは追加しません。"
        fi
    fi
''',
    r'''    if (( prebind_requested )); then
        local prebind_rc=0
        info "Incus起動前にGPU関連PCI機能をvfio-pciへ事前バインドします。"
        if prebind_vfio_functions "$bdf" "$vfio_timeout"; then
            :
        else
            prebind_rc=$?
            if [[ -n "$SYSFS_WRITE_STUCK_PID" ]]; then
                warn "PCI probeがカーネル内で継続中のため、VM '$vm' は停止したままにします。"
                warn "別端末で次を確認してください: ps -o pid,ppid,stat,wchan:32,cmd -p $SYSFS_WRITE_STUCK_PID"
                warn "state=Dのままなら、ホスト再起動後にVMを起動してください。"
                die "VFIO bind/probeが監視時間を超過しました（worker PID=$SYSFS_WRITE_STUCK_PID）。"
            fi
            if (( VM_ORIGINALLY_RUNNING && ! DRY_RUN )); then
                warn "事前バインド失敗のため、元の構成でVM '$vm' を再起動します。"
                incus_read start "$vm" || warn "元の構成でもVMを再起動できませんでした。"
            fi
            die "VFIO事前バインドに失敗しました (exit=$prebind_rc)。Incusデバイスは追加しません。"
        fi
    fi
''',
)

replace_once(
    "tests/test.sh",
    '''test_prepare_dry_run() {
''',
    r'''test_bind_write_watchdog_returns() {
    local bind_path="$SYS/bus/pci/drivers/vfio-pci/bind"
    local out_file="$TMP/bind-watchdog.out" out rc elapsed start

    rm -f "$SYS/bus/pci/devices/0000:03:00.0/driver"
    touch "$SYS/bus/pci/drivers_probe"
    rm -f "$bind_path"
    mkfifo "$bind_path"

    start=$SECONDS
    if timeout 8 "$TOOL" bind-vfio 0000:03:00.0 --force --timeout 1 \
        >"$out_file" 2>&1; then
        rc=0
    else
        rc=$?
    fi
    elapsed=$((SECONDS - start))
    rm -f "$bind_path"
    out=$(cat "$out_file")
    if ! (( rc != 0 && elapsed < 5 )); then
        printf 'watchdog timing/status mismatch: rc=%s elapsed=%s\n%s\n' \
            "$rc" "$elapsed" "$out" >&2
        return 1
    fi
    if ! assert_contains "$out" '監視ワーカーPID='; then
        printf 'watchdog PID output missing: rc=%s elapsed=%s\n%s\n' \
            "$rc" "$elapsed" "$out" >&2
        return 1
    fi
    if [[ "$out" != *"タイムアウトで中断しました"* && \
          "$out" != *"sysfsワーカーがカーネル待ち"* ]]; then
        printf 'watchdog timeout output missing: rc=%s elapsed=%s\n%s\n' \
            "$rc" "$elapsed" "$out" >&2
        return 1
    fi
}

test_prepare_dry_run() {
''',
)

replace_once(
    "tests/test.sh",
    '''run_test 'cluster member mismatch is blocked' test_cluster_member_mismatch

printf '\\n%d passed, %d failed\\n' "$pass" "$fail"
''',
    '''run_test 'cluster member mismatch is blocked' test_cluster_member_mismatch
run_test 'blocking sysfs bind returns via watchdog' test_bind_write_watchdog_returns

printf '\\n%d passed, %d failed\\n' "$pass" "$fail"
''',
)

readme_insert = r'''## bind/probe書き込み自体が停止する場合

`vfio-pci/bind`や`drivers_probe`へのsysfs書き込みは、PCIドライバーのprobeが
カーネル内で停止すると、呼び出したシェルまで応答しなくなることがあります。
`0.1.5`以降はこの書き込みを監視付きワーカーへ分離し、`--vfio-timeout`を
超えた時点でメイン処理へ制御を戻します。

ワーカーが通常のシグナルで終了できた場合は、変更済み状態を復元して終了します。
一方、ワーカーが`D`（uninterruptible sleep）のまま残った場合は、処理がまだ
カーネル内で進行中である可能性があるため、競合を避けて次を自動実行しません。

- `driver_override`やPCIドライバーの復元
- 停止したVMの再起動
- Incus GPUデバイスの追加

出力されたPIDを別端末から確認してください。

```bash
ps -o pid,ppid,stat,wchan:32,cmd -p <worker-pid>
```

`STAT`が`D`のままなら、通常の`kill`は処理を即座には終了させません。
ホストを再起動してPCI probeを解消した後、VMを起動してください。

'''
replace_once("README.md", "## 状態確認\n", readme_insert + "## 状態確認\n")

text = read("README.md")
text = text.replace("合計17項目を検証します。", "合計18項目を検証します。")
if "blocking sysfs bind" not in text:
    marker = "- GPUと同一グループの実エンドポイントは引き続き拒否すること\n"
    if marker in text:
        text = text.replace(
            marker,
            marker + "- 応答しないsysfs bindから監視時間内に制御が戻ること\n",
            1,
        )
write("README.md", text)

print("v0.1.5 transformation prepared")
