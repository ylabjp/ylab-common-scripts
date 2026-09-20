"""電気生理トレースの共通処理。

取得側 (slice-controller の viewer と ``scli``) と解析側 (slice-analysis の
``roicli`` と集計) の **両方** が同じ計算を使うために置く。ここには画面
(PyQt6 / pyqtgraph) を入れない。

下から順に:

- :mod:`~ylabcommon.ephys.filters`: 低域通過フィルタ
- :mod:`~ylabcommon.ephys.event_detection`: EPSC 様イベントの検出
- :mod:`~ylabcommon.ephys.raw_trace`: 取得直後の h5 を読む
- :mod:`~ylabcommon.ephys.sorted_trace`: sorter が書いた ``*_df.h5`` を読む
- :mod:`~ylabcommon.ephys.recording`: 2 つの形を **同じ形で** 開いて切り出す中間層
- :mod:`~ylabcommon.ephys.procedure`: 開く→選ぶ→掛ける→記録を組む、の手順
- :mod:`~ylabcommon.ephys.event_record`: 検出結果の記録 (json / csv)

画面から回すときも、コマンド (agent) から回すときも
:func:`~ylabcommon.ephys.procedure.run_detection` を通る。段取りを画面の中に
書かないのは、同じ値が 2 つの道から出るのを防ぐため。
"""
