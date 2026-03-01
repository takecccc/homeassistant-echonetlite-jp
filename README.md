# Smart Cosmo Monitor (pychonet)

`pychonet` を使って ECHONET Lite 機器（Smart Cosmo 想定）を探索し、現在値を表示する最小構成です。

## 1. セットアップ

```bash
uv sync
```

ネットワーク制限のある環境では次を使います。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync
```

## 2. 現在値の取得

### 2-1. 1回だけ取得

```bash
uv run python -m hems_echonet.main collect --host 192.168.1.50 --eoj 028801 --once
```

### 2-2. 定期取得（30秒間隔）

```bash
uv run python -m hems_echonet.main collect --host 192.168.1.50 --eoj 028801 --interval 30
```

## 3. LAN内ホスト探索

ECHONET Lite のマルチキャスト探索で応答ホストとオブジェクト一覧（EOJ）を取得します。

```bash
uv run python -m hems_echonet.main scan-hosts
```

必要なら、発見済みホストを CIDR / EOJ で絞り込みできます。

```bash
uv run python -m hems_echonet.main scan-hosts --cidr 192.168.1.0/24 --eoj 027901,028101,028201,028701 --verbose
```

MRA を使って `unknown` EPC 名を補完するには、MRA JSON を展開したディレクトリを指定します。

```bash
uv run python -m hems_echonet.main scan-hosts --mra-dir /path/to/mra
```

主なオプション:

- `--eoj`: 対象EOJ候補（カンマ区切り）。未指定時は全EOJを一覧表示
- `--mra-dir`: 展開済み MRA JSON ディレクトリ。指定時は EPC 名を MRA で補完
- `--discovery-wait`: マルチキャスト応答待ち秒数（既定: `2.0`）
- `--timeout`: 各ホストへの `discover(host)` と `getAllPropertyMaps` タイムアウト秒
- `--limit`: 発見ホストのうち先頭N件のみ表示（既定: `0` = 全件）
- `--verbose`: マルチキャスト探索中の経過時間と応答ホスト数を表示

出力には EOJ の説明に加えて、以下も表示されます。

- `inf-map(0x9D)`: 通知可能 EPC
- `set-map(0x9E)`: 設定可能 EPC
- `get-map(0x9F)`: 取得可能 EPC

`--mra-dir` を指定した場合は、EPC表示を以下の順で解決します。

1. MRA (`propertyName.ja` / `descriptions.ja`)
2. `pychonet` 内蔵の EPC 名
3. `unknown`

## 注意

- `pychonet` のデコード結果は機器実装に依存します。
- ECHONET Lite は通常同一L2セグメント内での利用が前提です。
