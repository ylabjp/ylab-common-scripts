# ThorImage `Experiment.xml` の読み方 (リバースエンジニアリングによる仮説)

ThorImageLS の `Experiment.xml` に公開仕様は無い。ここに書いてあるのは
**実データと突き合わせて確かめた仮説** であって、Thorlabs が保証したものではない。

そのため各項目に **確度** を付ける。

| 記号 | 意味 |
|---|---|
| 確認済 | 実データで検証した。反例が出るまでは信じてよい |
| 推定 | 属性名と値から読めるが、その条件のデータをまだ見ていない |
| 仕様外 | XML には答えが無いことが分かっている。別の情報源が要る |

反例が出たらこの表を直すこと。**コードのコメントではなくここが正典** で、
`xml_parser.py` はここを参照する。

- 対象バージョン: `<Software version="4.4.2026.1231"/>` (ThorImageLS 4.4)
- サンプル: [`samples/Experiment_streaming_xyt_ThorImageLS4.4.xml`](samples/Experiment_streaming_xyt_ThorImageLS4.4.xml)
  — 実際の取得の XML。[付録](#付録-サンプル-xml-の要点) に要点を抜き出してある

---

## 宿題 — サンプルが手に入ったら確認すること

**ここが「戻ってくる場所」。** 未確認のまま実装している仮説と、それを確かめるのに
何が要るかを 1 か所に集める。確認できたら本文へ移し、確度を上げること。

### A. fast-Z (`zFastEnable="1"`) の取得がまだ 1 件も無い

fast-Z のデータが 1 件あれば、以下がまとめて決まる。**優先度が最も高い。**

| 確かめること | 期待 | 外れたときの被害 |
|---|---|---|
| `SizeT = frames // steps` か | `frames` は面数なので段数で割る | 時点数が段数倍ずれる |
| `LSM/@NumberOfPlanes` が段数になるか | 平面取得では `1` だった | — (裏取りが増えるだけ) |
| `Streaming/@flybackFrames="1"` の意味 | 各ボリューム末尾に捨てる面が 1 枚ある? | **Z が 1 面ずれる。無視すると全ボリュームが 1 面分回転する** |
| `ZStage/@zStreamFrames` / `@zStreamMode` | 平面取得では `1` / `0`。判別に使えなかった | — |

確かめ方: fast-Z で 2〜3 ボリュームだけ撮り、`Experiment.xml` と生ファイル名一覧
(`dir /b`) を並べる。ファイル名の Z 連番が 1..steps を回るか、`frames` が
面数なのかボリューム数なのかが直接読める。

なお、ここでいう fast-Z は **`ZStage` 側の高速 Z** を指す。remote focus は別方式で、
そちらは下記 D のとおり未導入である。

### B. 片チャンネルだけ有効にした取得

`<ChannelEnable Set>` のビットマスク解釈 (`Set=1` → ChanA のみ) は **推定**。
`Set=2` で ChanB のみになるかを確認したい。外すとチャンネル名がずれる。

確かめ方: ChanB だけ有効にして数枚撮り、`Set` の値と生ファイルの接頭辞を見る。

### C. 途中で止めた取得の `ExperimentStatus`

平面取得の完了時は `<ExperimentStatus value="Complete" />`。途中で止めた取得で
別の値になるなら、「最後の時点が欠けている」の裏取りに使える。**未検証**。

### D. remote focus — **2026-08 時点で未導入 (将来導入予定)**

```xml
<RemoteFocus steps="1" startPlane="0" stepSize="1" IsRemoteFocus="0"
             captureMode="0" customSequenceEnabled="0" />
```

**この装置には remote focus がまだ入っていない。** サンプルの
`IsRemoteFocus="0"` / `steps="1"` は「無効」ではなく **「そもそも搭載していない」
状態の既定値**なので、この属性から remote focus 時の挙動は一切読み取れない。
`steps="1"` を「remote focus では 1 段」の根拠に使わないこと。

現在の Z の読み方 ([Z と T の枠](#z-と-t-の枠--streaming-が主、zstage--timelapse-は従)) は
`ZStage` と `Streaming` の 2 つしか見ていない。remote focus が導入されると
**Z を振る 3 つ目の経路**ができるので、そのときは読み方の見直しが要る。

導入時に確認すること:

- `IsRemoteFocus="1"` のとき、SizeZ は `RemoteFocus/@steps` か `ZStage/@steps` か
- µm/px (Z) は `RemoteFocus/@stepSize` か `ZStage/@stepSizeUM` か
  (単位も違う可能性がある — `stepSize` に `UM` が付いていない)
- `Streaming/@zFastEnable` との関係。排他なのか併用できるのか
- `RemoteFocus/@captureMode` / `@customSequenceEnabled` の意味。
  custom sequence だと Z の並びが等間隔でなくなるかもしれない
  (そうなるとファイル名の Z 連番と物理位置の対応が崩れる)

**導入したらこの節を宿題から本文へ移すこと。** 導入に気付かないまま古い読み方を
続けると、Z スタックの設定が残ったまま Streaming に切り替えた今回と
同じ壊れ方をする (使われない設定を信じて軸を取り違える)。

---

## 大原則: XML は「設定」、ファイルは「結果」

XML は **取得を始める前の画面の状態** を書き出したものである。ある取得で実際に
どの設定が使われたかは、別のノードが決めていることがある。使われなかった設定の値も
そのまま残るので、ノードを単独で読むと嘘を信じることになる。

実際に起きた事故:

```xml
<ZStage steps="61" stepSizeUM="0.5" enable="1" .../>
<Timelapse timepoints="3000" intervalSec="60" .../>
<Streaming enable="1" frames="3000" zFastEnable="0" .../>
```

`ZStage` だけを見て「61 段の Z スタック」と読むと、実際には単一平面の時系列
3001 枚だったものが **3001 時点まるごと Z 軸へ潰れ**、深さ 1500 µm という
あり得ないスタックが黙って出来上がった。

したがって:

- **面の配置 (どの面がどの Z / どの T か) はファイル名の連番が決める。**
  XML は枠の目安としてしか使わない
  (`thorlab_bioio_stack_builder._fill_frame` を参照)。
- 画素の縦横・型は **TIFF ヘッダ** が決める。
- XML から採るのは、ファイルにもヘッダにも書かれていない物理量だけ
  (µm/px、Z step、対物、時刻)。

---

## Z と T の枠 — `Streaming` が主、`ZStage` / `Timelapse` は従

**これが最も間違えやすい。**

```xml
<Streaming enable="1" frames="3000" zFastEnable="0" zFastMode="1" ... />
```

| `Streaming/@enable` | `Streaming/@zFastEnable` | SizeZ | SizeT | 確度 |
|---|---|---|---|---|
| `1` | `0` | **1** | `Streaming/@frames` | 確認済 |
| `1` | `1` | `ZStage/@steps` | `frames // steps` | 推定 ([宿題 A](#a-fast-z-zfastenable1-の取得がまだ-1-件も無い)) |
| `0` | — | `ZStage/@steps` (`enable="1"` のとき) | `Timelapse/@timepoints` | 確認済 |

**要点: `Streaming enable="1"` のとき、`ZStage/@steps` は `zFastEnable="1"` の
ときしか使われない。** 連続取得中に低速な Z ステージを動かすことはできないので、
Z を振るには fast-Z (共振/電気式) が要る。`zFastEnable="0"` なら Z は 1 面である。

`ZStage/@enable="1"` が残っていても意味を持たない。上の例がまさにそれで、
Z スタックの設定を組んだあと Streaming に切り替えたため、使われない `steps="61"` が
残っていた。

`zFastMode="1"` は fast-Z の **方式** (ノコギリ波/階段など) で、有効/無効ではない。
`zFastEnable` と混同しないこと。**この取り違えは `steps` を信じるのと同じ壊れ方をする。**

### `Streaming` が無い場合

古いバージョンや別の取得形式ではノードごと無いことがある。その場合は
`Streaming enable="0"` と同じ扱い (`ZStage` + `Timelapse`) にしている。**推定**。

### Z を振る経路は現状 2 つしかない

上の表が見ているのは `ZStage` (低速 Z) と `Streaming/@zFastEnable` (fast-Z) だけである。
**remote focus は 2026-08 時点で未導入** なので勘定に入れていない。導入されると
3 つ目の経路になり、この表だけでは足りなくなる
([宿題 D](#d-remote-focus--2026-08-時点で未導入-将来導入予定))。

---

## 時間軸 — **XML からは決まらない (仕様外)**

**結論: `Experiment.xml` に正確な時間軸は無い。トリガー記録から別途再構成する。**

XML の値は目安にしかならないので、時間精度が要る解析
(ΔF/F の立ち上がり、刺激との対応など) では **使ってはいけない**。

根拠 — 同じ取得について 3 桁違う値が並んでいる:

```xml
<Timelapse timepoints="3000" intervalSec="60" triggerMode="0" />
<Streaming enable="1" frames="3000" triggerMode="1" ... />
<LSM frameRate="45.638" averageMode="0" averageNum="10" ... />
```

- `Timelapse/@intervalSec="60"` を素直に読むと 3000 時点 × 60 秒 = **50 時間**
- `LSM/@frameRate="45.638"` から読むと 3000 フレーム = **約 66 秒**

さらに、どちらを採っても正しくならない理由がある:

- `Streaming/@triggerMode="1"` は外部トリガとみられる。この場合、面が取得される
  実時刻は **外部の刺激装置が決める** ので、XML のどこにも書かれていない
- `Timelapse/@timepoints` と `Streaming/@frames` がどちらも 3000 なのは、
  ThorImage が一方を他方へ写しているだけの可能性がある
  (Streaming 取得で `Timelapse` 側が使われている証拠は無い)
- `averageNum="10"` が効いていれば実効フレーム周期は 10 倍になるが、
  `averageMode="0"` が「平均なし」を意味するのかは未確認

### 現状の扱い

`TimeIntervalSec` には従来どおり `Timelapse/@intervalSec` をそのまま入れている。
**これは「正しい時間軸」ではなく、後方互換のための置き場所**である。
正確な時間軸はトリガー記録から再構成し、そちらを正とする。

将来ここを触るときの注意: XML 由来の値を「もっともらしい別の値」に差し替えても
問題は解決しない (どれも根拠が無い)。むしろ、どこかで暗黙に使われていたときに
静かに結果が変わる。時間軸を直すなら、トリガー由来の系列を明示的に持ち込むこと。

---

## チャンネル — `ChannelEnable/@Set` のビットマスク

```xml
<Wavelengths nyquistExWavelengthNM="0" nyquistEmWavelengthNM="0">
  <Wavelength name="ChanA" exposureTimeMS="0" />
  <Wavelength name="ChanB" exposureTimeMS="0" />
  <ChannelEnable Set="3" />
</Wavelengths>
```

`<Wavelength>` は **設定されている** 波長を並べるだけで、その取得で有効だったかは
`<ChannelEnable Set>` が持つ。`Set` はビットマスクで、`3 = 0b11` は 1 番目と
2 番目が有効。**確認済** (2 チャンネル分の生ファイルがある)。

`<Wavelength>` を数えるだけだと、片方だけ有効にした取得で「XML は 2 波長だが
実データは 1 チャンネル」という食い違いが出る。ビット位置と波長の対応は
**推定** ([宿題 B](#b-片チャンネルだけ有効にした取得))。

`LSM/@channel="3"` と `PMT/@enableA` `@enableB` も同じ情報を持っているように見える。
冗長なので今は使っていない。

---

## 画素サイズと視野

```xml
<LSM pixelX="512" pixelY="512" pixelWidthUM="0.17" pixelHeightUM="0.17"
     widthUM="86.96" heightUM="86.96" pixelSizeUM="2.93" fieldSize="63" .../>
```

| 項目 | 出どころ | 確度 |
|---|---|---|
| SizeX / SizeY | `LSM/@pixelX` / `@pixelY` | 確認済 |
| µm/px (XY) | `LSM/@pixelWidthUM` / `@pixelHeightUM` | 確認済 |
| µm/px (Z) | `ZStage/@stepSizeUM` の絶対値 | 確認済 |
| 対物 | `Magnification/@name` (`"25xOLY"`) | 確認済 |
| 取得日時 | `Date/@date` (`MM/DD/YYYY HH:MM:SS`) | 確認済 |

罠:

- `LSM/@width` / `@height` は **存在しない**。以前のアダプタがこれを読んでいたため
  SizeX/SizeY が常に既定値の 512 になっていた (このサンプルはたまたま 512 なので
  気付けなかった)
- `LSM/@pixelSizeUM="2.93"` は µm/px **ではない**。`pixelWidthUM` (0.17) と 17 倍
  違う。用途不明。**使わないこと**
- `Camera/@pixelSizeUM="0.25"` は多光子取得では無関係 (`Camera/@width="0"`)
- `widthUM="86.96"` ÷ `pixelX="512"` = 0.1698 ≒ `pixelWidthUM`。整合しているので
  どちらから求めてもよいが、丸めの分だけ `pixelWidthUM` の方が素直

XML とヘッダが食い違ったときは **ヘッダを採る** (XML は設定、ファイルは結果)。
食い違いは警告に出す。

---

## まだ読んでいない / 用途不明のノード

判断に使っていないが、後で要るかもしれないもの。

- `CaptureMode/@mode="1"` — 取得種別らしいが、値の意味は未確認。`Streaming/@enable`
  と重複している可能性がある
- `ExperimentStatus/@value="Complete"` — [宿題 C](#c-途中で止めた取得の-experimentstatus)
- `Streaming/@previewIndex="1"` — **推定**: 取得中の画面表示に使うチャンネルの
  番号。これが `ChanA_Preview.tif` / `ChanB_Preview.tif` の書き出しに対応する
  とみられる (→ [ファイル名の規約](#ファイル名の規約-xml-ではないが対になる知識))。
  値そのものは取り込みに使っていない
- `Streaming/@dmaFrames="1500"` — 転送バッファらしい。`frames` の半分。構造には無関係とみられる
- `RemoteFocus` — **2026-08 時点で未導入 (将来導入予定)**。今の値は搭載していない
  状態の既定値なので何も読み取れない。導入されたら Z の読み方を見直すこと
  ([宿題 D](#d-remote-focus--2026-08-時点で未導入-将来導入予定))
- `Photobleaching`, `SLM`, `Pockels` — 刺激系。取り込みの構造には関与しない。
  ただし **刺激のタイミングは時間軸の再構成に要る** ので、そちらでは読むことになる
- `Sample/@initialStageLocationX` `@initialStageLocationY` — ステージ位置。
  タイル取得を扱うようになったら要る

---

## ファイル名の規約 (XML ではないが対になる知識)

```
ChanA_001_001_001_001.tif
 ^ch  ^X  ^Y  ^Z  ^T
```

- **末尾 2 つの数値が (Z, T)。** 接頭辞の数は当てにしない
  (`Image_ChanA_...` のような形もありうるので、トークン数で決め打ちしない)
- チャンネルは `Chan` または `CH` を含むトークン
- X / Y はタイル (mosaic) の位置。複数値があれば mosaic なので取り込みは拒否する
- 連番の読めないファイルは枠のどの枡も指せないので落とす。1 枚混ざっただけで
  取得全体を諦めてはいけない

### `ChanA_Preview.tif` (取得ごとに必ずある)

ThorImage は取得ディレクトリに `ChanA_Preview.tif` / `ChanB_Preview.tif` を
書き出す。**確認済**:

- 取得中の画面表示のコピーであって、面ではない (`Streaming/@previewIndex` に
  対応するとみられる)
- 連番を持たないので `_thorlabs_zt` は `None` を返す
- **どの取得にも必ずある**。異常ではない

そのため取り込みでは `_is_preview` が **最初に外し、DEBUG に 1 行だけ残す**。
警告にはしない — 毎回必ず鳴る警告は読まれなくなり、本当に見てほしい警告
(連番の読めない見覚えのないファイル、はみ出し、途中で止まった取得) まで
一緒に読み飛ばされるため。

先に外すのには実利もある。`Preview` は名前の並びで最後に来るので、以前は
ヘッダの抜き取り検査 (`_page_counts` は先頭と末尾を見る) が必ずプレビューに
当たっていた。面数もサイズも他と違うため「面数が食い違う」と判定され、
サイズの分からないファイルが 1 つでもあると全件のヘッダを読みに行っていた。

`_fill_frame` の 3 段階:

1. XML が枠 (`SizeT` × `SizeZ`) を決める
2. ファイル名の連番が指す枡を埋める
3. 埋まらなかった分をカットする

枠は **目標であって上限ではない**。はみ出したファイルは捨てずに使い、件数だけ
報告する。XML が当てにならないことが分かっている以上、それを上限にすると実在する
面を落とす。

---

## 付録: サンプル XML の要点

全文: [`samples/Experiment_streaming_xyt_ThorImageLS4.4.xml`](samples/Experiment_streaming_xyt_ThorImageLS4.4.xml)
(`tests/test_thorlab_xml_reading.py` がこの現物を読んで期待値を固定している)

- 取得: 単一平面の連続取得 (XYT)、2 チャンネル、生ファイル 3001 枚 × ch
- 判定: `Streaming enable=1` + `zFastEnable=0` → **SizeZ=1, SizeT=3000**

構造の判定に関わるノードだけを抜き出したもの (属性は間引いてある):

```xml
<ThorImageExperiment>
  <Date date="07/29/2026 12:52:07" uTime="1785297127" />
  <Software version="4.4.2026.1231" />
  <Magnification mag="27.777" name="25xOLY" />

  <Wavelengths>
    <Wavelength name="ChanA" exposureTimeMS="0" />
    <Wavelength name="ChanB" exposureTimeMS="0" />
    <ChannelEnable Set="3" />            <!-- 0b11 = ChanA と ChanB が有効 -->
  </Wavelengths>

  <!-- steps=61 / enable=1 だが、Streaming 中は zFastEnable=0 なので使われない -->
  <ZStage name="ThorDAQZ" steps="61" stepSizeUM="0.5" enable="1"
          zStreamFrames="1" zStreamMode="0" />

  <!-- Streaming 取得では使われていないとみられる (frames と同値なだけ) -->
  <Timelapse timepoints="3000" intervalSec="60" triggerMode="0" />

  <LSM pixelX="512" pixelY="512"           <!-- SizeX / SizeY -->
       pixelWidthUM="0.17" pixelHeightUM="0.17"   <!-- um/px。pixelSizeUM ではない -->
       pixelSizeUM="2.93"                  <!-- um/px ではない。用途不明、使わない -->
       widthUM="86.96" heightUM="86.96"
       frameRate="45.638" averageMode="0" averageNum="10"
       NumberOfPlanes="1" />               <!-- 平面取得では 1。宿題 A -->

  <!-- ここが Z/T の主。zFastEnable=0 なので Z=1、T=frames=3000 -->
  <Streaming enable="1" frames="3000" dmaFrames="1500" triggerMode="1"
             zFastEnable="0" zFastMode="1"    <!-- Mode は方式。Enable と別物 -->
             flybackFrames="1"                <!-- 宿題 A: fast-Z で意味を持つ? -->
             previewIndex="1" />

  <!-- remote focus は 2026-08 時点で未導入。この値は「無効」ではなく
       「搭載していない」状態の既定値なので、挙動の根拠にならない。宿題 D -->
  <RemoteFocus steps="1" startPlane="0" stepSize="1" IsRemoteFocus="0" />

  <CaptureMode mode="1" />                 <!-- 値の意味は未確認 -->
  <ExperimentStatus value="Complete" />    <!-- 宿題 C -->
</ThorImageExperiment>
```

この XML で `xml_parser.ExperimentXMLParser` が返す値:

| 項目 | 値 | 出どころ |
|---|---|---|
| `SizeX` / `SizeY` | 512 / 512 | `LSM/@pixelX` `@pixelY` |
| `SizeZ` | **1** | `Streaming/@zFastEnable="0"` (`ZStage/@steps=61` は使わない) |
| `SizeT` | **3000** | `Streaming/@frames` (`Timelapse/@timepoints` ではない) |
| `Channels` | `["ChanA", "ChanB"]` | `ChannelEnable/@Set=3` |
| `PixelSizeX` / `Y` | 0.17 / 0.17 | `LSM/@pixelWidthUM` `@pixelHeightUM` |
| `PixelSizeZ` | 0.5 | `ZStage/@stepSizeUM` |
| `Objective` | `25xOLY` | `Magnification/@name` |
| `TimeIntervalSec` | 60.0 | `Timelapse/@intervalSec` — **時間軸としては信用しない** |
