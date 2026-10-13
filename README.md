# ECHONET Lite JP

ECHONET Lite 機器を Home Assistant に取り込むカスタム統合です。
機器ごとの個別実装は持たず、ECHONET コンソーシアムの **Machine Readable Appendix (MRA)** に記載されたクラス定義から、エンティティ・単位・値の変換方法を自動的に決めます。

- LAN 内の機器をマルチキャストで自動検出（IP アドレス指定も可）
- 機器のプロパティマップ（Get/Set/状変通知）に基づいてエンティティを自動生成
- 状変通知 (INF) を受信して即時に反映
- UID（識別番号）で機器を識別するため、DHCP で IP が変わっても同じ機器として扱える（応答しなくなった機器はすぐに再探索）
- 機器ごとの応答時間を学習し、失われた要求はすぐ再送する（応答を取りこぼしやすい機器でもポーリングが滞らない）
- 外部ライブラリへの依存なし（ECHONET Lite の通信処理を内蔵）

## 動作要件

- Home Assistant 2024.12 以降
- ECHONET Lite 機器が Home Assistant と同じネットワークセグメントにあること（マルチキャスト `224.0.23.0` が届く必要があります）
- UDP ポート 3610 を使用します。同じポートを使う他の ECHONET Lite 統合とは同時に使えません

## インストール

### HACS

1. HACS → 右上メニュー → **カスタムリポジトリ** に `https://github.com/takecccc/homeassistant-echonetlite-jp` を「統合」として追加
2. **ECHONET Lite JP** をインストールし、Home Assistant を再起動

### 手動

`custom_components/echonetlite_jp` を Home Assistant の `config/custom_components/` にコピーして再起動します。

## 設定

**設定 → デバイスとサービス → 統合を追加 → ECHONET Lite JP** から追加します。

| 項目 | 説明 |
| --- | --- |
| ホスト | 機器の IPv4 アドレス（カンマ区切り）。空欄ならマルチキャストで自動検出 |
| ネットワークインターフェースのアドレス | 複数の NIC がある場合に、マルチキャストに使うインターフェースの IPv4 アドレス。通常は空欄 |
| ポーリング間隔 | 全機器の値を取得する間隔（秒、既定 30） |

設定後も統合の **設定オプション** から変更できます。新しく接続された機器は 30 分ごとの再探索、または機器からのインスタンスリスト通知で自動的に追加されます。

## エンティティ

ECHONET オブジェクト（例: `028701` 分電盤メータリング）ごとに 1 つのデバイスが作られ、プロパティごとに次のエンティティになります。

| プロパティの種類 | 読み取りのみ | 設定可能 |
| --- | --- | --- |
| 2 値の状態（ON/OFF など） | バイナリセンサー | スイッチ |
| 多値の状態（運転モードなど） | センサー（列挙） | セレクト |
| 数値・レベル | センサー | ナンバー |
| 日付・時刻 | センサー | センサー |
| 複合値（電流の R 相/T 相など） | 要素ごとのセンサー | — |

- 単位・倍率・係数（例: 積算電力量 × 積算電力量単位 `0xC2`）は MRA の定義に従って適用されます。「未計測」などの特殊コードは不明 (unknown) として扱います。
- 設定可能な動作状態 (`0x80`) は、デバイス名をそのまま名前にしたメインのスイッチになります。
- 設置場所や時刻設定などの管理用プロパティは「診断」カテゴリに入ります。
- MRA に定義がない EPC（メーカー独自 `0xF0`–`0xFF` など）は、生データ (16 進) のセンサーとして **無効状態** で登録されます。必要なものだけ有効化してください。
- メーカー独自のクラスグループ `0x0F` のオブジェクトは、機種固有の定義 (quirk) がある場合だけ取り込みます。
- 表示名は Home Assistant の言語設定が日本語なら日本語、それ以外は英語になります。エンティティ ID は言語に関係なく英語（例: `sensor.power_distribution_board_metering_ch1_energy`）です。

### 分電盤メータリング (0x0287)

