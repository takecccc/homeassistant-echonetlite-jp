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
export DATABASE_URL='postgresql://postgres:password@localhost:5432/hems'
uv run python -m hems_echonet.main collect --once
```

### 2-2. 定期取得（30秒間隔）

```bash
uv run python -m hems_echonet.main collect --interval 30
```

`collect` は、`--host` 未指定時にマルチキャスト探索でホストを検出し、`--eoj` 未指定時は検出された全EOJを定期取得します。
取得方式は raw EPC 固定です（`update()` は使用しません）。

デバッグ用に特定ホスト/EOJへ絞る場合:

```bash
uv run python -m hems_echonet.main collect --host 192.168.1.50 --eoj 028701,027B01 --once
```

`collect` は同時に PostgreSQL に以下を保存します。

- `devices`（`device_uid` 主キー）
- `device_addresses`（`device_uid` と IP の履歴）
- `samples_raw`（生のEPC値）

DSNを明示する場合:

```bash
uv run python -m hems_echonet.main collect --dsn postgresql://postgres:password@localhost:5432/hems --once
```

DB未構築時に表示だけ確認したい場合（保存なし）:

```bash
uv run python -m hems_echonet.main collect --once
```

`collect` の運用方針:

1. `discover(host)` / `getAllPropertyMaps` は起動時に実施
2. テレメトリEPCは `--interval` で定期取得
3. 失敗時は `--rediscover-on-error`（既定: 有効）で再探索
4. `--refresh-interval`（既定: 86400秒）で日次リフレッシュ

## 2-3. DB内容の確認

`DATABASE_URL` を設定済みなら、次で保存内容を確認できます。

### テーブル一覧

```bash
psql "$DATABASE_URL" -c "\dt"
```

### 登録済み機器（UID）

```bash
psql "$DATABASE_URL" -c "SELECT device_uid, manufacturer, product_code, first_seen, last_seen FROM devices ORDER BY last_seen DESC;"
```

### IP履歴（DHCP変化確認）

```bash
psql "$DATABASE_URL" -c "SELECT device_uid, ip, active, first_seen, last_seen FROM device_addresses ORDER BY device_uid, last_seen DESC;"
```

### 最新の生データ

```bash
psql "$DATABASE_URL" -c "SELECT collected_at, device_uid, ip, eoj, epc_key, value_json FROM samples_raw ORDER BY collected_at DESC LIMIT 100;"
```

### 直近1時間の件数

```bash
psql "$DATABASE_URL" -c "SELECT date_trunc('minute', collected_at) AS t, count(*) FROM samples_raw WHERE collected_at > now() - interval '1 hour' GROUP BY 1 ORDER BY 1;"
```

## 2-4. DB初期化

### DB作成（未作成の場合）

```bash
createdb hems
```

### スキーマ初期化（テーブル作成）

このプロジェクトは `collect` 実行時に `devices / device_addresses / samples_raw` を自動作成します。  
テーブルだけ先に作る場合は、1回取得を実行します。

```bash
export DATABASE_URL='postgresql://postgres:password@localhost:5432/hems'
uv run python -m hems_echonet.main collect --once
```

### 既存データを全消去して再作成

```bash
psql "$DATABASE_URL" -c "DROP TABLE IF EXISTS samples_raw, device_addresses, devices CASCADE;"
uv run python -m hems_echonet.main collect --once
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
