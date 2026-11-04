"""测试用的插件包引导。

插件目录名 ``whale-affect`` 含有连字符，不能直接用 ``import`` 语句导入；
生产环境里 Runner 用 ``spec_from_file_location`` 以合成包名加载 ``plugin.py``，
包内的 ``from .models import ...`` 因此可用。

测试按下述方式复现同样的包结构，使被测模块保持与运行时一致的相对导入，
不需要在生产代码里写 ``try: from .x except ImportError: from x`` 这类掩盖错误的兜底。
"""

from __future__ import annotations

import importlib.util
import sys
import sysconfig
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "whale_affect_under_test"

# 与 SDK / Runner 一致的第三方依赖路径，未安装 SDK 时才需要显式提供。
SDK_PATH = Path(sysconfig.get_paths()["purelib"])


def install_package() -> str:
    """把插件目录注册成可导入的合成包。

    Returns:
        str: 注册使用的包名。
    """

    if PACKAGE_NAME in sys.modules:
        return PACKAGE_NAME

    if SDK_PATH.is_dir() and str(SDK_PATH) not in sys.path:
        sys.path.append(str(SDK_PATH))

    spec = importlib.util.spec_from_loader(PACKAGE_NAME, loader=None, is_package=True)
    if spec is None:
        raise RuntimeError("无法为插件目录创建包 spec")

    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE_NAME] = package
    return PACKAGE_NAME


install_package()
