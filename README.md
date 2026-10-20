# incus-gpu-passthrough

**0.2.0:** 停止する環境では `inspect VM GPU --project NAME` で読み取り専用の一括診断を実行してください。末尾の「0.2.0」節に変更点と検証範囲を記載しています。

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
- VM起動失敗時、VFIOドライバー、IOMMUグループ、`driver_override`、カーネルログを自動採取
- Incusの短いdriver probe待機を回避する、明示的な`vfio-pci`事前バインド
- `vfio-pci/bind`へ直接書き込み、`EINVAL`・`EBUSY`等のカーネル拒否理由を表示
- probe失敗時にPCI BAR、電源状態、リンク状態、IOMMUグループ利用プロセスを採取
- VM停止後の事前バインド・デバイス追加失敗時に、元々稼働中だったVMを自動復旧
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
- Python 3.8以降（JSON解析、独立した監視プロセス）
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

VFIOへのバインド失敗まで調べる場合は、カーネルログも含めます。カーネルログの閲覧権限がない環境では`sudo`を付けてください。

```bash
sudo incus-gpu doctor --vm ai-vm --gpu 1 --kernel-log
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

Incusが`vfio-pci`への切り替え待機で失敗する環境では、root権限で事前バインドしてから起動できます。

```bash
sudo incus-gpu attach ai-vm 0000:41:00.0 \
  --restart \
  --prebind-vfio \
  --vfio-timeout 15
