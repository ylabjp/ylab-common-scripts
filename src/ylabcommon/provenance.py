"""解析の出力に「どのコードで作ったか」を残す (記録だけ。判断はしない)。

なぜ要るか
----------
解析は結果を上書きしていく。slice-analysis の sorter は ``volume.ome.tif`` を、
behavior-analysis の前処理は日フォルダの出力を作り直す。**出来上がったファイルを
見ても、どの版のコードが作ったのかは分からない。** 手法は実際に何度も変わるので
(位置合わせの修正、既定アルゴリズムの入れ替え)、版が違えば同じ生データから違う
数値が出る。後から辿れないと、**作り直すべきものとそのままでよいものの区別が
付かない。**

**ここは「記録」だけを持つ。「作り直すべきか」は判断しない。**
----------------------------------------------------------------
これは意図的な線引きである (2026-09-23 の判断)。

behavior-analysis は記録と判断を 1 つにしていた: ``analysis_meta.yaml`` に
``schema_version`` / ``config_hash`` / 上流のスナップショットを書き、それを
**等値比較**して「STALE なら自動で上書きする」を決めていた。これで 2 つ困った。

1. **記録に項目を足せない。** 上流スナップショットは記録をまるごと写すので、
   項目を 1 つ足すだけで既存の下流が一斉に STALE になり、大規模な再計算が走る
   (実際に 3,346 件の ``analysis_meta.yaml`` が対象になる状態だった)。
   記録を育てたいのに、比較に使っているせいで育てられない
2. **機械が上書きを決めてしまう。** 中身を見ていない結果を勝手に作り直す。
   どれがだめかを決められるのは中身を見た人だけである
   (slice-analysis の ``redo_drift3d`` が「人がリネームしたものだけやり直す」
   形にしているのと同じ考え)

だから記録は **追記するだけ・誰も等値比較しない** ものにする。そうすると項目は
自由に足せるし、場合分け (「この項目は比較から外す」) も要らない。**作り直す対象を
決めるのは、記録を読む別のプロセス** (一覧・集計して人が決める) の仕事。

読み書きは pydantic で型を付ける
--------------------------------
**ただし「厳しく検証する」ためではない。** 記録は追記だけで、**古い版が書いた
レコードも、新しい版が書いたレコードも読めなければ困る**。そこで:

- すべての項目に既定値を持たせる —— 項目が増える前に書かれた古いレコードも読める
- :class:`ProvenanceRecord` は ``extra="allow"`` —— **知らない項目が来ても捨てずに
  保持する**。新しい版が足した項目を、古い版で読んで書き戻しても消えない
- 読めない行は飛ばす (:func:`read`)

つまり型は「決まった形を強制する」ためではなく、**項目名と意味を 1 か所に書いて
おくため**と、**前後の版と行き違っても壊れないため**に使う。

記録する形
----------
出力フォルダに ``_provenance.jsonl``。**追記のみ**なので、作り直した履歴が
そのまま残る。1 行 1 レコード (:class:`ProvenanceRecord` の JSON)。

**取れなくても解析は止めない。** git が無い・パッケージ情報が読めない環境でも
``None`` を入れて先へ進む。キーごと消さないのは「調べていない」のか「調べて
分からなかった」のかを区別するため (``ylabcommon.reporting.source`` と同じ方針)。

``dirty=True`` は **未コミットの変更で走らせた**印で、commit だけでは再現できない。
"""
from __future__ import annotations

import getpass
import hashlib
import json
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

#: 追記先。``_`` 始まりなので、構造チェックや glob の対象から外れる。
PROVENANCE_FILE = "_provenance.jsonl"

#: レコードの形の版。読む側が形の違いを判別できるようにする。**項目を足しただけ
#: では上げない** (足しても古い読み手が壊れないのがこの形の要点)。意味が変わった
#: ときだけ上げる。
SCHEMA_VERSION = 1

#: ハッシュの桁数 (sha256 の先頭)。
HASH_DIGITS = 16

#: git は 1 プロセスに 1 回だけ読む。``git status`` は大きな作業ツリーだと数秒
#: かかるので、何十件も回すバッチで毎回呼ばない。
_source_cache: dict[str, Any] | None = None
_deps_cache: "Deps | None" = None
_deps_cache_key: str | None = None


