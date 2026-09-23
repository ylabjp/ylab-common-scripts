# 解析の出所を残す (provenance)

Norm source: ylabjp/general research guidelines。図の出所は
[reporting-spec.md](reporting-spec.md) の `source` が担う。ここはそれを **図以外の
工程** (sorter / 前処理 / 集計) へ広げたもの。

## 何のためか

解析は結果を上書きしていく。出来上がったファイルを見ても、**どの版のコードが
作ったのかは分からない**。手法は実際に何度も変わる (位置合わせの修正、既定
アルゴリズムの入れ替え) ので、版が違えば同じ生データから違う数値が出る。後から
辿れないと、作り直すべきものとそのままでよいものの区別が付かない。

## 記録だけを持つ。判断はしない

**これは意図的な線引きである (2026-09-23 の判断)。**

behavior-analysis は記録と判断を 1 つにしていた。`analysis_meta.yaml` に
`schema_version` / `config_hash` / 上流のスナップショットを書き、それを**等値比較**
して「STALE なら自動で上書きする」を決めていた。これで 2 つ困った。

1. **記録に項目を足せない。** 上流スナップショットは記録をまるごと写すので、
   項目を 1 つ足すだけで既存の下流が一斉に STALE になり、大規模な再計算が走る
   (実際に 3,346 件の `analysis_meta.yaml` が対象になる状態だった)。記録を育てたい
   のに、比較に使っているせいで育てられない
2. **機械が上書きを決めてしまう。** 中身を見ていない結果を勝手に作り直す。
   どれがだめかを決められるのは中身を見た人だけである

so: **記録は追記するだけ・誰も等値比較しない。** そうすると項目は自由に足せるし、
「この項目は比較から外す」という場合分けも要らない。**作り直す対象を決めるのは、
記録を読む別のプロセス** (一覧・集計して人が決める) の仕事。

`ylabcommon.provenance` に `is_stale()` のような判断関数を足さないこと。
試験 (`test_the_module_does_not_decide_whether_to_redo_anything`) がそれを守る。

## 形

出力フォルダに `_provenance.jsonl`。**追記のみ**なので、作り直した履歴が残る。

```json
{"schema_version": 1, "stage": "sorter", "created_at": "2026-09-23T10:11:12+09:00",
 "source": {"repo": "slice-analysis", "commit": "<40桁の sha>", "dirty": false,
            "script": "src/.../sorter.py", "config_hash": "<16桁>"},
 "deps": {"package": {"name": "sliceanalysis", "version": "0.1.0"},
          "ylabcommon": {"version": "0.3.1", "commit": "<40桁の sha>"},
          "python": "3.12.9"},
 "config": {...}, "inputs": {...}, "host": "ws-hpc", "user": "shoyag"}
```

型は pydantic (`ProvenanceRecord` / `SourceRef` / `Deps` / ...)。**厳しく検証する
ためではない。** 記録は追記だけなので、**古い版が書いたレコードも新しい版が書いた
レコードも読めなければ困る**。そこで:

* すべての項目に既定値 —— 項目が増える前の古いレコードも読める
* `extra="allow"` —— **知らない項目が来ても捨てずに保持する**。新しい版が足した
  項目を古い版で読んで書き戻しても消えない
* 読めない行は飛ばす

つまり型は「決まった形を強制する」ためではなく、**項目名と意味を 1 か所に書いて
おくため**と、**前後の版と行き違っても壊れないため**に使う。`SCHEMA_VERSION` は
**項目を足しただけでは上げない**(足しても壊れないのがこの形の要点)。

* `commit` は **短縮せず 40 桁**。短縮 sha は将来衝突しうるし復元もできない
  (reporting-spec と同じ方針)。表示側で短くする
* **`dirty: true` は未コミットの変更で走らせた印**で、commit だけでは再現できない
* `config_hash` は「同じ設定で走らせたか」を 1 文字列で突き合わせるため。
  **これ自体は判断をしない** —— 違うと分かるだけで、作り直すかは読む側が決める
* `inputs` は「どの入力から作ったか」の記録。**比較には使わない**
* 取れなくても**解析は止めない**。git が無い環境では `null` を入れて進む。キーごと
  消さないのは「調べていない」と「調べて分からなかった」を区別するため

## 使い方

```python
from ylabcommon import provenance

provenance.record(out_dir, "sorter", config=image_param,
                  package="sliceanalysis", script=__file__)
```

一覧・集計 (読むだけ。何も作り直さない):

```
prov <フォルダ>                # 一覧
prov <フォルダ> --by-commit    # 版ごとの件数 = 作り直す対象を人が決めるための一覧
prov <フォルダ> --stage sorter # 工程で絞る
prov <フォルダ> --json         # 機械可読
```

**この機能より前に作った出力には記録が無い。** 記録の無いフォルダは一覧に出ない。
そこは作り直したときに初めて記録が付く。
