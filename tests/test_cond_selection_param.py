"""GroupAnalysisItemParam の cond_by_mouse / exclude_mice の静的検証。

cond_by_mouse は cond_group の「単位を mouse に置き換えた版」で、書けば in / 書かなければ
out になる。書き方の矛盾を黙って一方に倒すと **n が意図せず減った図が正常に見える**ため、
データを見なくても判定できる矛盾は config 読み込み時に落とす。

データ依存の検査(存在しない mouse_id / どの群にも入らない個体)は behavior_analysis 側の
resolve_cond_selection が警告する。ここはあくまで静的な検査だけ。
"""
import pytest
from pydantic import ValidationError

from ylabcommon.models.parameters.behavior import GroupAnalysisItemParam


def _ga(**kw):
    return GroupAnalysisItemParam(group_analysis_param="x.yaml", **kw)


class TestDefaults:
    def test_absent_by_default(self):
        ga = _ga()
        assert ga.cond_by_mouse is None and ga.exclude_mice is None

    def test_existing_config_still_loads(self):
        """既存 config(cond_group だけ)が影響を受けないこと。"""
        ga = _ga(cond_group={"ACSF": ["cond_ACSF"]})
        assert ga.cond_group == {"ACSF": ["cond_ACSF"]}
        assert ga.cond_by_mouse is None


class TestAccepted:
    def test_cond_by_mouse_alone(self):
        ga = _ga(cond_by_mouse={"Responder": ["m1", "m3"], "NonResponder": ["m2"]})
        assert list(ga.cond_by_mouse) == ["Responder", "NonResponder"]

    def test_exclude_mice_alone(self):
        assert _ga(exclude_mice=["m9"]).exclude_mice == ["m9"]

    def test_exclude_mice_with_cond_group(self):
        """cond 構成はそのままに個体だけ外す、という組み合わせは正当。"""
        ga = _ga(cond_group={"ACSF": ["cond_ACSF"]}, exclude_mice=["m9"])
        assert ga.exclude_mice == ["m9"]

    def test_exclude_mice_with_cond_by_mouse_when_disjoint(self):
        """併用の意味は薄いが、重ならない限り矛盾ではないので通す。"""
        ga = _ga(cond_by_mouse={"R": ["m1"]}, exclude_mice=["m9"])
        assert ga.exclude_mice == ["m9"]

    def test_empty_cond_by_mouse_is_treated_as_absent(self):
        """cond_by_mouse: {} は「指定なし」。cond_group との併用チェックにも掛からない。"""
        ga = _ga(cond_group={"ACSF": ["cond_ACSF"]}, cond_by_mouse={})
        assert ga.cond_group == {"ACSF": ["cond_ACSF"]}


class TestRejected:
    def test_cond_group_and_cond_by_mouse_together(self):
        """どちらも「その行の cond は何か」を決めるので重ねられない。"""
        with pytest.raises(ValidationError, match="cannot be used together"):
            _ga(cond_group={"ACSF": ["cond_ACSF"]}, cond_by_mouse={"R": ["m1"]})

    def test_group_with_no_mouse(self):
        """空グループは書き間違い。通すと凡例だけ出て中身が無い図になる。"""
        with pytest.raises(ValidationError, match="no mouse"):
            _ga(cond_by_mouse={"Responder": ["m1"], "NonResponder": []})

    def test_same_mouse_in_two_groups(self):
        with pytest.raises(ValidationError, match="only one group"):
            _ga(cond_by_mouse={"R": ["m1", "m2"], "N": ["m2"]})

    def test_mouse_in_both_cond_by_mouse_and_exclude(self):
        with pytest.raises(ValidationError, match="both cond_by_mouse and exclude_mice"):
            _ga(cond_by_mouse={"R": ["m1", "m2"]}, exclude_mice=["m2"])

    def test_error_names_the_offending_mouse(self):
        """どれを直せばよいかがメッセージだけで分かること。"""
        with pytest.raises(ValidationError) as e:
            _ga(cond_by_mouse={"R": ["m1"], "N": ["m1"]})
        assert "m1" in str(e.value) and "'R'" in str(e.value) and "'N'" in str(e.value)
