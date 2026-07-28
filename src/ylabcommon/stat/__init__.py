"""統計検定のユーティリティ。

このファイルが無いと setuptools の `[tool.setuptools.packages.find]`
(= find_packages) が `ylabcommon.stat` をパッケージとして認識せず、
インストール済みの ylabcommon に stat/ が同梱されない。ソースを
PYTHONPATH で直読みする場合は名前空間パッケージとして解決されるため
気付きにくいが、依存側(behavior-analysis の draw_graph)は
`from ylabcommon.stat.steel import steel_test` を import 時に評価するので、
同梱されないとモジュールの読み込み自体が失敗する。
"""

from ylabcommon.stat.steel import steel_test

__all__ = ["steel_test"]
