"""ylabcommon.reporting (Phase 1: FigureStore / manifest / render_report) の検証。

仕様は docs/reporting-spec.md。この層の存在意義は3つで、テストもそこを固定する:

* **参照できる**  — 図1枚ごとに安定したID(`{prj}_{group}_{kind}[_{seq}]`)と SVG/PNG
* **照会できる**  — 検定結果が manifest の数値として残る(**図に描かれなくても**)
* **追跡できる**  — どの repo のどの commit で、未コミット変更があったかが残る

加えて **互換の約束**: PDF の中身は従来の PdfPages 直書きと同じで、per-figure ファイルと
manifest は純粋な追加。呼び出し箇所は1つずつ移行できる。
"""
import json
import subprocess

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pytest

from ylabcommon.reporting import (
    FigureStore,
    SourceInfo,
    StatRecord,
    figure_id,
    manifest_path,
    read_manifest,
    render_report,
    slug,
    split_figure_id,
    validate_figure_id,
)


@pytest.fixture
def fig():
    f, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    yield f
    plt.close(f)


def _records(prj_dir):
    return read_manifest(prj_dir)


# --------------------------------------------------------------------------- #
# 図ID
# --------------------------------------------------------------------------- #
class TestFigureId:
    def test_builds_the_spec_shape(self):
        assert figure_id("prj1-2-3", "conda", "psth") == "prj1-2-3_conda_psth"
        assert figure_id("prj1-2-3", "all", "event-raster", "p02") == \
            "prj1-2-3_all_event-raster_p02"

    def test_int_seq_is_zero_padded(self):
        assert figure_id("p1", "g", "k", 3) == "p1_g_k_03"

    @pytest.mark.parametrize("raw,expected", [
        ("PSTH", "psth"),
        ("event raster", "event-raster"),
        ("cond_A", "cond-a"),          # フィールド内の区切りは '-' に寄せる
        ("  spaced  ", "spaced"),
        ("a//b", "a-b"),
    ])
    def test_slug_normalises(self, raw, expected):
        assert slug(raw) == expected

    def test_slug_rejects_input_with_nothing_usable(self):
        """空フィールドのIDを黙って作ると、後で別の図と衝突する。"""
        with pytest.raises(ValueError, match="alphanumeric"):
            slug("日本語")

    @pytest.mark.parametrize("fid", [
        "prj1-2-3_conda_psth",
        "prj1-2-3_all_event-raster_p02",
        "a_b_c",
    ])
    def test_accepts_valid(self, fid):
        assert validate_figure_id(fid) == fid

    @pytest.mark.parametrize("fid", [
        "only_two",                    # フィールド不足 -> prj/group/kind を復元できない
        "a_b_c_d_e",                   # 多すぎ
        "Prj_g_k",                     # 大文字
        "prj_g_k with space",
        "prj_-g_k",                    # フィールド先頭の '-'
        "prj__g_k",                    # 空フィールド
        "",
    ])
    def test_rejects_invalid(self, fid):
        with pytest.raises(ValueError, match="invalid figure id"):
            validate_figure_id(fid)

    def test_split_round_trips(self):
        assert split_figure_id("prj1-2-3_all_event-raster_p02") == \
            ("prj1-2-3", "all", "event-raster", "p02")
        assert split_figure_id("p_g_k") == ("p", "g", "k", None)


# --------------------------------------------------------------------------- #
# StatRecord — 「描かれなくても記録する」
# --------------------------------------------------------------------------- #
class TestStatRecord:
    def test_keeps_p_even_when_none(self):
        """検定を試みて値が出なかったことを表現できる必要がある。

        キーごと消すと「計算していない」のか「計算して出なかった」のか区別できない。
        """
        d = StatRecord(name="a_vs_b", test="mannwhitneyu", p=None,
                       params={"skipped": "n<2"}).to_dict()
        assert d["p"] is None and d["params"] == {"skipped": "n<2"}

    def test_omits_unset_optional_fields(self):
        d = StatRecord(name="x", test="t").to_dict()
        assert set(d) == {"name", "test", "p"}

    def test_accepts_a_plain_dict(self):
        s = StatRecord.coerce({"name": "x", "test": "t", "p": 0.01, "n": [3, 4]})
        assert s.p == 0.01 and s.n == [3, 4]

    def test_rejects_unknown_keys(self):
        """typo を黙って捨てると、記録したつもりの数値が消える。"""
        with pytest.raises(ValueError, match="unknown key"):
            StatRecord.coerce({"name": "x", "test": "t", "pvalue": 0.01})


