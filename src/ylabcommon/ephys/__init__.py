"""電気生理トレースの共通処理。

取得側 (slice-controller の viewer) と解析側 (slice-analysis) の **両方** が
同じ計算を使うために置く。ここには画面 (PyQt6 / pyqtgraph) を入れない。

- :mod:`~ylabcommon.ephys.filters`: 低域通過フィルタ
- :mod:`~ylabcommon.ephys.event_detection`: EPSC 様イベントの検出
- :mod:`~ylabcommon.ephys.event_record`: 検出結果の記録 (json / csv)
- :mod:`~ylabcommon.ephys.sorted_trace`: sorter が書いた ``*_df.h5`` を読む
"""
