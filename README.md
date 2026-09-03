# incus-gpu-passthrough

Ubuntuホスト上のGPUと、作成済みのIncus仮想マシンを指定して、物理GPUパススルーを設定・解除するBashツールです。

Incusの次の設定を安全側の事前検査付きで実行します。

```bash
incus config device add <VM> <DEVICE> gpu \
  gputype=physical pci=0000:01:00.0
```

## 主な機能

- `/sys`からGPUを列挙し、番号・PCI BDF・製品名で選択
- 作成済みIncus VMかどうかを検証
- IOMMUグループの有無を検証
- boot VGA、ホストで使用中のDRMデバイス、別VMへの重複割当を検出
- Incusクラスタでは、VMを保持するメンバー上で実行しているかを検証
- 稼働中VMを明示指定により停止し、設定後に再起動
- VM起動失敗時、追加したGPUデバイスを自動ロールバック
- GPU設定の一覧表示と解除
- Ubuntu/GRUB向けのIOMMU・VFIO起動設定を明示的なサブコマンドで生成
- `--dry-run`対応

## 対応範囲

- ホスト: Ubuntu系Linux、Bash 4.3以降
- Incusインスタンス: 仮想マシン
- GPU方式: `gputype=physical`
- CPU: Intel/AMD x86_64
- 起動設定自動化: GRUB + initramfs-tools

`attach`自体はGRUBを変更しません。ホスト側IOMMUが未設定の場合だけ、内容を確認してから`prepare-host`を実行してください。

## 必要なもの

- Incus CLIとローカルIncusサーバー
- Python 3（IncusのJSON出力解析用）
- PCI/IOMMUパススルー対応CPU・マザーボード・ファームウェア
- BIOS/UEFIでIntel VT-dまたはAMD-Viが有効
- `lspci`は推奨。未導入でもsysfsのvendor/device IDで動作します
- `fuser`は任意。存在する場合、ホスト上のGPU使用プロセス検出に利用します

## インストール

```bash
chmod +x incus-gpu install.sh
sudo ./install.sh
```

インストールせず直接実行することもできます。

```bash
./incus-gpu --help
```

## 最短手順

### 1. GPUを確認

```bash
incus-gpu list
```

出力例:

```text
No.  PCI BDF      IOMMU   HOST DRIVER   BOOT   DEVICE
1    41:00.0      23      nvidia        no     VGA compatible controller: NVIDIA ...
2    61:00.0      31      amdgpu        yes    VGA compatible controller: AMD/ATI ...
```

### 2. VMとGPUの前提を診断

```bash
incus-gpu doctor --vm ai-vm --gpu 1
```

IOMMUグループが無い場合、まずBIOS/UEFIでVT-dまたはAMD-Viを有効にします。その後、Ubuntu側の起動設定が必要なら次を実行します。

```bash
sudo incus-gpu prepare-host
sudo reboot
incus-gpu doctor --vm ai-vm --gpu 1
```

変更内容だけ見る場合:

```bash
sudo incus-gpu prepare-host --dry-run
```

### 3. GPUをVMへ設定

VMが停止中の場合:

```bash
incus-gpu attach ai-vm 1
```

PCI BDFを直接指定する場合:

```bash
incus-gpu attach ai-vm 0000:41:00.0
```

VMが稼働中で、停止後に再起動する場合:

```bash
incus-gpu attach ai-vm 0000:41:00.0 --restart
```

名前の一部でも一意なら指定できます。

```bash
incus-gpu attach ai-vm 'RTX 4090' --restart
```

実際に変更せず確認する場合:

```bash
incus-gpu attach ai-vm 1 --restart --dry-run
```

## 状態確認

```bash
incus-gpu status ai-vm
```

表示内容:

- VM状態
- GPUデバイス名
- PCI BDF
- `physical`等のGPUタイプ
- VM本体設定かプロファイル由来か
- 現在のホストドライバー
- IOMMUグループ

## GPU設定の解除

GPUが1台だけ設定されている場合:

```bash
incus-gpu detach ai-vm
```

PCI BDF指定:

```bash
incus-gpu detach ai-vm 0000:41:00.0
```

Incusデバイス名指定:

```bash
incus-gpu detach ai-vm --device gpu-41-00-0
```

稼働中VMを停止し、解除後に再起動:

```bash
incus-gpu detach ai-vm --device gpu-41-00-0 --restart
```

プロファイル由来のGPUデバイスはVM本体から削除できないため、該当プロファイル側で解除してください。

## コマンド

