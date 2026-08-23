"""図の manifest(JSON Lines)とその図IDの規約。

`docs/reporting-spec.md` の「Directory layout」「Figure ID」「Manifest」節の実装。
1図 = 1レコードで、レコードは **prj_dir からの相対パス**だけを持つ(マシン固有の
ルートを混ぜると manifest が可搬でなくなる)。

    prj_dir/
      figures/{figure_id}.svg
      figures/{figure_id}.png
      report/manifest.jsonl
"""
from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

FIGURES_DIRNAME = "figures"
REPORT_DIRNAME = "report"
MANIFEST_NAME = "manifest.jsonl"

#: 図IDの1フィールド。小文字英数、区切りは `-`(先頭/末尾には置けない)。
_FIELD = r"[a-z0-9]+(?:-[a-z0-9]+)*"
#: 図ID全体 `{prj}_{group}_{kind}[_{seq}]`。フィールド区切りは `_` で3〜4個。
_ID_RE = re.compile(rf"^{_FIELD}(?:_{_FIELD}){{2,3}}$")


def figures_dir(prj_dir: Path | str) -> Path:
    return Path(prj_dir) / FIGURES_DIRNAME


def report_dir(prj_dir: Path | str) -> Path:
    return Path(prj_dir) / REPORT_DIRNAME


def manifest_path(prj_dir: Path | str) -> Path:
    return report_dir(prj_dir) / MANIFEST_NAME


def slug(text: Any) -> str:
    """図IDの1フィールドとして使える形へ寄せる(小文字英数と `-` のみ)。

    英数以外は全て `-` にまとめ、前後の `-` は落とす。日本語などの非ASCIIは
    そのままではファイル名として扱いにくいので落ちる。**空になる入力は
    ValueError** — 黙って空フィールドの図IDを作ると、あとで別の図と衝突する。
    """
    out = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    if not out:
        raise ValueError(
            f"cannot build a figure-id field from {text!r}: no ascii alphanumeric "
            "character remains. Pass an ascii name (Japanese labels belong in the caption)."
        )
    return out


#: content_token がハッシュへ切り替える長さ。図IDがファイル名として扱いにくくならない
#: 範囲で、読める限りは元の文字列を残したい。
CONTENT_TOKEN_MAX_LEN = 48
#: ハッシュに落としたときの桁数(sha1 の先頭)。衝突確率は実用上無視できる。
CONTENT_TOKEN_HASH_LEN = 10


def content_token(*parts: Any, max_len: int = CONTENT_TOKEN_MAX_LEN) -> str:
    """図の**内容**から決まる安定したトークンを作る(図IDの seq 用)。

    ページの列挙順で seq を振ると、図が1枚増減しただけで以降の全図のIDがずれ、
    せっかくのアドレスが参照として使えなくなる。代わりに「その図が何を表しているか」
    (集計キー、プロット名など)から作る。**同じ内容なら再実行しても同じ**。

    長くなりすぎる場合だけ sha1 の先頭 CONTENT_TOKEN_HASH_LEN 桁へ落とす。ハッシュも
    入力が同じなら同じなので、安定性は保たれる(読みやすさだけを失う)。
    """
    raw = "-".join(str(p) for p in parts if p is not None and str(p) != "")
    token = slug(raw)
    if len(token) <= max_len:
        return token
    import hashlib

    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:CONTENT_TOKEN_HASH_LEN]
    # 先頭の読める部分を残しつつ、全体長を max_len に収める。
    head = token[: max_len - CONTENT_TOKEN_HASH_LEN - 1].rstrip("-")
    return f"{head}-{digest}"


def figure_id(prj: Any, group: Any, kind: Any, seq: Any = None) -> str:
    """`{prj}_{group}_{kind}[_{seq}]` を組み立てる。各フィールドは slug 化する。

    seq に int を渡すと2桁ゼロ詰め(`3` -> `03`)。ページ番号などを入れる想定。
    """
    parts = [slug(prj), slug(group), slug(kind)]
    if seq is not None:
        parts.append(slug(f"{seq:02d}" if isinstance(seq, int) else seq))
    return "_".join(parts)


def validate_figure_id(fid: str) -> str:
    """図IDが規約どおりか確かめて返す。違反なら ValueError。

    ここで弾かないと、ファイル名としては通るが report から参照しにくいIDや、
    フィールド数が違って prj/group/kind を復元できないIDが混ざる。
    """
    if not isinstance(fid, str) or not _ID_RE.match(fid):
        raise ValueError(
            f"invalid figure id {fid!r}. Expected '{{prj}}_{{group}}_{{kind}}[_{{seq}}]' "
            "with lowercase [a-z0-9] fields, '-' inside a field and '_' between fields "
            "(e.g. 'prj1-2-3_conda_psth', 'prj1-2-3_all_event-raster_p02'). "
            "Use figure_id() to build one."
        )
    return fid


