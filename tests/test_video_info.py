"""VideoInfo の解析オーナー(実行PC名 + 開始時刻)の検証。

DLC は複数のワークステーションが同じ raw_video を取り合う。`analysis_status` だけだと
"analyzing" のまま固着したフォルダを見ても「生きている解析なのか、落ちた残骸なのか」
「どのPCが掴んだのか」が分からない。status と同時に PC 名と開始時刻を書く。

既存の video_info.json には新フィールドが無いので、**読めなくなっていないこと**を
最優先で固定する(壊れると全プロジェクトの DLC が止まる)。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from ylabcommon.models.parameters.behavior import VideoInfo


class TestBackwardCompatibility:
    def test_old_json_without_new_fields_still_loads(self):
        """新フィールドを持たない既存の video_info.json がそのまま読める。"""
        old = '{"raw_video_list": ["a.avi"], "analysis_status": "pending", "dlc_param": "x"}'
        v = VideoInfo(**json.loads(old))
        assert v.analysis_status == "pending"
        assert v.analysis_host is None and v.analysis_started_at is None

    def test_unset_status_is_still_normalised(self):
        """既存の "" / null -> pending の正規化を壊していない。"""
        assert VideoInfo(raw_video_list=[], analysis_status="").analysis_status == "pending"
        assert VideoInfo(raw_video_list=[], analysis_status=None).analysis_status == "pending"

    def test_dump_round_trips(self):
        v = VideoInfo(raw_video_list=["a.avi"])
        v.mark_analyzing()
        again = VideoInfo(**json.loads(json.dumps(v.model_dump())))
        assert again.analysis_host == v.analysis_host
        assert again.analysis_started_at == v.analysis_started_at


class TestMarkAnalyzing:
    def test_sets_status_host_and_time_together(self):
        v = VideoInfo(raw_video_list=["a.avi"])
        v.mark_analyzing()
        assert v.analysis_status == "analyzing"
        assert v.analysis_host                      # platform.node()
        # tz 付き ISO8601 として読み戻せること(後で経過時間を出すのに要る)
        started = datetime.fromisoformat(v.analysis_started_at)
        assert started.tzinfo is not None

    def test_matches_the_host_used_by_betterstack_log(self):
        """video_info.json とログを同じ名前で突き合わせられること。"""
        from ylabcommon.utils import betterstack_log

        v = VideoInfo(raw_video_list=[])
        v.mark_analyzing()
        assert v.analysis_host == betterstack_log._runtime_context()["host"]

    def test_overwrites_a_previous_owner(self):
        """別PCが掴み直したら上書きされる(古いPC名が残らない)。"""
        v = VideoInfo(raw_video_list=[], analysis_host="old-pc",
                      analysis_started_at="2020-01-01T00:00:00+09:00")
        v.mark_analyzing()
        assert v.analysis_host != "old-pc"
        assert v.analysis_started_at != "2020-01-01T00:00:00+09:00"


class TestOwnerLabel:
    def test_reports_host_time_and_elapsed_hours(self):
        started = datetime.now(timezone.utc) - timedelta(hours=2.5)
        v = VideoInfo(raw_video_list=[], analysis_host="ws-hpc",
                      analysis_started_at=started.isoformat(timespec="seconds"))
        label = v.analysis_owner_label()
        assert "ws-hpc" in label and "2.5h ago" in label

    def test_unknown_when_nothing_recorded(self):
        """旧データ(未記録)でも例外にせず、分からないことを言う。"""
        assert VideoInfo(raw_video_list=[]).analysis_owner_label() == "unknown host"

    def test_host_only_when_time_is_missing(self):
        v = VideoInfo(raw_video_list=[], analysis_host="ws-hpc")
        assert v.analysis_owner_label() == "ws-hpc"

    def test_broken_timestamp_does_not_raise(self):
        """壊れた時刻でラベル生成が落ちると、固着の調査そのものができなくなる。"""
        v = VideoInfo(raw_video_list=[], analysis_host="ws-hpc",
                      analysis_started_at="not-a-time")
        assert v.analysis_owner_label() == "ws-hpc since not-a-time"

    def test_naive_timestamp_is_handled(self):
        """tz 無しで書かれた時刻(手編集など)でも経過時間を出せる。"""
        started = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        v = VideoInfo(raw_video_list=[], analysis_host="ws-hpc", analysis_started_at=started)
        assert "1.0h ago" in v.analysis_owner_label()


class TestStaleness:
    """"analyzing" のまま放置された主張を、いつ奪ってよいか。

    閾値を下回るものまで奪うと走行中の解析と二重に走り、GPU を無駄にしたうえ
    同じフォルダへ同時に書き込む。逆に永久に奪えないと固着したフォルダを誰も
    再解析できない。どちらにも倒れないことを両方向で固定する。
    """

    def _v(self, hours_ago=None, **kw):
        started = None
        if hours_ago is not None:
            started = (datetime.now(timezone.utc)
                       - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        return VideoInfo(raw_video_list=[], analysis_started_at=started, **kw)

    def test_old_claim_is_stale(self):
        from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS

        assert self._v(ANALYZING_STALE_HOURS + 1).analysis_is_stale() is True

    def test_recent_claim_is_not_stale(self):
        from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS

        assert self._v(ANALYZING_STALE_HOURS - 1).analysis_is_stale() is False

    def test_boundary_is_inclusive(self):
        from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS

        assert self._v(ANALYZING_STALE_HOURS + 0.01).analysis_is_stale() is True

    def test_threshold_is_overridable(self):
        assert self._v(3).analysis_is_stale(hours=2) is True
        assert self._v(3).analysis_is_stale(hours=4) is False

    def test_file_mtime_is_used_when_start_time_is_missing(self):
        """開始時刻を書く前の旧データでも、json の mtime で古さを判断できる。"""
        import time

        from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS

        v = self._v()
        old = time.time() - (ANALYZING_STALE_HOURS + 1) * 3600
        assert v.analysis_is_stale(old) is True
        assert v.analysis_is_stale(time.time()) is False

    def test_start_time_wins_over_file_mtime(self):
        """開始時刻がある場合はそちらが正。json は他の理由でも書き換わるため。"""
        import time

        v = self._v(1)
        assert v.analysis_is_stale(time.time() - 100 * 3600) is False

    def test_unknown_age_is_not_stale(self):
        """判断材料が無いときは奪わない(二重解析より固着のほうがまし)。"""
        v = self._v()
        assert v.analysis_age_in_hours() is None
        assert v.analysis_is_stale() is False

    def test_broken_timestamp_falls_back_to_mtime(self):
        import time

        from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS

        v = VideoInfo(raw_video_list=[], analysis_started_at="not-a-time")
        old = time.time() - (ANALYZING_STALE_HOURS + 1) * 3600
        assert v.analysis_is_stale(old) is True
