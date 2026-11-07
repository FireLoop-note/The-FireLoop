"""Fire Loop 结果展示窗口（tkinter，标准库，独立短命进程）。

由 agent 处理完任务后以 pythonw 起独立进程调用（见 install/show.py），把结果展示给用户：
标题 + 可滚动只读文本框 + "复制全文" / "关闭" 按钮，窗口居中且不超过 900x700。

命令行：
    python -m fireloop.result_window --title "标题" --text-file "结果.txt"
    python -m fireloop.result_window --title "标题" --text "结果正文"
    （--text 与 --text-file 同时给时优先 --text-file；文本文件按 UTF-8 读，容忍 BOM）

代码里直接调用：
    from fireloop.result_window import show
    show("标题", "正文")

退出码：
    0  正常显示，用户关闭窗口
    2  参数 / 输入错误（缺 --text 或 --text-file、文件读不了、不是合法 UTF-8 等）
    3  没有图形环境（tkinter 缺失或 TclError）
    4  其他内部错误

注意：本进程常由 pythonw.exe 启动，那时 sys.stdout / sys.stderr 可能是 None，
所有输出都走 _emit() 做非空保护，错误路径绝不因为"没有控制台"再崩一次。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

from . import APP_NAME

DEFAULT_TITLE = APP_NAME

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_GUI = 3
EXIT_INTERNAL = 4

MAX_WIDTH = 900
MAX_HEIGHT = 700
MIN_WIDTH = 520
MIN_HEIGHT = 320

_FONT_FAMILY = "Microsoft YaHei UI"

# Win32 剪贴板常量
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

USAGE = """Fire Loop 结果展示窗口

用法:
  python -m fireloop.result_window --title "标题" --text-file "D:\\结果.txt"
  python -m fireloop.result_window --title "标题" --text "结果正文"

选项:
  --title <标题>      窗口标题（缺省 "Fire Loop"）
  --text-file <文件>  从 UTF-8 文件读正文；同时给 --text 时优先本项
  --text <文本>       直接给正文
  -h, --help          显示本帮助

退出码:
  0 正常显示并关闭 / 2 参数错误 / 3 没有图形环境 / 4 内部错误
