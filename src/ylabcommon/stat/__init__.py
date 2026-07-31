"""統計検定ユーティリティ (ylab 共通)。

``steel.py`` の Steel 検定 (対照群との多重比較, Dunnett のノンパラ版) などを提供する。

NOTE: このファイルは同梱のためには必要ない。``[tool.setuptools.packages.find]`` を
pyproject に書いた場合 ``namespaces`` の既定は True (= find_namespace_packages) なので、
``__init__.py`` の無いディレクトリも配布物に含まれる。実際 ``models/parameters`` /
``bioio/core`` / ``parser`` は ``__init__.py`` を持たないまま同梱されている
(setuptools に pyproject を解釈させると packages に並ぶ)。
以前ここに「このファイルが無いと同梱されない」と書いていたが誤りだったので訂正する。
"""
