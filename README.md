# Smart Cosmo (ECHONET Lite) 取得サンプル

家庭内ネットワーク上の ECHONET Lite 機器に対して、Python で UDP/3610 を使い情報取得する最小サンプルです。  
`uv` でセットアップし、以下を実行できます。

- ノード探索 (`discover`)
- 任意 EOJ/EPC の GET (`get`)
- プロパティマップ取得 (`get-map`)

## 1. セットアップ

```bash
uv sync
```

## 2. 実行

### 2-1. ネットワーク上のノード探索

```bash
uv run python -m hems_echonet.main discover --timeout 4
```

`EPC 0xD6` (インスタンスリストS) を使って、応答したノードの EOJ 一覧を表示します。

### 2-2. 特定機器から 1 つの EPC を取得

```bash
uv run python -m hems_echonet.main get --host 192.168.1.50 --deoj 028801 --epc E7
```

- `--host`: 対象機器IP
- `--deoj`: 対象オブジェクトEOJ (3バイトhex)
- `--epc`: 読み出したいEPC (1バイトhex)

### 2-3. GET可能/SET可能/通知可能 EPC の確認

```bash
uv run python -m hems_echonet.main get-map --host 192.168.1.50 --deoj 028801
```

`EPC 0x9D/0x9E/0x9F` を取得して、対応 EPC 一覧を表示します。

## Smart Cosmo 向けの進め方

1. まず `discover` で対象 IP と EOJ を確認する
2. `get-map` で取得可能 EPC を確認する
3. `get` で必要 EPC を順次読みに行く

## 注意点

- ECHONET Lite は通常同一L2セグメント内で通信します。
- マルチキャスト (`224.0.23.0:3610`) が遮断されると探索できません。
- 機器固有EPCはメーカー仕様書に依存します。