class _Tolerant(BaseModel):
    """前後の版と行き違っても壊れない土台。

    ``extra="allow"`` は「雑に受ける」ためではない。記録は追記だけなので、
    **新しい版が足した項目を古い版で読んでも捨てない**ことが要る。捨てると、
    読んで書き戻しただけで情報が減る。
    """

    model_config = ConfigDict(extra="allow")


class SourceRef(_Tolerant):
    """作ったコードの出所。``reporting.SourceInfo`` と同じ意味・同じ取り方。"""

    #: リポジトリ名 (origin の URL から)。
    repo: str | None = None
    #: **短縮せず 40 桁**。短縮 sha は将来衝突しうるし復元もできない。
    commit: str | None = None
    #: 未コミットの変更があったか。**True なら commit だけでは再現できない。**
    dirty: bool | None = None
    #: 走らせたスクリプト。
    script: str | None = None
    #: 設定のハッシュ。**これ自体は判断をしない** —— 違うと分かるだけ。
    config_hash: str | None = None


class PackageRef(_Tolerant):
    """呼び出し側パッケージの名前と版。"""

    name: str | None = None
    version: str | None = None


class YlabCommonRef(_Tolerant):
    """ylabcommon の版と git commit。

    git から入れた依存の commit は、uv が dist-info の ``direct_url.json`` に書く。
    ここが唯一の手掛かり (パッケージは ``__version__`` を持たない)。
    """

    version: str | None = None
    commit: str | None = None


class Deps(_Tolerant):
    """走らせた環境。"""

    package: PackageRef = Field(default_factory=PackageRef)
    ylabcommon: YlabCommonRef = Field(default_factory=YlabCommonRef)
    python: str | None = None


class ProvenanceRecord(_Tolerant):
    """1 回の実行の記録。**比較には使わない。**"""

    #: 形の版 (:data:`SCHEMA_VERSION`)。
    schema_version: int = SCHEMA_VERSION
    #: 工程の名前 (``sorter`` / ``preprocess_video`` など)。
    stage: str = ""
    #: ISO8601 (タイムゾーン付き)。
    created_at: str = ""
    source: SourceRef = Field(default_factory=SourceRef)
    deps: Deps = Field(default_factory=Deps)
    #: 結果を決めた設定。そのまま残す。
    config: Any = None
    #: どの入力から作ったか。**記録だけ**で、比較して何かを決めることはしない。
    inputs: Any = None
    #: 追加で残したいもの。
    extra: dict[str, Any] | None = None
    host: str | None = None
    user: str | None = None

    def short(self) -> str:
        """1 行にする (一覧表示用)。"""
        commit = (self.source.commit or "unknown")[:12]
        dirty = self.source.dirty
        mark = "+dirty" if dirty else ("" if dirty is False else "+unknown")
        return "%s %s%s %s" % (self.stage or "?", commit, mark, (self.created_at or "")[:19])


def config_hash(config: Any, fields: Iterable[str] | None = None) -> str | None:
    """設定の安定ハッシュ。順序に依存しない正規 JSON の sha256。

    同じ設定で走らせたかを 1 つの文字列で突き合わせるためのもの。**これ自体は
    判断をしない** —— 値が違うことが分かるだけで、作り直すかどうかは読む側が決める。

    Args:
        config: pydantic のモデル (``model_dump`` を持つもの) か、ただの dict。
        fields: 見る top-level 項目を絞る。無関係な設定の変更でハッシュが変わるのを
            避けたいときに使う (behavior の ``compute_config_hash`` と同じ用途)。
    """
    if config is None:
        return None
    data = config
    dump = getattr(config, "model_dump", None)
    if callable(dump):
        data = dump(mode="json", include=set(fields)) if fields else dump(mode="json")
    elif fields is not None and isinstance(config, dict):
        data = {k: v for k, v in config.items() if k in set(fields)}
    try:
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HASH_DIGITS]


def _package_ref(package: str | None) -> PackageRef:
    ref = PackageRef(name=package)
    if not package:
        return ref
    try:
        from importlib.metadata import version as _v

        ref.version = _v(package)
    except Exception:
        pass
    return ref