# --------------------------------------------------------------------------- #
# FigureStore
# --------------------------------------------------------------------------- #
class TestFigureStore:
    def test_writes_svg_png_pdf_and_manifest(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="psth.pdf") as store:
            store.save(fig, key="p1_conda_psth", caption="cue onset")
        assert (tmp_path / "figures" / "p1_conda_psth.svg").exists()
        assert (tmp_path / "figures" / "p1_conda_psth.png").exists()
        assert (tmp_path / "psth.pdf").exists()
        (rec,) = _records(tmp_path)
        assert rec["id"] == "p1_conda_psth"
        assert rec["caption"] == "cue onset"
        assert rec["files"] == {"svg": "figures/p1_conda_psth.svg",
                                "png": "figures/p1_conda_psth.png"}
        assert rec["pdf"] == {"file": "psth.pdf", "page": 1}

    def test_derives_prj_group_kind_from_the_id(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="prj1-2-3_all_event-raster_p02")
        (rec,) = _records(tmp_path)
        assert (rec["prj"], rec["group"], rec["kind"]) == \
            ("prj1-2-3", "all", "event-raster")

    def test_manifest_paths_are_relative_to_prj_dir(self, tmp_path, fig):
        """マシン固有のルートが混ざると manifest が可搬でなくなる。"""
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k")
        raw = manifest_path(tmp_path).read_text(encoding="utf-8")
        assert str(tmp_path) not in raw

    def test_page_numbers_follow_the_pdf(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k1")
            store.save(fig, key="p_g_k2")
        assert [r["pdf"]["page"] for r in _records(tmp_path)] == [1, 2]

    def test_passthrough_pages_do_not_shift_recorded_page_numbers(self, tmp_path, fig):
        """移行途中は素の PdfPages 直書きと混在する。ページ番号がずれないこと。"""
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.pdf.savefig(fig)              # 未移行の呼び出し(manifest に載らない)
            store.pdf.savefig(fig)
            store.save(fig, key="p_g_k")        # PDF 上は3ページ目
        (rec,) = _records(tmp_path)
        assert rec["pdf"]["page"] == 3

    def test_pdf_property_raises_without_a_pdf(self, tmp_path):
        store = FigureStore(tmp_path, scope="s")
        assert store.has_pdf is False
        with pytest.raises(ValueError, match="without pdf_name"):
            _ = store.pdf

    def test_works_without_a_pdf(self, tmp_path, fig):
        with FigureStore(tmp_path, scope="stats-only") as store:
            store.save(fig, key="p_g_k")
        (rec,) = _records(tmp_path)
        assert rec.get("pdf") is None
        assert (tmp_path / "figures" / "p_g_k.png").exists()

    def test_requires_a_scope(self, tmp_path):
        """scope が無いと再実行のたびにレコードが二重になる。"""
        with pytest.raises(ValueError, match="pdf_name or scope"):
            FigureStore(tmp_path)

    def test_savefig_kwargs_reach_every_output(self, tmp_path, monkeypatch, fig):
        """bbox_inches='tight' などが片方だけに効くと図とPDFが食い違う。"""
        seen = []
        real = type(fig).savefig
        monkeypatch.setattr(type(fig), "savefig",
                            lambda self, target, **kw: (seen.append(kw), real(self, target, **kw))[1])
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k", bbox_inches="tight")
        assert all(kw.get("bbox_inches") == "tight" for kw in seen)
        assert any("dpi" in kw for kw in seen)          # PNG には dpi が乗る

    def test_records_stats_that_were_never_drawn(self, tmp_path, fig):
        """この層の眼目。今は p 値が図のピクセルにしか無い。"""
        stats = [
            {"name": "a_vs_b", "test": "mannwhitneyu", "p": 0.031,
             "n": [12, 11], "params": {"alternative": "two-sided"}},
            {"name": "a_vs_c", "test": "steel", "p": None,
             "params": {"skipped": "n<2"}},
        ]
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k", stats=stats)
        (rec,) = _records(tmp_path)
        assert [s["name"] for s in rec["stats"]] == ["a_vs_b", "a_vs_c"]
        assert rec["stats"][0]["p"] == 0.031
        assert rec["stats"][1]["p"] is None

    def test_source_is_attached_to_every_record(self, tmp_path, fig):
        src = SourceInfo(repo="behavior-analysis", commit="a" * 40, dirty=True,
                         script="pipeline/group_report.py")
        with FigureStore(tmp_path, pdf_name="a.pdf", source=src) as store:
            store.save(fig, key="p_g_k1")
            store.save(fig, key="p_g_k2")
        for rec in _records(tmp_path):
            assert rec["source"]["repo"] == "behavior-analysis"
            assert rec["source"]["dirty"] is True

    def test_data_paths_are_recorded(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k", data=["cond_a/m1", "cond_b/m2"])
        (rec,) = _records(tmp_path)
        assert rec["data"] == ["cond_a/m1", "cond_b/m2"]

    def test_duplicate_id_in_one_run_raises(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k")
            with pytest.raises(ValueError, match="already saved"):
                store.save(fig, key="p_g_k")

    def test_id_taken_by_another_report_raises(self, tmp_path, fig):
        """図IDは prj_dir 内で一意。黙って上書きすると別の図が消える。"""
        with FigureStore(tmp_path, pdf_name="a.pdf") as s1:
            s1.save(fig, key="p_g_k")
        with FigureStore(tmp_path, pdf_name="b.pdf") as s2:
            with pytest.raises(ValueError, match="already used by another report"):
                s2.save(fig, key="p_g_k")

    def test_save_after_close_raises(self, tmp_path, fig):
        store = FigureStore(tmp_path, pdf_name="a.pdf")
        store.close()
        with pytest.raises(ValueError, match="closed"):
            store.save(fig, key="p_g_k")

    def test_close_is_idempotent(self, tmp_path):
        store = FigureStore(tmp_path, pdf_name="a.pdf")
        store.close()
        store.close()


class TestManifestLifecycle:
    """spec の open question「append か rewrite か」に対する実装の約束。

    採ったのは **run 単位の書き換え、ただし scope 内だけ**。同じレポートを作り直すと
    自分のレコードは差し替わり、他のレポートのレコードは残る。
    """

    def test_rerun_replaces_its_own_records(self, tmp_path, fig):
        for _ in range(3):
            with FigureStore(tmp_path, pdf_name="a.pdf") as store:
                store.save(fig, key="p_g_k")
        assert len(_records(tmp_path)) == 1          # 二重にならない

    def test_rerun_keeps_other_reports_records(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as s:
            s.save(fig, key="p_ga_k")
        with FigureStore(tmp_path, pdf_name="b.pdf") as s:
            s.save(fig, key="p_gb_k")
        with FigureStore(tmp_path, pdf_name="a.pdf") as s:
            s.save(fig, key="p_ga_k")
        assert sorted(r["id"] for r in _records(tmp_path)) == ["p_ga_k", "p_gb_k"]

    def test_stale_records_of_the_same_scope_are_dropped(self, tmp_path, fig):
        """前回あった図が今回出なくなったら manifest からも消える。"""
        with FigureStore(tmp_path, pdf_name="a.pdf") as s:
            s.save(fig, key="p_g_k1")
            s.save(fig, key="p_g_k2")
        with FigureStore(tmp_path, pdf_name="a.pdf") as s:
            s.save(fig, key="p_g_k1")
        assert [r["id"] for r in _records(tmp_path)] == ["p_g_k1"]

    def test_records_are_appended_as_they_are_saved(self, tmp_path, fig):
        """run の途中で落ちても、そこまでの図は manifest に残ること。"""
        store = FigureStore(tmp_path, pdf_name="a.pdf")
        store.save(fig, key="p_g_k1")
        assert len(_records(tmp_path)) == 1      # close 前でも読める
        store.close()

    def test_malformed_lines_are_skipped_with_a_warning(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as s:
            s.save(fig, key="p_g_k")
        with manifest_path(tmp_path).open("a", encoding="utf-8") as f:
            f.write("{ this is not json\n")
        with pytest.warns(UserWarning, match="malformed"):
            assert len(_records(tmp_path)) == 1


# --------------------------------------------------------------------------- #
# SourceInfo
# --------------------------------------------------------------------------- #
def _git_repo(path):
    def run(*args):
        subprocess.run(["git", *args], cwd=str(path), check=True,
                       capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    run("add", "a.py")
    run("commit", "-q", "-m", "init")


class TestSourceInfo:
    def test_captures_repo_and_commit(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _git_repo(repo)
        info = SourceInfo.capture(path=repo, script=repo / "a.py")
        assert info.repo == "myrepo"
        assert info.commit is not None and len(info.commit) == 40
        assert info.dirty is False
        assert info.script == "a.py"

    def test_dirty_flag_reflects_uncommitted_changes(self, tmp_path):
        """図がどの状態のコードで作られたかは、commit だけでは足りない。"""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _git_repo(repo)
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        assert SourceInfo.capture(path=repo).dirty is True

    def test_untracked_file_counts_as_dirty(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _git_repo(repo)
        (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
        assert SourceInfo.capture(path=repo).dirty is True

    def test_outside_a_repo_degrades_without_raising(self, tmp_path):
        """git が無い/リポジトリ外でも解析は止めない。分からない項目は None。"""
        info = SourceInfo.capture(path=tmp_path)
        assert info.repo is None and info.commit is None and info.dirty is None

    def test_unknown_fields_stay_as_null_keys(self, tmp_path):
        """キーごと消すと「調べていない」のか「分からなかった」のか区別できない。"""
        d = SourceInfo.capture(path=tmp_path).to_dict()
        assert set(d) == {"repo", "commit", "dirty", "script", "params_hash"}

    def test_params_hash_is_caller_supplied(self, tmp_path):
        """何を入れてどう正規化するかは spec の open question。ここでは決めない。"""
        assert SourceInfo.capture(path=tmp_path, params_hash="abc").params_hash == "abc"

    def test_script_defaults_to_the_calling_file(self, tmp_path):
        info = SourceInfo.capture(path=tmp_path)
        assert info.script == "test_reporting.py"

    def test_store_captures_source_when_not_given(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k")
        (rec,) = _records(tmp_path)
        # 中継している FigureStore ではなく、呼び出し元(このテストファイル)が出ること。
        # ここは実リポジトリ内なので、リポジトリルート相対のパスになる。
        assert rec["source"]["script"] == "tests/test_reporting.py"
        assert rec["source"]["repo"] == "ylab-common-scripts"


# --------------------------------------------------------------------------- #
# render_report
# --------------------------------------------------------------------------- #
class TestRenderReport:
    def _build(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="psth.pdf") as store:
            store.save(fig, key="p1_conda_psth", caption="cond A のPSTH",
                       stats=[{"name": "a_vs_b", "test": "mannwhitneyu",
                               "p": 0.031, "n": [12, 11]}],
                       data=["cond_a/m1"])
            store.save(fig, key="p1_condb_psth", caption="cond B のPSTH")
            store.save(fig, key="p1_conda_bar")
        return render_report(tmp_path)

    def test_writes_report_md(self, tmp_path, fig):
        path = self._build(tmp_path, fig)
        assert path == tmp_path / "report" / "report.md"
        assert path.exists()

    def test_one_section_per_group_in_manifest_order(self, tmp_path, fig):
        text = self._build(tmp_path, fig).read_text(encoding="utf-8")
        assert text.index("## conda") < text.index("## condb")
        assert text.count("## conda") == 1          # 同じ group は1節にまとまる

    def test_png_links_are_relative_and_resolve(self, tmp_path, fig):
        path = self._build(tmp_path, fig)
        text = path.read_text(encoding="utf-8")
        assert "![p1_conda_psth](../figures/p1_conda_psth.png)" in text
        assert (path.parent / "../figures/p1_conda_psth.png").resolve().exists()

    def test_stats_appear_as_numbers(self, tmp_path, fig):
        """図のピクセルではなく、読み取れる表として出ること。"""
        text = self._build(tmp_path, fig).read_text(encoding="utf-8")
        assert "| a_vs_b | mannwhitneyu | 12, 11 |" in text
        assert "0.031" in text

    def test_captions_are_kept_verbatim(self, tmp_path, fig):
        text = self._build(tmp_path, fig).read_text(encoding="utf-8")
        assert "cond A のPSTH" in text              # 日本語も落とさない

    def test_pdf_page_is_shown(self, tmp_path, fig):
        text = self._build(tmp_path, fig).read_text(encoding="utf-8")
        assert "`psth.pdf` p.1" in text

    def test_source_line_shortens_the_sha(self, tmp_path, fig):
        src = SourceInfo(repo="r", commit="0123456789abcdef" * 2 + "01234567",
                         dirty=True, script="s.py")
        with FigureStore(tmp_path, pdf_name="a.pdf", source=src) as store:
            store.save(fig, key="p_g_k")
        text = render_report(tmp_path).read_text(encoding="utf-8")
        assert "r@01234567 (dirty) — s.py" in text

    def test_null_p_is_shown_explicitly(self, tmp_path, fig):
        """空欄だと「検定していない」と読めてしまう。"""
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k",
                       stats=[{"name": "x", "test": "steel", "p": None}])
        text = render_report(tmp_path).read_text(encoding="utf-8")
        assert "| n/a |" in text

    def test_empty_manifest_still_writes_a_file(self, tmp_path):
        text = render_report(tmp_path).read_text(encoding="utf-8")
        assert "no figures recorded" in text

    def test_is_regenerable(self, tmp_path, fig):
        """manifest が正本。何度作り直しても同じ。"""
        first = self._build(tmp_path, fig).read_text(encoding="utf-8")
        assert render_report(tmp_path).read_text(encoding="utf-8") == first

    def test_pipe_in_a_value_does_not_break_the_table(self, tmp_path, fig):
        with FigureStore(tmp_path, pdf_name="a.pdf") as store:
            store.save(fig, key="p_g_k",
                       stats=[{"name": "a|b", "test": "t", "p": 0.5}])
        text = render_report(tmp_path).read_text(encoding="utf-8")
        assert "a\\|b" in text


def test_manifest_is_jsonl_one_record_per_line(tmp_path, fig):
    with FigureStore(tmp_path, pdf_name="a.pdf") as store:
        store.save(fig, key="p_g_k1")
        store.save(fig, key="p_g_k2")
    lines = manifest_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["id"] for line in lines] == ["p_g_k1", "p_g_k2"]
