"""Fire Loop（火环）结果展示器入口：agent 处理完任务后，用 pythonw 拉起本文件显示结果。

为什么需要它：项目是 ``src/`` 布局，``pythonw -m fireloop.result_window`` 要求 ``src``
在 ``sys.path`` 里（PYTHONPATH 或把包安装进 site-packages）。本入口用 ``__file__``
推算项目根、把 ``<项目根>\\src`` 插进 ``sys.path``，所以不依赖 PYTHONPATH、不改环境变量、
不挑启动时的工作目录。

调用方（由 agent 的任务处理器拉起，不是用户手动跑）：
    pythonw "<项目根>\\install\\show.py" --title <标题> --text-file <UTF-8 文件>
    pythonw "<项目根>\\install\\show.py" --title <标题> --text <文本>

也可以经启动器转发：
    python install\\launch.py show --title <标题> --text-file <文件>

退出码：
    0   正常显示、用户关闭窗口
    2   参数错误（由 fireloop.result_window 返回）
    3   无 GUI 环境（TclError，由 fireloop.result_window 返回）
    4   入口自己 import 失败（源码缺失/损坏；pythonw 无控制台，只能靠退出码排查）

注意：不带 --help 直接跑会真的弹出窗口；验证/排障请用 --help。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 项目根：本文件位于 <项目根>\install\show.py，上两级就是项目根。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

EXIT_IMPORT_FAILED = 4


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    src = str(SRC_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)

    try:
        from fireloop.result_window import main as result_main
    except Exception as exc:  # 入口坏了要给出可读原因，不要裸 traceback
        print(
            f"show.py 启动失败：import fireloop.result_window 失败（{type(exc).__name__}: {exc}）；"
            f"源码目录 {SRC_DIR}",
            file=sys.stderr,
        )
        return EXIT_IMPORT_FAILED

    return result_main(args)


if __name__ == "__main__":
    sys.exit(main())