```

事前バインドだけを単独で確認する場合:

```bash
sudo incus-gpu bind-vfio 0000:41:00.0 --timeout 15
```

ホストドライバーへ戻す場合:

```bash
sudo incus-gpu release-vfio 0000:41:00.0
```

## PCI操作が停止する場合

新しいbind/unbindを重ねず、`inspect VM GPU --project NAME` で読み取り専用の
診断レポートを採取してください。PID選択、別々のjournalctl/lspci実行は不要です。
読み取り・書き込み・変更要求のタイムアウトを区別して記録し、未完了の可能性が
ある操作に自動復元・再起動を重ねません。詳しくは末尾の0.2.0節を参照してください。

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
- IOMMUグループ（未バインドのホストブリッジなど、割当不要なブリッジは除外）

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
incus-gpu doctor [--vm VM] [--gpu GPU] [--project NAME] [--kernel-log]
incus-gpu attach VM GPU [options]
incus-gpu detach VM [GPU] [options]
incus-gpu status VM
sudo incus-gpu bind-vfio GPU [--timeout SEC] [--force|--dry-run]
sudo incus-gpu release-vfio GPU [--force|--dry-run]
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
| `--prebind-vfio` | Incus起動前にGPUと同一スロットの関連機能を`vfio-pci`へ事前バインド |
| `--vfio-timeout SEC` | 事前バインド完了を待つ秒数。既定15秒 |
| `--keep-on-failure` | GPU追加後のVM起動失敗時に設定を残す |
| `--dry-run` | 変更コマンドを表示するだけ |

`--force`は、内容を理解している場合にだけ使用してください。特にboot VGAやホストの表示サーバーが使っているGPUを外すと、ホスト画面が消える可能性があります。

## doctorオプション

| オプション | 内容 |
|---|---|
| `--vm VM` | 対象VMの種類、状態、配置メンバー、設定済みGPUを確認 |
| `--gpu GPU` | 対象GPU、IOMMUグループ、グループ内PCI機能を確認 |
| `--project NAME` | Incusプロジェクトを指定 |
| `--kernel-log` | GPU、VFIO、IOMMU、AMD GPUに関係する直近のカーネルログを表示 |

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

`--restart`または`--start`で、GPU追加後のVM起動要求が明確な失敗を返した場合（タイムアウトで状態不明の場合を除く）:

1. 今回追加したGPUデバイスを削除
2. PCI状態の復元も成功し、元々稼働していたVMなら、GPU追加前の構成で再起動を試行
3. エラーを返す

失敗した設定を残して調査したい場合だけ、`--keep-on-failure`を付けます。

起動失敗時は、ロールバック前に次の情報も自動表示します。

- `vfio-pci`ドライバーが登録されているか
- IOMMUグループ番号と`/dev/vfio/<group>`の有無
- グループ内各PCI機能のクラス、現在のドライバー、`driver_override`
- 対象GPU、VFIO、IOMMUに関係する起動試行直後のカーネルログ
- `--project`を含む正しい`incus info --show-log`コマンド

### IOMMUグループ検査

GPUと同じIOMMUグループに、GPU基板上の音声・USB機能以外のPCIエンドポイントが含まれる場合は停止します。PCI-to-PCIブリッジ、およびホストドライバーへバインドされていないホストブリッジ等のブリッジクラスは、VFIOへ割り当てるエンドポイントではないため警告対象から除外します。

## VFIOバインド失敗の調査

`Device took too long to activate` は、Incusが期待したGPUのVFIO状態を確認
できなかったことを示します。それだけでGPU固有のprobe不具合、BAR競合、
IOMMU分離不良などのどれかを断定することはできません。

```bash
sudo incus-gpu inspect ai-vm 0000:41:00.0 --project my-project
```

この1回で、既存プロセス、PCI状態、VM状態、Incusログ/実行中操作、カーネル
ログを採取します。カーネルログはCall Traceを失わないように保存します。
`report.txt` に取得不能項目、停止した読取り操作、ワーカーのstackも記録します。

`inspect --probe` と明示した場合だけ、VM停止とVFIOバインド試験を実行します。
診断中に実行中操作が見つかった場合や読取りがタイムアウトした場合は、追加の
PCI変更を行いません。GPU本体と音声機能の部分的なバインド状態だけを見て
無条件に `release-vfio --force` を実行することは推奨しません。

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
- `doctor`のVFIO状態表示
- GPU追加後の起動失敗ロールバック
- 起動失敗時のプロジェクト付き診断案内
- Incusクラスタの実行メンバー不一致防止
- GPUと同一グループの未バインドホストブリッジを誤検出しないこと
- GPUと同一グループの実エンドポイントは引き続き拒否すること
- 応答しないsysfs bindから監視時間内に制御が戻ること
- `bind-vfio --dry-run`がGPU本体と同一スロット機能だけを対象にすること
- `attach --prebind-vfio --dry-run`がIncus設定前にVFIO操作を組み立てること

CLI回帰20項目とPython監視処理19項目を検証します。実GPU検証とは別です。

## 参考

- Incus GPU device: https://linuxcontainers.org/incus/docs/main/reference/devices_gpu/
- Incus PCI device: https://linuxcontainers.org/incus/docs/main/reference/devices_pci/
- Incus instance options: https://linuxcontainers.org/incus/docs/main/reference/instance_options/


## 0.2.0: 停止する実機の調査方法を見直し

以前の `inspect` は、診断のための `driver_override` 読み取りや `lspci` 自体が
止まるケースを監視できず、VMを止めてさらにPCI操作を重ねていました。
旧版の「driver_override停止試験」はdry-runの文字列確認であり、実際の停止を
検証していませんでした。`driver=-` という直前の表示だけで、停止したsyscallや
GPU/カーネルの根本原因を断定することはできません。

**0.2.0の `inspect` は既定で読み取り専用です。VM停止、bind/unbind、reset、
GRUB更新、ホスト再起動は行いません。** 以前の版から実行中の処理を勝手にkill
することもありません。既存の処理のPID、状態、wchan、syscall、kernel stack、
PCIの各属性、GPUの同一スロット機能、VM状態、Incus操作、カーネルログを
期限付きで採取し、表示したローカルディレクトリの `report.txt` にまとめます。
読み取りが停止した場合も、その操作名とワーカーのスタックを記録します。

リポジトリ内から、更新と診断を一度に実行できます。再インストールは不要です。

```bash
git pull --ff-only && sudo ./incus-gpu inspect VM 0000:01:00.0 --project PROJECT
```

`result=COLLECTED` は採取が完了したという意味であり、パススルー成功では
ありません。`summary.json` の `passthrough_verified` は読み取り診断では常に
`false` です。取得できなかった任意項目と、タイムアウトは区別して記録します。
レポートは所有者専用ディレクトリ（0700）に保存し、外部へ自動送信しません。
カーネルログ・VMログを含むため、共有前に内容を確認してください。

実際にVMを停止してVFIOバインドと復元を試験する機能は、`inspect --probe` と
明示した場合だけ実行します。`--dry-run` は実機成功の証明にはなりません。

### 実行と復元の変更

- sysfsの**読み取りも書き込みも**別プロセスへ隔離し、単調増加時計で監視します。
  ワーカー出力は通常ファイルへ分離し、残った子プロセスが端末やパイプのEOFを
  保持して呼出元を巻き込むことを避けます。子プロセスの終了を無期限には待ちません。
- `attach --prebind-vfio` は全関連機能の変更前状態を**VM停止前に**採取します。
  採取不能を未バインドや空のoverrideとして扱いません。音声機能がGPU本体と別の
  IOMMUグループでも同一PCIスロットの機能を対象にします。
- `driver_override` はunbindより前に設定します。復元時は保存した元ドライバーに
  明示的にbindし、その後に保存したoverrideを戻します。元ドライバーとoverrideが
  異なる場合、復元失敗、途中失敗を成功として扱いません。
- PCI書き込みやIncus変更要求がタイムアウトしたら、**カーネル/サーバー操作の
  取り消しに成功したとは判断しません**。同じ実行内で復元やVM再起動を重ねず、
  変更状態が不確定であることを保存します。`inspect` は残存プロセス・属性・必要に
  応じてIncusの実行中操作を確認して、解決済みのツール内部マーカーだけを照合解除
  します。操作中プロセスが残る場合は追加のPCI変更を止めます。
- `attach` 等の実行全体も監視します（既定180秒、
  `INCUS_GPU_TOTAL_TIMEOUT` で変更可能）。ホストのカーネル全体が停止する場合まで
  ユーザー空間ツールで回復できるという保証はありません。`D`状態だけを根拠に
  自動再起動やPCIバスリセットは行いません。

Bashコマンドに加えPython標準ライブラリの補助実装を使用します。Python 3.8以降が
必要です。`install.sh` は両方を配置します。単体Bashファイルだけのコピーではなく、
リポジトリから直接実行するかインストーラーを使用してください。

### 検証範囲

`make test` で既存CLIの回帰試験とPython監視処理の試験を実行します。
FIFOを本当にブロックさせる読取り/書込み試験、TERM無視、出力を保持する子プロセス、
別IOMMUグループの同一スロット機能、変更前スナップショットの途中失敗、復元失敗、
ステージングインストールを検証します。終了不能なカーネルタスクは別の注入テストで
扱い、FIFOのテスト結果を実際のD状態や実GPUでの成功と混同しません。

Linux VFIOのグループ/デバイスの区別は
https://docs.kernel.org/driver-api/vfio.html を参照してください。
`/dev/vfio/GROUP` が存在しても、GPU本体のバインド成功を意味しません。
