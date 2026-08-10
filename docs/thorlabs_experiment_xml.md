# ThorImage `Experiment.xml` の読み方 (リバースエンジニアリングによる仮説)

ThorImageLS の `Experiment.xml` に公開仕様は無い。ここに書いてあるのは
**実データと突き合わせて確かめた仮説** であって、Thorlabs が保証したものではない。

そのため各項目に **確度** を付ける。

| 記号 | 意味 |
|---|---|
| 確認済 | 実データで検証した。反例が出るまでは信じてよい |
| 推定 | 属性名と値から読めるが、その条件のデータをまだ見ていない |
| 未解決 | 食い違いが分かっているが、どちらが正しいか決まっていない |

反例が出たらこの表を直すこと。**コードのコメントではなくここが正典** で、
`xml_parser.py` はここを参照する。

対象バージョン: `<Software version="4.4.2026.1231"/>` (ThorImageLS 4.4)

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
| `1` | `1` | `ZStage/@steps` | `frames // steps` | 推定 |
| `0` | — | `ZStage/@steps` (`enable="1"` のとき) | `Timelapse/@timepoints` | 確認済 |

**要点: `Streaming enable="1"` のとき、`ZStage/@steps` は `zFastEnable="1"` の
ときしか使われない。** 連続取得中に低速な Z ステージを動かすことはできないので、
Z を振るには fast-Z (共振/電気式) が要る。`zFastEnable="0"` なら Z は 1 面である。

`ZStage/@enable="1"` が残っていても意味を持たない。上の例がまさにそれで、
Z スタックの設定を組んだあと Streaming に切り替えたため、使われない `steps="61"` が
残っていた。

`zFastMode="1"` は fast-Z の **方式** (ノコギリ波/階段など) で、有効/無効ではない。
`zFastEnable` と混同しないこと。

### `Streaming` が無い場合

古いバージョンや別の取得形式ではノードごと無いことがある。その場合は
`Streaming enable="0"` と同じ扱い (`ZStage` + `Timelapse`) にしている。**推定**。

### 裏取りに使えそうな属性 (未検証)

- `LSM/@NumberOfPlanes` — 上の例では `1` で、実データの Z=1 と一致する。
  fast-Z のときに `61` になるなら、これ 1 つで判定できるかもしれない。
  **fast-Z のデータが手に入ったら確認すること。**
- `RemoteFocus/@steps` — 上の例では `1`。remote focus 方式の fast-Z ではここが
  効く可能性がある。**未検証**。
- `ZStage/@zStreamFrames`, `ZStage/@zStreamMode` — 名前からして Streaming 時の
  Z 制御に関係しそうだが、上の例では `1` / `0` で判別に使えなかった。**未検証**。

---

## 時間軸 — **未解決**

```xml
<Timelapse timepoints="3000" intervalSec="60" .../>
<Streaming enable="1" frames="3000" .../>
<LSM frameRate="45.638" averageMode="0" averageNum="10" .../>
```

`Timelapse/@intervalSec="60"` を素直に読むと 3000 時点 × 60 秒 = **50 時間**。
一方 `frameRate="45.638"` から読むと 3000 フレーム = **約 66 秒**。3 桁違う。

- `Timelapse/@timepoints` と `Streaming/@frames` がどちらも 3000 なのは、
  ThorImage が一方を他方へ写しているだけかもしれない。
- `Streaming/@triggerMode="1"` は外部トリガの可能性があり、その場合の間隔は
  フレームレートでもなく `intervalSec` でもない。
- `averageNum="10"` が効いていれば実効フレーム周期は 10 倍になるが、
  `averageMode="0"` が「平均なし」を意味するのかは未確認。

**現状の実装は `Timelapse/@intervalSec` をそのまま採っている** (従来どおり)。
根拠が足りないまま変えると、ΔF/F の時間軸が静かにずれるため保留している。
Streaming 取得の実時間が分かる資料 (取得ログ、ストップウォッチ、あるいは
`ChanA` の連番と壁時計の対応) が 1 件あれば決められる。

---

## チャンネル — `ChannelEnable/@Set` のビットマスク

```xml
<Wavelengths>
  <Wavelength name="ChanA" exposureTimeMS="0" />
  <Wavelength name="ChanB" exposureTimeMS="0" />
  <ChannelEnable Set="3" />
</Wavelengths>
```

`<Wavelength>` は **設定されている** 波長を並べるだけで、その取得で有効だったかは
`<ChannelEnable Set>` が持つ。`Set` は 1-origin のビットマスクで、`3 = 0b11` は
1 番目と 2 番目が有効。**確認済** (2 チャンネル分の生ファイルがある)。

`<Wavelength>` を数えるだけだと、片方だけ有効にした取得で「XML は 2 波長だが
実データは 1 チャンネル」という食い違いが出る。**推定** (片チャンネル取得の実例は未確認)。

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
| 取得時刻 | `Date/@date` (`MM/DD/YYYY HH:MM:SS`) | 確認済 |

罠:

- `LSM/@width` / `@height` は **存在しない**。以前のアダプタがこれを読んでいたため
  SizeX/SizeY が常に既定値の 512 になっていた (この XML ではたまたま 512 なので
  気付けなかった)。
- `LSM/@pixelSizeUM="2.93"` は µm/px **ではない**。`pixelWidthUM` (0.17) と 17 倍
  違う。用途不明。**使わないこと。**
- `Camera/@pixelSizeUM="0.25"` は多光子取得では無関係 (`Camera/@width="0"`)。
- `widthUM="86.96"` ÷ `pixelX="512"` = 0.1698 ≒ `pixelWidthUM`。整合しているので
  どちらから求めてもよいが、丸めの分だけ `pixelWidthUM` の方が素直。

XML とヘッダが食い違ったときは **ヘッダを採る** (XML は設定、ファイルは結果)。
食い違いは警告に出す。

---

## まだ読んでいない / 用途不明のノード

判断に使っていないが、後で要るかもしれないもの。

- `CaptureMode/@mode="1"` — 取得種別らしいが、値の意味は未確認。`Streaming/@enable`
  と重複している可能性がある
- `ExperimentStatus/@value="Complete"` — 途中で止めた取得では別の値になるはず。
  「最後の時点が欠けている」の裏取りに使えるかもしれない。**未検証**
- `Streaming/@previewIndex="1"` — 生データに連番の付かない余分なファイルが
  混ざることがあり、これが関係している可能性がある。連番の読めないファイルは
  `_fill_frame` が落として報告する
- `Streaming/@flybackFrames="1"` — fast-Z のとき、各ボリュームの末尾に捨てる面が
  あることを示すかもしれない。**fast-Z のデータで要確認** (無視すると Z が 1 面ずれる)
- `Photobleaching`, `SLM`, `Pockels` — 刺激系。取り込みの構造には関与しない
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
- 連番の読めないファイル (preview など) は枠のどの枡も指せないので落とす。
  1 枚混ざっただけで取得全体を諦めてはいけない

`_fill_frame` の 3 段階:

1. XML が枠 (`SizeT` × `SizeZ`) を決める
2. ファイル名の連番が指す枡を埋める
3. 埋まらなかった分をカットする

枠は **目標であって上限ではない**。はみ出したファイルは捨てずに使い、件数だけ
報告する。XML が当てにならないことが分かっている以上、それを上限にすると実在する
面を落とす。
