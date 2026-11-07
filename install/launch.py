"""Fire Loop（火环）启动器：注册表协议/右键/登录自启命令的唯一入口。

为什么不直接用 ``pythonw -m fireloop.cli``：项目是 ``src/`` 布局，``-m`` 要求
``src`` 在 ``sys.path`` 里（靠 PYTHONPATH 或把包安装进 site-packages）。这个启动器
用 ``__file__`` 推算项目根、把 ``src`` 插进 ``sys.path``，所以：

- 不依赖 PYTHONPATH，不改任何环境变量；
- 不挑启动时的工作目录（资源管理器/注册表拉起时 cwd 是什么都行）；
- 注册表里只需要写一份稳定的命令行。

注册表里的命令（由 install/install.ps1 写入）：
    "<pythonw.exe>" "<项目根>\\install\\launch.py" cli uri "%1"
    "<pythonw.exe>" "<项目根>\\install\\launch.py" cli file "%1"
    "<pythonw.exe>" "<项目根>\\install\\launch.py" agent

结果展示器（agent 处理完任务后由 task_processor 拉起，也可走本启动器）：
    "<pythonw.exe>" "<项目根>\\install\\launch.py" show --title <标题> --text-file <文件>

手动用法：
    python install\\launch.py cli ping
    python install\\launch.py cli file "D:\\资料\\示例.txt"
    python install\\launch.py cli uri "fireloop://task?text=hello"
    python install\\launch.py agent [--pipe-name ...] [--mutex-name ...]
    python install\\launch.py show --help
"""

from __future__ import annotations

import sys
from pathlib import Path

# 项目根：本文件位于 <项目根>\install\launch.py，上两级就是项目根。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

USAGE = """Fire Loop（火环）启动器 install/launch.py

用法:
  launch.py cli <参数...>      转发给 fireloop.cli（ping / task / file / uri ...）
  launch.py agent [参数...]    启动常驻 agent（可传 --pipe-name / --mutex-name）
  launch.py show [参数...]     转发给 fireloop.result_window（结果展示窗口）
  launch.py --help             显示本帮助

说明:
  这个文件是注册表协议/右键/自启命令的唯一入口。
  它自己把 <项目根>\\src 加进 sys.path，所以不依赖 PYTHONPATH、不挑工作目录。

退出码:
  透传被转发模块的退出码（cli 按 docs/协议-v1.md；agent 0 正常退出/1 已有实例；
  show 0 正常关闭/2 参数错误/3 无 GUI）；启动器自身参数错误返回 2。
"""


def _ensure_src_on_path() -> None:
    """把项目的 src 目录插到 sys.path 最前面。幂等。"""
    src = str(SRC_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        print(USAGE, end="", file=sys.stderr)
        return 2
    if args[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0

    target = args[0]
    rest = args[1:]

    if target not in ("cli", "agent", "show"):
        print(f"启动器错误：不认识的入口 {target!r}（只支持 cli / agent / show）", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2

    if not SRC_DIR.is_dir():
        print(f"启动器错误：找不到源码目录 {SRC_DIR}", file=sys.stderr)
        return 2

    _ensure_src_on_path()

    try:
        if target == "cli":
            from fireloop.cli import main as target_main
        elif target == "agent":
            from fireloop.agent import main as target_main
        else:
            from fireloop.result_window import main as target_main
    except Exception as exc:  # 包坏了要给出可读原因，而不是裸 traceback
        print(
            f"启动器错误：import fireloop 失败（{type(exc).__name__}: {exc}）；"
            f"源码目录 {SRC_DIR}",
            file=sys.stderr,
        )
        return 4

    return target_main(rest)


if __name__ == "__main__":
    sys.exit(main())
