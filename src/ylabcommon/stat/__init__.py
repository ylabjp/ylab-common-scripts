"""統計検定ユーティリティ (ylab 共通)。

``steel.py`` の Steel 検定 (対照群との多重比較, Dunnett のノンパラ版) などを提供する。

NOTE: このファイルが無いと ``ylabcommon.stat`` は setuptools の
``[tool.setuptools.packages.find]`` (= find_packages) で検出されず、
``ylabcommon @ git+https://...`` としてインストールした先に同梱されない
(= import 時に ModuleNotFoundError になる)。名前空間パッケージのままにせず、
明示的にパッケージ化しておくこと。
"""