```text
incus-gpu list
incus-gpu doctor [--vm VM] [--gpu GPU]
incus-gpu attach VM GPU [options]
incus-gpu detach VM [GPU] [options]
incus-gpu status VM
incus-gpu prepare-host [--yes|--dry-run|--undo]
```

`attach`は短縮形も使えます。

```bash
incus-gpu ai-vm 1 --restart
```

## attach / detach オプション

| オプション | 内容 |
|---|---|
| `--device NAME` | Incusデバイス名を指定 |
| `--project NAME` | Incusプロジェクトを指定 |
| `--stop` | 稼働中VMを停止し、停止したままにする |
| `--restart` | 稼働中VMを停止し、処理後に再起動する |
| `--start` | 処理後にVMを起動する |
| `--timeout SEC` | 通常停止の待機秒数。既定60秒 |
| `--force-stop` | 通常停止失敗時に強制停止 |
| `--force` | boot VGA、使用中GPU、重複割当等の保護を解除 |
| `--keep-on-failure` | GPU追加後のVM起動失敗時に設定を残す |
| `--dry-run` | 変更コマンドを表示するだけ |

`--force`は、内容を理解している場合にだけ使用してください。特にboot VGAやホストの表示サーバーが使っているGPUを外すと、ホスト画面が消える可能性があります。

## prepare-hostが変更するもの

Intelの場合、次のGRUBスニペットを作成します。

```text
/etc/default/grub.d/99-incus-gpu-passthrough.cfg
```

内容の要点:

```bash
GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} intel_iommu=on iommu=pt"
```

AMDの場合は`amd_iommu=on iommu=pt`になります。

VFIOモジュール設定:

```text
/etc/modules-load.d/incus-gpu-vfio.conf
```

```text
vfio
vfio_pci
vfio_iommu_type1
```

その後、次を実行します。

```bash
update-initramfs -u
update-grub
```

この処理はGPUをvendor/device IDで恒久的に`vfio-pci`へ固定しません。同一型番GPUを複数搭載した環境で全台を奪わないようにし、IncusがVM起動時に対象GPUを切り替える方式を使います。

作成した起動設定を削除する場合:

```bash
sudo incus-gpu prepare-host --undo
sudo reboot
```

## 安全動作

### VM停止を暗黙に行わない

物理GPUデバイスはVMへホットプラグできないため、稼働中VMに対しては何も指定しなければエラーにします。

```bash
incus-gpu attach ai-vm 1 --restart
```

または:

```bash
incus-gpu attach ai-vm 1 --stop
```

### 起動失敗時のロールバック

`--restart`または`--start`で、GPU追加後のVM起動に失敗した場合:

1. 今回追加したGPUデバイスを削除
2. 元々稼働していたVMなら、GPU追加前の構成で再起動を試行
3. エラーを返す

失敗した設定を残して調査したい場合だけ、`--keep-on-failure`を付けます。

### IOMMUグループ検査

GPUと同じIOMMUグループに、GPU基板上の音声・USB機能以外のPCIエンドポイントが含まれる場合は停止します。PCIeブリッジは警告対象から除外します。

## ゲスト側

GPUを設定しただけでは、ゲストOS内のドライバーは自動導入されません。VM内で対象GPU用のNVIDIA、AMD、Intelドライバーを導入してください。

計算用途では、ゲスト内で次を確認します。

```bash
lspci -nnk
```

NVIDIAの場合:

```bash
nvidia-smi
```

物理モニターへ出力する用途では、ゲストOSがIncusの仮想GPUではなくパススルーGPUを表示用として選ぶよう、ゲスト側の表示設定が別途必要になる場合があります。

## クラスタ

GPUはホスト固有のPCIデバイスです。Incusクラスタでは、対象VMが存在するクラスタメンバーへSSH等で入り、そのメンバー上で本ツールを実行してください。別メンバー上での実行は事前検査で拒否します。

## テスト

実機を変更しないモック試験:

```bash
make test
```

テストは一時sysfs、IOMMUグループ、Incus JSON/APIモックを作り、次を確認します。

- GPU列挙
- 停止VMへの追加
- 同一設定の冪等性
- 状態表示
- 解除
- 稼働中VMの保護
- 停止・再起動
- 重複割当防止
- boot VGA保護
- `prepare-host --dry-run`
- `doctor`
- GPU追加後の起動失敗ロールバック
- Incusクラスタの実行メンバー不一致防止

合計13項目を検証します。

## 参考

- Incus GPU device: https://linuxcontainers.org/incus/docs/main/reference/devices_gpu/
- Incus PCI device: https://linuxcontainers.org/incus/docs/main/reference/devices_pci/
- Incus instance options: https://linuxcontainers.org/incus/docs/main/reference/instance_options/