"""


class UsageError(Exception):
    """命令行参数或输入文件不合法。"""


class HelpRequested(Exception):
    """请求打印帮助。"""


# ---------------------------------------------------------------------------
# 输出（pythonw 下 stdout/stderr 可能是 None）
# ---------------------------------------------------------------------------


def _emit(message: str, stream_name: str = "stderr") -> None:
    stream = getattr(sys, stream_name, None)
    if stream is None:
        return
    try:
        print(message, file=stream)
    except Exception:  # noqa: BLE001 - 没有控制台时输出失败无所谓，不能影响主流程
        pass


# ---------------------------------------------------------------------------
# 参数解析与文本读取（纯逻辑，可在无 GUI 环境单测）
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> tuple[str, str | None, str | None]:
    """解析命令行 → (title, text_file, text)。

    --text 和 --text-file 都允许出现（优先 text-file 由 resolve_text 决定）；
    都不知道就由 resolve_text 报错。非法参数抛 UsageError，-h/--help 抛 HelpRequested。
    """
    title = DEFAULT_TITLE
    text_file: str | None = None
    text: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            raise HelpRequested()

        if arg in ("--title", "--text", "--text-file"):
            if i + 1 >= len(argv):
                raise UsageError(f"{arg} 后面缺少值")
            option, value = arg, argv[i + 1]
            i += 2
        elif arg.startswith("--title="):
            option, value = "--title", arg[len("--title="):]
            i += 1
        elif arg.startswith("--text-file="):
            option, value = "--text-file", arg[len("--text-file="):]
            i += 1
        elif arg.startswith("--text="):
            option, value = "--text", arg[len("--text="):]
            i += 1
        else:
            raise UsageError(f"不认识的参数：{arg}")

        if option == "--title":
            title = value
        elif option == "--text-file":
            text_file = value
        else:
            text = value
    return title, text_file, text


def read_text_file(path: str) -> str:
    """按 UTF-8 读文本文件（utf-8-sig 容忍 BOM，纯 UTF-8 也照常读）。"""
    return Path(path).read_text(encoding="utf-8-sig")


def resolve_text(text_file: str | None, text: str | None) -> str:
    """决定要显示的正文：优先 text-file，其次 --text；都没有抛 UsageError。"""
    if text_file is not None:
        if not text_file.strip():
            raise UsageError("--text-file 路径不能为空")
        return read_text_file(text_file)
    if text is not None:
        return text
    raise UsageError("必须提供 --text 或 --text-file 之一")


# ---------------------------------------------------------------------------
# 窗口（tkinter 在 show() 里才 import：无 GUI 环境下本模块照样能 import 和单测）
# ---------------------------------------------------------------------------


def _no_gui_errors() -> tuple[type[BaseException], ...]:
    """会被视为"没有图形环境"的异常类型。"""
    errors: list[type[BaseException]] = [ImportError]
    try:
        import tkinter

        errors.append(tkinter.TclError)
    except Exception:  # noqa: BLE001 - tkinter 本身不可用时只认 ImportError
        pass
    return tuple(errors)


def _estimate_size(text: str) -> tuple[int, int]:
    """按正文行数/最长行粗略估窗口大小，夹在 [MIN, MAX] 之间。纯逻辑。"""
    lines = text.splitlines() or [""]
    longest = min(max(len(line) for line in lines), 200)
    line_count = min(len(lines), 60)
    width = 320 + longest * 11
    height = 160 + line_count * 24
    return (
        max(MIN_WIDTH, min(MAX_WIDTH, width)),
        max(MIN_HEIGHT, min(MAX_HEIGHT, height)),
    )


def show(title: str, text: str) -> None:
    """弹出结果窗口并阻塞到用户关闭。没有图形环境时抛 TclError / ImportError。"""
    import tkinter as tk  # 延迟 import：无 GUI 环境也能用本模块的纯逻辑

    root = tk.Tk()
    try:
        _build_window(root, tk, title, text)
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _build_window(root: Any, tk: Any, title: str, text: str) -> None:
    root.title(title)
    root.minsize(MIN_WIDTH, MIN_HEIGHT)
    root.maxsize(MAX_WIDTH, MAX_HEIGHT)

    # 居中，且不超过 900x700
    width, height = _estimate_size(text)
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")

    header = tk.Label(
        root,
        text=title,
        anchor="w",
        justify="left",
        font=(_FONT_FAMILY, 12, "bold"),
        wraplength=MAX_WIDTH - 40,
        padx=12,
        pady=8,
    )
    header.pack(side="top", fill="x")

    footer = tk.Frame(root)
    footer.pack(side="bottom", fill="x", padx=12, pady=(0, 12))

    body = tk.Frame(root)
    body.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 8))

    scrollbar = tk.Scrollbar(body, orient="vertical")
    scrollbar.pack(side="right", fill="y")
    text_area = tk.Text(
        body,
        wrap="word",  # 自动换行
        font=(_FONT_FAMILY, 10),
        yscrollcommand=scrollbar.set,
        padx=6,
        pady=6,
        undo=False,
    )
    scrollbar.configure(command=text_area.yview)
    text_area.pack(side="left", fill="both", expand=True)
    text_area.insert("1.0", text)
    text_area.configure(state="disabled")  # 只读：用户只能选中/复制，不能改

    close_btn = tk.Button(footer, text="关闭", width=12, command=root.destroy)
    close_btn.pack(side="right")
    copy_btn = tk.Button(footer, text="复制全文", width=12)
    copy_btn.pack(side="right", padx=(0, 8))

    def on_copy() -> None:
        ok = _copy_to_clipboard(text, tk, root)
        copy_btn.configure(text="已复制" if ok else "复制失败")

        def restore() -> None:
            try:
                copy_btn.configure(text="复制全文")
            except tk.TclError:  # 窗口已经关了
                pass

        root.after(1500, restore)

    copy_btn.configure(command=on_copy)
    root.bind("<Escape>", lambda _event: root.destroy())


def _copy_to_clipboard(text: str, tk: Any, root: Any) -> bool:
    """复制全文。优先 Win32 API（窗口关闭后内容仍在），退化到 tkinter 剪贴板。"""
    if _set_windows_clipboard(text):
        return True
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        return True
    except tk.TclError:
        return False


def _set_windows_clipboard(text: str) -> bool:
    """用 Win32 API 直接把文本放进剪贴板，进程退出后内容不丢。失败返回 False。"""
    if sys.platform != "win32":
        return False
    try:
        return _win32_set_clipboard(text)
    except Exception:  # noqa: BLE001 - 复制失败只影响按钮反馈，不能崩窗口
        return False


def _win32_set_clipboard(text: str) -> bool:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE

    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        # 成功后内存所有权归系统，不能再 GlobalFree
        return True
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回退出码（0/2/3/4），不抛裸 traceback。"""
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        title, text_file, text = parse_args(argv)
        content = resolve_text(text_file, text)
    except HelpRequested:
        _emit(USAGE, "stdout")
        return EXIT_OK
    except UsageError as exc:
        _emit(f"用法错误：{exc}")
        _emit(USAGE)
        return EXIT_USAGE
    except UnicodeDecodeError as exc:
        _emit(f"读取文本失败：文件不是合法的 UTF-8（{exc}）")
        return EXIT_USAGE
    except OSError as exc:
        _emit(f"读取文本失败：{exc}")
        return EXIT_USAGE

    try:
        show(title, content)
    except _no_gui_errors() as exc:
        _emit(f"没有可用的图形环境，无法显示窗口（{type(exc).__name__}: {exc}）")
        return EXIT_NO_GUI
    except Exception as exc:  # noqa: BLE001 - pythonw 下要把一切变成可读错误码
        _emit(f"显示窗口失败：{type(exc).__name__}: {exc}")
        return EXIT_INTERNAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