回路ごとの積算電力量・瞬時電力を、リスト形式のプロパティ（`0xB3`/`0xB7`/`0xBA`/`0xBE`）から取得して、チャンネルごとのセンサーにします。チャンネル範囲を設定できる機器では、チャンネル数 (`0xB1`/`0xB8`) に合わせて範囲を指定して読み出します。

### 機種固有の対応 (quirks)

MRA に載っていないメーカー独自プロパティや、MRA どおりに動かない機器への対応は、[custom_components/echonetlite_jp/quirks/](custom_components/echonetlite_jp/quirks/README.md) に JSON ファイルを追加して行います。
クラスコード・メーカーコード・商品コードで対象機器を指定し、MRA と同じ形式でプロパティの追加・上書き・削除ができます。
対応してほしい機器がある場合は、診断情報を添えて Issue を作成してください。

## サービス

MRA でエンティティにならないプロパティの確認や、デバッグに使えます。

- `echonetlite_jp.get_property` — 指定した EPC を読み出し、生データ (`edt`) とデコード結果 (`value`) を返します（レスポンス付きサービス）
- `echonetlite_jp.set_property` — 指定した EPC に 16 進の EDT を書き込みます

```yaml
action: echonetlite_jp.get_property
data:
  device_id: <デバイス ID>
  epc: "0xC0"
```

## トラブルシューティング

- 統合のページの **診断情報をダウンロード** で、各機器のプロパティマップと全プロパティの生データを取得できます。不具合報告に添付してください。
- 詳細なログは `configuration.yaml` で有効にできます。

  ```yaml
  logger:
    logs:
      custom_components.echonetlite_jp: debug
  ```

## コマンドラインツール

Home Assistant を使わずに、同じコア実装で機器と通信できます（UDP 3610 を使うため、Home Assistant と同じホストでは統合を止めてから実行してください）。

```bash
uv sync
uv run scripts/echonet_cli.py scan                          # 機器・オブジェクト・プロパティマップの一覧
uv run scripts/echonet_cli.py dump --host 192.168.1.20      # 全プロパティを JSON で出力
uv run scripts/echonet_cli.py get 192.168.1.20 028701 0xC0  # 指定プロパティを読み出し
uv run scripts/echonet_cli.py set 192.168.1.20 013001 0x80 31
```

## 開発

```bash
uv sync
uv run pytest                                     # テスト（ループバック上の疑似機器と通信）
uv run ruff format custom_components scripts tests
uv run ruff check custom_components scripts tests
uv run mypy                                        # 型チェック (strict)
```

### 構成

```
custom_components/echonetlite_jp/
├── echonet/            Home Assistant に依存しない ECHONET Lite 実装
│   ├── protocol.py     フレーム・EOJ・プロパティマップのエンコード/デコード
│   ├── transport.py    UDP 送受信、TID による応答の対応付け、INF 受信
│   ├── client.py       Get/Set、分割取得と個別取得へのフォールバック
│   ├── mra.py          MRA の遅延読み込みと $ref 解決、quirk の適用
│   ├── quirks.py       機種固有の定義ファイルの読み込みと検証
│   ├── codec.py        MRA スキーマに基づく値のデコード/エンコード
│   ├── device.py       機器 (オブジェクト) の状態
│   ├── network.py      探索・ポーリング・通知の反映
│   └── classes/        MRA だけでは表せないクラス固有処理 (0x0287)
├── mra_data/           同梱の MRA (V1.3.1)
├── quirks/             機種固有の定義 (JSON)
├── properties.py       MRA プロパティ → HA エンティティの対応付け
├── coordinator.py      DataUpdateCoordinator
├── entity.py           エンティティ共通処理
└── sensor.py / binary_sensor.py / switch.py / number.py / select.py
```

## ライセンス

[Apache License 2.0](LICENSE)。同梱の MRA とメーカーコード表の権利表示は [NOTICE](NOTICE) を参照してください。

## クレジット

- 同梱の MRA: Machine Readable Appendix V1.3.1, (C) 2021-2025 Kanagawa Institute of Technology, ECHONET Consortium
- メーカーコード表は [pychonet](https://github.com/scottyphillips/pychonet) (MIT License) に由来します
