"""出所の記録 (_provenance.jsonl) —— どの版のコードで作ったかを残す。

**ここは記録だけで、作り直すべきかの判断はしない。** その線引き自体を試験で
固めておく (判断を足したくなったとき、ここが「足さない」と言っている)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ylabcommon import provenance as P


def test_record_writes_commit_and_is_readable(tmp_path: Path) -> None:
    out = P.record(tmp_path, "sorter", config={"a": 1}, package="ylabcommon")
    assert out is not None and out.name == P.PROVENANCE_FILE

    rec = P.latest(tmp_path, "sorter")
    assert rec is not None and rec.stage == "sorter"
    assert rec.schema_version == P.SCHEMA_VERSION
    commit = rec.source.commit
    assert commit is None or (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit))
    assert rec.deps.python
    assert rec.deps.package.name == "ylabcommon"


def test_record_appends_so_the_history_survives(tmp_path: Path) -> None:
    """作り直しても上書きしない —— いつどの版で作り直したかが残る。"""
    P.record(tmp_path, "sorter", config={"a": 1})
    P.record(tmp_path, "sorter", config={"a": 2})
    P.record(tmp_path, "aggregation", config={"b": 1})

    assert [r.stage for r in P.read(tmp_path)] == ["sorter", "sorter", "aggregation"]
    assert P.latest(tmp_path, "sorter").config == {"a": 2}   # 後勝ち
    assert P.latest(tmp_path).stage == "aggregation"


def test_adding_a_field_does_not_disturb_existing_records(tmp_path: Path) -> None:
    """**記録は誰も等値比較しない**ので、項目を足しても既存の記録に影響しない。

    behavior の analysis_meta.yaml は記録を比較していたため項目を足せなかった
    (足すと下流が一斉に STALE になる)。ここはその結合を持たないことを固定する。
    """
    P.record(tmp_path, "sorter", config={"a": 1})
    P.record(tmp_path, "sorter", config={"a": 1}, extra={"new_field": "x"})

    first, second = P.read(tmp_path)
    assert first.extra is None             # 古い記録はそのまま
    assert second.extra == {"new_field": "x"}
    # 同じ設定なら hash は変わらない (項目を足しても)
    assert first.source.config_hash == second.source.config_hash


def test_config_hash_is_stable_and_discriminating() -> None:
    assert P.config_hash({"a": 1, "b": 2}) == P.config_hash({"b": 2, "a": 1})
    assert P.config_hash({"a": 1}) != P.config_hash({"a": 2})
    assert P.config_hash(None) is None


def test_config_hash_can_look_at_a_subset_of_fields() -> None:
    """無関係な設定の変更でハッシュが動かないように、見る項目を絞れる。"""
    base = {"keep": 1, "ignore": "before"}
    other = {"keep": 1, "ignore": "after"}
    assert P.config_hash(base, fields=["keep"]) == P.config_hash(other, fields=["keep"])
    assert P.config_hash(base) != P.config_hash(other)


def test_config_hash_takes_a_pydantic_model() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Cfg(pydantic.BaseModel):
        a: int = 1
        b: str = "x"

    assert P.config_hash(Cfg()) == P.config_hash({"a": 1, "b": "x"})
    assert P.config_hash(Cfg(), fields=["a"]) == P.config_hash({"a": 1})


def test_config_hash_survives_values_json_cannot_take() -> None:
    assert P.config_hash({"dir": Path("/mnt/x")}) is not None


def test_a_broken_line_does_not_hide_the_good_ones(tmp_path: Path) -> None:
    P.record(tmp_path, "sorter")
    with (tmp_path / P.PROVENANCE_FILE).open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    P.record(tmp_path, "aggregation")

    assert [r.stage for r in P.read(tmp_path)] == ["sorter", "aggregation"]


def test_reading_a_folder_without_a_record_is_empty_not_an_error(tmp_path: Path) -> None:
    assert P.read(tmp_path) == []
    assert P.latest(tmp_path) is None
    assert P.describe(None) == "(no provenance)"


def test_record_does_not_raise_when_it_cannot_write(tmp_path: Path) -> None:
    """**解析を止めない。** 書けない置き場でも例外を投げず None を返す。"""
    blocked = tmp_path / "a-file"
    blocked.write_text("", encoding="utf-8")
    assert P.record(blocked / "under-a-file", "sorter") is None


def test_scan_collects_the_latest_per_folder_and_stage(tmp_path: Path) -> None:
    P.record(tmp_path / "cond_A" / "s1", "sorter")
    P.record(tmp_path / "cond_A" / "s1", "sorter")
    P.record(tmp_path / "cond_B" / "s2", "aggregation")

    found = P.scan(tmp_path)
    assert sorted((f.dir, f.record.stage) for f in found) == [
        ("cond_A/s1", "sorter"), ("cond_B/s2", "aggregation")]
    assert [f.dir for f in P.scan(tmp_path, stage="sorter")] == ["cond_A/s1"]


def test_record_is_valid_jsonl(tmp_path: Path) -> None:
    """1 行 = 1 JSON。jq など外の道具から読める形であること。"""
    P.record(tmp_path, "sorter", config={"x": 1})
    lines = (tmp_path / P.PROVENANCE_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["stage"] == "sorter"


def test_the_module_does_not_decide_whether_to_redo_anything() -> None:
    """**判断を持たない**ことを固定する (2026-09-23 の線引き)。

    「STALE か」「上書きするか」を返す関数をここに足さない。作り直す対象を決めるのは
    記録を読む別のプロセスの仕事で、機械が黙って上書きしてよいことにはしない。
    """
    forbidden = [n for n in dir(P)
                 if any(k in n.lower() for k in ("stale", "overwrite", "should_", "needs_"))]
    assert forbidden == [], "判断する関数が増えている: %s" % forbidden


def test_cli_lists_and_groups(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    P.record(tmp_path / "s1", "sorter")
    P.record(tmp_path / "s2", "sorter")

    assert P.cli([str(tmp_path)]) == 0
    assert "sorter" in capsys.readouterr().out

    assert P.cli([str(tmp_path), "--by-commit"]) == 0
    out = capsys.readouterr().out
    assert "count" in out and "sorter" in out

    assert P.cli([str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)


# -------------------------------------------------------------------------
#  版が行き違っても読めること。**記録は追記だけなので、ここが要点**
# -------------------------------------------------------------------------

def test_a_record_from_a_newer_version_keeps_its_unknown_fields(tmp_path: Path) -> None:
    """知らない項目が来ても**捨てない**。

    捨てると、新しい版が書いたレコードを古い版で読んで書き戻しただけで情報が減る。
    項目を自由に足せることがこの設計の要点なので、読む側が落としてはいけない。
    """
    line = json.dumps({
        "schema_version": 1, "stage": "sorter", "created_at": "2026-09-23T10:00:00+09:00",
        "source": {"commit": "a" * 40, "dirty": False, "brand_new_source_field": 1},
        "deps": {"python": "3.12.9", "brand_new_dep": "x"},
        "some_future_top_level_field": {"k": "v"},
    }, ensure_ascii=False)
    (tmp_path / P.PROVENANCE_FILE).write_text(line + "\n", encoding="utf-8")

    rec = P.latest(tmp_path)
    assert rec is not None and rec.stage == "sorter"
    assert rec.source.commit == "a" * 40
    # 知らない項目が残っていること
    dumped = rec.model_dump(mode="json")
    assert dumped["some_future_top_level_field"] == {"k": "v"}
    assert dumped["source"]["brand_new_source_field"] == 1
    assert dumped["deps"]["brand_new_dep"] == "x"


def test_a_record_from_an_older_version_still_reads(tmp_path: Path) -> None:
    """項目が少ない古いレコードも、既定値で読める。"""
    line = json.dumps({"stage": "sorter"}, ensure_ascii=False)
    (tmp_path / P.PROVENANCE_FILE).write_text(line + "\n", encoding="utf-8")

    rec = P.latest(tmp_path)
    assert rec is not None
    assert rec.stage == "sorter"
    assert rec.source.commit is None      # 無い項目は None
    assert rec.deps.python is None
    assert rec.schema_version == P.SCHEMA_VERSION


def test_the_record_model_is_typed(tmp_path: Path) -> None:
    """属性でたどれること (dict の綴り違いで静かに None にならない)。"""
    P.record(tmp_path, "sorter", config={"a": 1}, package="ylabcommon")
    rec = P.latest(tmp_path)
    assert isinstance(rec, P.ProvenanceRecord)
    assert isinstance(rec.source, P.SourceRef)
    assert isinstance(rec.deps, P.Deps)
    assert isinstance(rec.deps.package, P.PackageRef)
    assert isinstance(rec.deps.ylabcommon, P.YlabCommonRef)
    assert rec.short().startswith("sorter ")
