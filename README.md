# ECHONET Lite JP Integration

一般の ECHONET Lite 機器を探索・収集するための実装です。
Home Assistant のカスタム Integration (`custom_components/echonetlite_jp`) を提供しています。

このリポジトリの主機能は Home Assistant Integration です。  
`hems_echonet.main` のCLIスクリプト群は、手動デバッグ・検証用途として提供しています。

## Home Assistant Integration

### HACS で導入

1. HACS の `Integrations` で `Custom repositories` を開く
2. このリポジトリ URL を追加し、Category は `Integration` を選択
3. `ECHONET Lite JP` をインストール
4. Home Assistant を再起動

その後、Home Assistant UI で以下から追加できます。

- `設定` -> `デバイスとサービス` -> `統合を追加` -> `ECHONET Lite JP`

### 手動で導入

このリポジトリの `custom_components/echonetlite_jp` を Home Assistant 設定ディレクトリへ配置して再起動してください。

```bash
# 例: Home Assistant config dir が /config の場合
cp -r custom_components/echonetlite_jp /config/custom_components/
```

再起動後、Home Assistant UI で同様に追加できます。

設定項目のポイント:

- `host` 未指定: マルチキャスト探索で自動検出
- `eoj` 未指定: 検出した全EOJを収集
- `mra_dir` 未指定時: Integration 同梱の MRA を使用
- `mra_dir` 指定時: 指定ディレクトリの MRA JSON を優先使用
- `exclude_unknown_epcs` 既定 `true`: MRAで名称解決できない unknown EPC をエンティティ化しない
- `exclude_metadata_epcs` 既定 `true`: 識別・静的情報 EPC（例: `0x82/0x83/0x8A-0x8E`）を除外
- `exclude_range_epcs` 既定 `true`: 範囲指定/クエリ系 EPC を除外
- `exclude_auxiliary_epcs` 既定 `true`: 補助 EPC（Atomic 補助・単位/係数系）を除外
- Home Assistant 上のデバイスは `UID + EOJ` 単位で登録
- DHCPでIPが変わっても `UID` ベースで同一デバイスとして追従
- デバイス名: `メーカー + デバイス名 + EOJ説明(EOJ)` で表示
- センサー: 取得した EPC ごとに個別センサーを作成
- センサー値: MRA定義に基づき、可能な範囲で単位・倍率 (`multiple`)・状態(enum)を反映
- スイッチ: `set_map` 内で ON/OFF と判定できる EPC（例: 運転状態 EPC `0x80`）を Switch として作成
- Number: `set_map` 内で数値型(`number`)と判定できる EPC を NumberEntity として作成
- Select: `set_map` 内で状態列挙型(`state/enum`)と判定できる EPC を SelectEntity として作成
- `Status change announcement property map (0x9D)` / `Get property map (0x9F)` / `Set property map (0x9E)` はエンティティとして登録しない
- Set操作:
  - `set_epc` は EDT の16進文字列を直接指定
  - `set_epc_value` は MRA 定義を使って enum/数値(倍率付き)を EDT へ変換して設定

- 全EOJを収集したい場合: `eoj` を空欄
- 絞り込みたい場合: `eoj` に `028701,027901` のように EOJ をカンマ区切りで指定

## Debug スクリプト（手動実行）

以下は Integration 本体とは独立した、手動デバッグ用コマンドです。

### 1. セットアップ

```bash
uv sync
```

ネットワーク制限のある環境では次を使います。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync
```

### 2. 現在値の取得

### 2-1. 1回だけ取得

```bash
uv run python -m hems_echonet.main collect --once
```

### 2-2. 定期取得（30秒間隔）

```bash
uv run python -m hems_echonet.main collect --interval 30
```

`collect` は、`--host` 未指定時にマルチキャスト探索でホストを検出し、`--eoj` 未指定時は検出された全EOJを定期取得します。

デバッグ用に特定ホスト/EOJへ絞る場合:

```bash
uv run python -m hems_echonet.main collect --host 192.168.1.50 --eoj 028701,027B01 --once
```

`collect` は同時に SQLite (`hems_registry.sqlite3`) に以下を保存します。

- `devices`（`device_uid` 主キー）
- `device_addresses`（`device_uid` と IP の履歴）
- `samples_raw`（生のEPC値）

DBパスを明示する場合:

```bash
uv run python -m hems_echonet.main collect --db-path ./debug.sqlite3 --once
```

保存せず表示のみ確認したい場合:

```bash
uv run python -m hems_echonet.main collect --db-path '' --once
```

### 2-3. DB内容の確認

既定DB (`hems_registry.sqlite3`) を確認する例です。

### テーブル一覧

```bash
sqlite3 hems_registry.sqlite3 ".tables"
```

### 登録済み機器（UID）

```bash
sqlite3 hems_registry.sqlite3 "SELECT device_uid, manufacturer, product_code, first_seen, last_seen FROM devices ORDER BY last_seen DESC;"
```

### IP履歴（DHCP変化確認）

```bash
sqlite3 hems_registry.sqlite3 "SELECT device_uid, ip, active, first_seen, last_seen FROM device_addresses ORDER BY device_uid, last_seen DESC;"
```

### 最新の生データ

```bash
sqlite3 hems_registry.sqlite3 "SELECT collected_at, device_uid, ip, eoj, epc_key, value_json FROM samples_raw ORDER BY collected_at DESC LIMIT 100;"
```

### 直近1時間の件数

```bash
sqlite3 hems_registry.sqlite3 "SELECT substr(collected_at,1,16) AS minute, count(*) FROM samples_raw WHERE collected_at >= datetime('now','-1 hour') GROUP BY 1 ORDER BY 1;"
```

### 2-4. DB初期化

### DB作成（未作成の場合）

```bash
uv run python -m hems_echonet.main collect --once
```

### スキーマ初期化（テーブル作成）

このプロジェクトは `collect` 実行時に `devices / device_addresses / samples_raw` を自動作成します。  
テーブルだけ先に作る場合は、1回取得を実行します。

```bash
uv run python -m hems_echonet.main collect --once
```

### 既存データを全消去して再作成

```bash
rm -f hems_registry.sqlite3
uv run python -m hems_echonet.main collect --once
```

### 3. LAN内ホスト探索

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
- `--timeout`: 通信タイムアウト秒
- `--limit`: 発見ホストのうち先頭N件のみ表示（既定: `0` = 全件）
- `--verbose`: マルチキャスト探索中の経過時間と応答ホスト数を表示

出力には EOJ の説明に加えて、以下も表示されます。

- `inf-map(0x9D)`: 通知可能 EPC
- `set-map(0x9E)`: 設定可能 EPC
- `get-map(0x9F)`: 取得可能 EPC

`--mra-dir` を指定した場合は、EPC表示を以下の順で解決します。

1. MRA (`propertyName.ja` / `descriptions.ja`)
2. Integration 内蔵の EPC 名辞書
3. `unknown`

## 注意

- EPCのデコード結果は機器実装に依存します。
- ECHONET Lite は通常同一L2セグメント内での利用が前提です。
