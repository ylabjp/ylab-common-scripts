# ylab-common-scripts

ラボ共通ライブラリ `ylabcommon`。顕微鏡データの読み込み、解析 config、
共通クラスを提供する。構成は [README.md](README.md) を参照。

ラボ全体の規約は [ylabjp/general](https://github.com/ylabjp/general) の
`docs/research-guidelines/` が正。このファイルはこのリポジトリ固有の指示だけを書く。
新しく作る Markdown の見出しは英語で書く（本文は日本語でよい）。**既存の見出しは改名しない**——
アンカーが変わって他リポジトリからのリンクが切れる（正:
[writing-rules.md の Heading language](https://github.com/ylabjp/general/blob/main/docs/research-guidelines/50-publishing/writing-rules.md#heading-language)）。

**ここを変えると behavior-analysis / behavior-config / slice-analysis /
slice-controller が同時に影響を受ける。** 各リポジトリは git 参照で pin して
いるので、破壊的な変更を入れるときは呼び出し側を先に確認すること。

## ブランチ運用

**`main` に直接コミット・push しない。** ブランチを切って Pull Request を通す。
**Claude は編集を始める前に、いまいるブランチが `main` でないことを確認すること。**
`main` にいたらブランチを切ってから作業する。**自分が開いた PR を自分でマージしない**
——レビューはループの唯一のチェックポイントなので、人に残す。

### ブランチ名には作業者のイニシャルを入れる（必須）

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

## このファイルについて

**最小構成である。** いまはブランチ運用しか書いていない。このリポジトリ固有の
規約（検証の走らせ方、置いてよいもの、命名など）が決まったらここに足す。
ラボ横断の規約は general 側に書き、ここには写さない。
