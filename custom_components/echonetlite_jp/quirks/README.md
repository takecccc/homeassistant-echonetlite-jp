# 機種固有の対応 (quirks)

MRA に載っていない、または MRA どおりに動かない機器への対応を、このディレクトリの JSON ファイルで追加します。
ファイル名は自由です（`<メーカー>_<商品コード>_<EOJ>.json` を推奨）。起動時に全ファイルを読み込み、内容に誤りのあるファイルはエラーログを出して無視します。

## 照合

`match` に書いた条件をすべて満たす ECHONET オブジェクトに適用されます。

| キー | 必須 | 内容 |
| --- | --- | --- |
| `eoj` | ○ | クラスコード (クラスグループ + クラス)。例: `"0x0287"` |
| `manufacturer` | | メーカーコード (EPC `0x8A`)。例: `"0x00000B"` |
| `productCode` | | 商品コード (EPC `0x8C`、前後の空白を除いた文字列)。例: `"MKN7350S1"` |

複数のファイルが一致した場合は、条件の少ないものから順に重ねて適用されます（クラスのみ → メーカー指定 → 商品コード指定）。

メーカーコードと商品コードは `uv run scripts/echonet_cli.py scan` の `maker=` と `product=` で確認できます。

## 書けること

```json
{
  "description": "どの機種の何に対応するかのメモ",
  "match": {"eoj": "0x0287", "manufacturer": "0x00000B", "productCode": "MKN7350S1"},
  "maxProperties": 8,
  "elProperties": [
    {
      "epc": "0xF0",
      "propertyName": {"ja": "独自計測値", "en": "Vendor measurement"},
      "shortName": "vendorMeasurement",
      "accessRule": {"get": "required", "set": "notApplicable", "inf": "optional"},
      "data": {"type": "number", "format": "uint16", "minimum": 0, "maximum": 65533, "unit": "W"}
    },
    {
      "epc": "0xC8",
      "homeassistant": {"enabled": false}
    },
    {"epc": "0xF9", "remove": true}
  ]
}
```

| キー | 内容 |
| --- | --- |
| `className` / `shortName` | クラス名の上書き。MRA にないクラス（クラスグループ `0x0F` のメーカー独自クラスなど）を取り込むときに指定します。`0x0F` のオブジェクトは、一致する quirk がある場合だけ取り込まれます |
| `maxProperties` | 1 回の GET で要求するプロパティ数の上限。多数のプロパティを一度に要求すると応答しない機器向け |
| `extension` | クラス拡張の名前（`echonet/classes/__init__.py` の `EXTENSIONS`）。`null` で既定の拡張を無効化 |
| `elProperties` | プロパティの追加・上書き・削除（下記） |

### elProperties

MRA の `elProperties` と同じ形式です（`$ref` で `mra_data/definitions/definitions.json` の定義も参照できます）。

- MRA にない EPC を追加するときは `data` が必須です。
- MRA にある EPC では、書いたキーだけが上書きされます。名前だけ、`data` だけの変更もできます。
- `"remove": true` でその EPC をエンティティにしなくなります。
- `homeassistant` で表示を調整できます:
  - `enabled`: `false` で無効状態で登録
  - `category`: `"diagnostic"`、`"config"`、`null`（通常）

## 追加の手順

1. CLI で対象機器の値を確認する（`scan`、`get`）
2. quirk ファイルを書く
3. `tests/` に、疑似機器 (`tests/fake_node.py`) でその機器の値を再現したテストを追加する
4. `uv run pytest`