def _ylabcommon_ref() -> YlabCommonRef:
    ref = YlabCommonRef()
    try:
        from importlib.metadata import distribution

        dist = distribution("ylabcommon")
        ref.version = dist.version
        raw = dist.read_text("direct_url.json")
        if raw:
            ref.commit = (json.loads(raw).get("vcs_info") or {}).get("commit_id")
    except Exception:
        pass
    return ref


def _deps(package: str | None) -> Deps:
    """実行環境。1 回読んで覚える。"""
    global _deps_cache, _deps_cache_key
    key = package or ""
    if _deps_cache is not None and _deps_cache_key == key:
        return _deps_cache.model_copy(deep=True)
    deps = Deps(package=_package_ref(package), ylabcommon=_ylabcommon_ref(),
                python=platform.python_version())
    _deps_cache, _deps_cache_key = deps, key
    return deps.model_copy(deep=True)


def _source(base: Path, script: str | Path | None) -> SourceRef:
    """リポジトリの commit / 未コミット変更の有無。1 回読んで覚える。

    ``reporting.SourceInfo`` をそのまま使う —— 図の manifest が既に同じ形で
    記録しているので、**図と工程で別々の取り方をしない**。
    """
    global _source_cache
    if _source_cache is None:
        try:
            from ylabcommon.reporting import SourceInfo

            info = SourceInfo.capture(path=base).to_dict()
            info.pop("params_hash", None)     # ここでは config_hash として持つ
            _source_cache = info
        except Exception:
            _source_cache = {"repo": None, "commit": None, "dirty": None, "script": None}
    ref = SourceRef(**_source_cache)
    if script is not None:
        ref.script = str(script)
    return ref


def capture(stage: str, *, config: Any = None, config_fields: Iterable[str] | None = None,
            config_hash_value: str | None = None,
            inputs: Any = None, package: str | None = None,
            script: str | Path | None = None, base: str | Path | None = None,
            extra: dict[str, Any] | None = None) -> ProvenanceRecord:
    """1 レコードぶんを組み立てる (ファイルには書かない)。

    Args:
        stage: 工程の名前。
        config: 結果を決める設定。そのまま記録し、ハッシュも取る。
        config_fields: ハッシュに入れる top-level 項目を絞る。
        config_hash_value: **既に計算済みのハッシュ**をそのまま入れる。呼び出し側が
            別の式で出したハッシュを既に他所に書いている場合に使う (behavior-analysis
            の ``analysis_meta.yaml`` がそれで、3,346 件の既存記録と値を揃える必要が
            ある)。渡したときは ``config`` からは計算し直さない。
        inputs: どの入力から作ったか。**記録だけ**で、比較はしない。
        package: 呼び出し側のパッケージ名 (版を記録するため)。
        script: 記録するスクリプト。
        base: どのリポジトリを見るか。省略時は ``script`` の場所、それも無ければ cwd。
        extra: 追加で残したいもの。
    """
    caller_base = Path(base) if base is not None else (
        Path(script).parent if script is not None else Path.cwd())
    source = _source(caller_base, script)
    source.config_hash = (config_hash_value if config_hash_value is not None
                          else config_hash(config, config_fields))
    record_model = ProvenanceRecord(
        schema_version=SCHEMA_VERSION,
        stage=stage,
        created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        source=source,
        deps=_deps(package),
        config=_jsonable(config),
        inputs=_jsonable(inputs),
        extra=extra,
    )
    try:
        record_model.host = socket.gethostname()
    except Exception:
        pass
    try:
        record_model.user = getpass.getuser()
    except Exception:
        pass
    return record_model


def _jsonable(value: Any) -> Any:
    """pydantic なら dump、それ以外はそのまま (書き出しは default=str が拾う)。"""
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:
            return None
    return value


def record(out_dir: str | Path, stage: str, **kwargs: Any) -> Path | None:
    """``out_dir/_provenance.jsonl`` に 1 行追記する。

    **解析を止めない。** 置き場が読み取り専用・ネットワークが切れた等で書けない
    ことがあるが、出所が残らないだけで解析結果そのものは正しい。

    Returns:
        書いたファイルのパス。書けなければ None。
    """
    try:
        path = Path(out_dir) / PROVENANCE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = capture(stage, **kwargs).model_dump(mode="json")
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return path
    except Exception as e:  # pragma: no cover - 置き場の都合でしか通らない
        print("WARNING: could not record provenance in %s (%s)" % (out_dir, e))
        return None


