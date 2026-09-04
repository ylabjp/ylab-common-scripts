# ylab-common-scripts

ラボ共通ライブラリ `ylabcommon`。顕微鏡データの読み込み、解析 config、
共通クラスを提供する。構成は [README.md](README.md) を参照。

ラボ全体の規約は [ylabjp/general](https://github.com/ylabjp/general) の
`docs/research-guidelines/` が正。このファイルはこのリポジトリ固有の指示だけを書く。

**ここを変えると behavior-analysis / behavior-config / slice-analysis /
slice-controller が同時に影響を受ける。** 各リポジトリは git 参照で pin して
いるので、破壊的な変更を入れるときは呼び出し側を先に確認すること。

## Branch workflow

**`main` に直接コミット・push しない。** ブランチを切って Pull Request を通す。
**Claude は編集を始める前に、いまいるブランチが `main` でないことを確認すること。**
`main` にいたらブランチを切ってから作業する。**自分が開いた PR を自分でマージしない**
——レビューはループの唯一のチェックポイントなので、人に残す。

### Branch names carry the operator's initials (required)

**Claude はブランチを作る前に、作業者のイニシャルを聞くこと。聞かずに作らない。**
形式は `claude/<イニシャル>-<topic>-<id>`（例 `claude/sy-output-name-fix-a1b2c3`）。

AI セッションから出た PR の作成者は、そのセッションを回したアカウントになる。
つまり **「誰の依頼で走ったか」を持っているのはブランチ名だけ**である。
`claude/*` が並んだときに誰のものか分からなくなるので、入れ忘れない。

- イニシャルは 2〜3 文字の小文字（`sy`、`etn`）。表記は人ごとに固定する
- web から始まったセッションのように**ブランチ名を渡された場合も、
  イニシャルが無ければ付け直す**
- 人が自分で切るときは従来どおり `<name>/<topic>`。名前が入っているので足さない

正: [repository-workflow.md](https://github.com/ylabjp/general/blob/main/docs/research-guidelines/80-operations/repository-workflow.md#ブランチ名)。

## Fill from evidence, never from helpfulness (always on)

**ここは科学研究のリポジトリである。根拠があれば論理的に埋め、根拠が無ければ埋めない。
根拠が無いところは、ユーザに聞いてから生成する。聞かずに埋めない**。
親切に埋めた値は、書かれた瞬間に測った値と見分けがつかなくなり、次に読む人も次に走る AI も
それを事実として扱う。**間違った値は、無い値より悪い**——無い値は少なくとも「無い」と見える。

* **根拠とは、指せるもの**である。リポジトリのファイルと行、Airtable のレコード、
  プロトコルの版、実験ノート、図、あるいはそれらから書き下した導出。埋めるときは
  **値の横に出典か導出を併記する**
* **根拠が無いのに埋めない**。もっともらしい日付・数値・番号・名前・要約・理由を
  「たぶんこうだろう」で生成しない。空欄は `未確認` と明示して残す
* **一般論はこの実験の根拠ではない**。教科書的な標準値・典型的な手順・平均的な文章を
  使うなら一般論と明示し、この実験の値と見分けがつく形にする
* **聞くときは、何が分かれば埋まるかを名指しする**。見つけた候補を並べ、
  「どれか、どれでもないか」を一語で答えられる形にする
* **下書きは求められれば書く**（一本道ドラフト、申請書の初稿）。ただし穴は `未確認` と
  印を付け、根拠と推測を分けて見せ、末尾に「聞くことリスト」を付ける
  （現物確認チェックリストと同じ形）。**書く前に聞ける状況なら、書く前に聞く**

このリポジトリで特に:

* 共通ライブラリの既定値（単位・スケール・ファイル形式の前提）を推測で変えない。根拠は
  呼び出し側 4 リポジトリの実際の使い方で、確認できなければ聞く
* テストの期待値を、通るように書き換えない。期待値の根拠は仕様か実測

正: [ai-principles.md の Fill From Evidence, Never From Helpfulness](https://github.com/ylabjp/general/blob/main/docs/research-guidelines/70-ai/ai-principles.md#fill-from-evidence-never-from-helpfulness)。

## About this file

**最小構成である。** いまはブランチ運用と、常時適用の規則（根拠の無いところを
埋めない）の写ししか書いていない。このリポジトリ固有の規約（検証の走らせ方、
置いてよいもの、命名など）が決まったらここに足す。ラボ横断の規約の本文は general 側に
書き、ここには要点と正へのリンクだけを置く。
