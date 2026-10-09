"""webui —— 融合项目的只读仪表盘（借鉴 B 的形态）。

导入本包的任何模块都**不得**产生以下副作用：

* 不 import ``playwright`` / ``cloakbrowser``（本机没装，import 期碰上就是崩溃）；
* 不 import ``core.tasks``（它在模块级执行 ``config = get_config()`` / ``userData = get_userData()``，
  import 即求值，且会连带拉起 ``core.browser`` → ``cloakbrowser``）；
* 不读 ``.env``（只有 ``python -m webui`` 入口读，见 ``webui/_main_.py``）。

所有环境变量与账号数据都在**函数内部**惰性读取，保证 ``python -c "import webui.app"`` 恒成功。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