def read(out_dir: str | Path) -> list[ProvenanceRecord]:
    """``_provenance.jsonl`` を読む。無ければ空。**読めない行は飛ばす。**

    古い版が書いたレコード (項目が少ない) も、新しい版が書いたレコード (知らない
    項目がある) も読める。前者は既定値で、後者は ``extra="allow"`` で保持される。
    """
    path = Path(out_dir) / PROVENANCE_FILE
    if not path.exists():
        return []
    records: list[ProvenanceRecord] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(ProvenanceRecord.model_validate_json(line))
        except Exception:
            continue
    return records


def latest(out_dir: str | Path, stage: str | None = None) -> ProvenanceRecord | None:
    """いちばん新しいレコード。``stage`` を渡せばその工程のうちで。"""
    records = [r for r in read(out_dir) if stage is None or r.stage == stage]
    return records[-1] if records else None


class ScanHit(_Tolerant):
    """:func:`scan` の 1 件。"""

    #: 歩き始めた場所からの相対パス。
    dir: str
    record: ProvenanceRecord


def scan(root: str | Path, stage: str | None = None) -> list[ScanHit]:
    """``root`` 以下を歩き、フォルダ×工程ごとの最新を集める。"""
    root = Path(root)
    out: list[ScanHit] = []
    for path in sorted(root.rglob(PROVENANCE_FILE)):
        by_stage: dict[str, ProvenanceRecord] = {}
        for rec in read(path.parent):
            if stage is not None and rec.stage != stage:
                continue
            by_stage[rec.stage] = rec      # 後勝ち = 最新
        for rec in by_stage.values():
            try:
                rel = path.parent.relative_to(root).as_posix()
            except ValueError:
                rel = str(path.parent)
            out.append(ScanHit(dir=rel or ".", record=rec))
    return out


def describe(rec: ProvenanceRecord | None) -> str:
    """レコードを 1 行にする。無ければその旨。"""
    return rec.short() if rec is not None else "(no provenance)"


def cli(argv: list[str] | None = None) -> int:
    """``prov`` —— 置き場を歩いて「どの版で作られたか」を一覧・集計する。

    **これは読むだけの道具で、何も作り直さない。** 版ごとにまとめると、作り直す
    対象を人が決めるための一覧になる。
    """
    import argparse
    from collections import Counter

    parser = argparse.ArgumentParser(
        prog="prov",
        description="解析結果がどの版のコードで作られたかを一覧する "
                    "(_provenance.jsonl を読むだけ。何も書き換えない)。")
    parser.add_argument("root", help="歩き始める場所。")
    parser.add_argument("--stage", default=None, help="この工程だけ。")
    parser.add_argument("--by-commit", action="store_true",
                        help="1 件ずつではなく commit ごとの件数でまとめる。")
    parser.add_argument("--json", action="store_true", help="JSON で出す。")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print("Not a folder: %s" % root, file=sys.stderr)
        return 2

    found = scan(root, stage=args.stage)
    if args.json:
        json.dump([h.model_dump(mode="json") for h in found], sys.stdout,
                  ensure_ascii=False, indent=1, default=str)
        print()
        return 0
    if not found:
        print("No %s under %s. Nothing recorded there yet (outputs made before "
              "this feature carry no record)." % (PROVENANCE_FILE, root))
        return 0

    if args.by_commit:
        counter: Counter = Counter()
        for hit in found:
            src = hit.record.source
            counter[((src.commit or "unknown")[:12], bool(src.dirty), hit.record.stage)] += 1
        print("%-14s %-6s %-22s %s" % ("commit", "dirty", "stage", "count"))
        for (commit, dirty, stage_name), n in counter.most_common():
            print("%-14s %-6s %-22s %d" % (commit, "yes" if dirty else "no", stage_name, n))
        return 0

    print("%-60s %s" % ("dir", "stage commit created_at"))
    for hit in found:
        print("%-60s %s" % (hit.dir[:60], hit.record.short()))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