def split_figure_id(fid: str) -> tuple[str, str, str, str | None]:
    """図IDを (prj, group, kind, seq) へ分解する。"""
    parts = validate_figure_id(fid).split("_")
    prj, group, kind = parts[0], parts[1], parts[2]
    return prj, group, kind, (parts[3] if len(parts) == 4 else None)


@dataclass(frozen=True)
class StatRecord:
    """図に紐づく検定結果1件。

    **描画されたかどうかに関わらず記録する**(spec の "Every computed statistic is
    recorded")。今は p 値がラベルのピクセルとしてしか存在せず、あとから照会できない。

    p は「検定を試みたが値が出なかった」ことを表せるよう None を許し、その場合も
    キー自体は残す(理由は params に入れる: 例 `{"skipped": "n<2"}`)。
    statistic は検定統計量(U など)。params は片側/両側などの検定条件。
    """

    name: str
    test: str
    p: float | None = None
    n: Sequence[int] | None = None
    statistic: float | None = None
    params: dict | None = None

    def to_dict(self) -> dict:
        # p は「計算したが値なし」を表現できるよう None でも残す。他は省く。
        out: dict = {"name": self.name, "test": self.test, "p": self.p}
        if self.n is not None:
            out["n"] = list(self.n)
        if self.statistic is not None:
            out["statistic"] = self.statistic
        if self.params:
            out["params"] = dict(self.params)
        return out

    @classmethod
    def coerce(cls, value: "StatRecord | dict") -> "StatRecord":
        """dict でも StatRecord でも受ける(呼び出し側は dict を組み立てがち)。"""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            known = {"name", "test", "p", "n", "statistic", "params"}
            unknown = set(value) - known
            if unknown:
                raise ValueError(
                    f"unknown key(s) {sorted(unknown)} in a stat record. "
                    f"Known keys: {sorted(known)}; put anything else in 'params'."
                )
            return cls(**value)
        raise TypeError(f"stat record must be a StatRecord or dict, got {type(value)!r}")


@dataclass(frozen=True)
class FigureRecord:
    """manifest の1行。`docs/reporting-spec.md` の Manifest 節と同じ形。"""

    id: str
    prj: str
    group: str
    kind: str
    #: どの run(= どのレポート)が書いたレコードか。再実行時に自分の分だけを
    #: 差し替えるための鍵で、spec の "Manifest lifecycle" の
    #: 「rewrite per run, scoped to the PDF being regenerated」を実体にしたもの。
    scope: str | None = None
    caption: str | None = None
    files: dict = field(default_factory=dict)
    pdf: dict | None = None
    stats: tuple = ()
    source: dict | None = None
    data: tuple = ()
    created_at: str | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "id": self.id, "prj": self.prj, "group": self.group, "kind": self.kind,
        }
        if self.scope is not None:
            out["scope"] = self.scope
        if self.caption is not None:
            out["caption"] = self.caption
        out["files"] = dict(self.files)
        if self.pdf is not None:
            out["pdf"] = dict(self.pdf)
        out["stats"] = [StatRecord.coerce(s).to_dict() for s in self.stats]
        if self.source is not None:
            out["source"] = dict(self.source)
        if self.data:
            out["data"] = list(self.data)
        if self.created_at is not None:
            out["created_at"] = self.created_at
        return out


def read_manifest(prj_dir: Path | str) -> list[dict]:
    """manifest を読み、レコードの list を返す。無ければ空。

    壊れた行は**捨てて警告する**(1行の破損で report 生成全体を落とさない)。
    manifest は追記で育つので、途中で切れた行が残ることがありうる。
    """
    path = manifest_path(prj_dir)
    if not path.exists():
        return []
    records: list[dict] = []
    broken = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        if isinstance(obj, dict) and obj.get("id"):
            records.append(obj)
        else:
            broken += 1
    if broken:
        warnings.warn(
            f"{broken} malformed line(s) in {path} were skipped.", UserWarning,
        )
    return records


def write_manifest(prj_dir: Path | str, records: Iterable[dict]) -> Path:
    """manifest を丸ごと書き直す(同じディレクトリの一時ファイル経由で置き換える)。"""
    path = manifest_path(prj_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n" for r in records
    )
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return path


def append_record(prj_dir: Path | str, record: dict) -> Path:
    """1レコードを追記する。

    run の途中で落ちても、そこまでに描けた図は manifest に残る(close 時に
    まとめて書くと、落ちたときに全部消える)。
    """
    path = manifest_path(prj_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")
    return path
