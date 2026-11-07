"""Fire Loop 项目验收入口（M1a 起，后续里程碑追加套件）。

真起 agent 进程 + 真跑 CLI，逐条核对 docs/fire-loop-方案.md 验收清单。
不是模块单测的替代：单测验逻辑，本套件验真实进程行为。

隔离设计（重点）
----------------
每次运行都会生成一份完全独立的验收环境：

- 唯一命名管道：``\\\\.\\pipe\\fireloop-accept-<pid>-<uuid8>``（B2 事件阶段再加 ``-events``）
- 唯一单实例互斥体：``Local\\FireLoopAccept-<pid>-<uuid8>``
- 唯一临时 FIRELOOP_HOME（token / 日志 / config.json 都写在里面）
- B1 测试注册表前缀：``fireloop-test-<uuid8>``（绝不等于生产前缀 fireloop）

测试 agent 用 ``python -m fireloop.agent --pipe-name ... --mutex-name ...`` 启动，
CLI 每次都显式带 ``--pipe-name``。所以：

- 全程**不碰**生产管道 ``\\\\.\\pipe\\fireloop-implant``；
- 全程**不碰**生产互斥体 ``Local\\FireLoopAgent``；
- 全程**不碰**生产注册表键（HKCU\\Software\\Classes\\fireloop、FireLoop 右键、Run\\FireLoop）；
- 用户电脑上真实 agent 常驻时，验收照样能跑，且**绝不杀**真实 agent
  （套件只管理自己 subprocess 拉起来的 pid）。

运行（docs/验收.md 有完整说明）：

    cd D:\\ds植入式
    $env:PYTHONPATH = "$PWD\\src"
    python tests\\acceptance\\run_m1a_acceptance.py               # 默认全跑
    python tests\\acceptance\\run_m1a_acceptance.py --suite m1a   # 只跑 M1a 基线回归

可选参数：

    --suite LIST              all（默认）或逗号分隔的 m1a,b1,b2
    --json <path>             机器可读结果路径（默认写 %TEMP%\\fireloop-acceptance\\）
    --skip-graceful-exit      跳过 Ctrl+Break 优雅退出项并标 SKIP（无控制台环境用；默认真测）
    --enable-desktop-events   真测剪贴板/前台窗口事件（会短暂改动桌面/剪贴板；默认 SKIP）

退出码：0 = 全部通过（SKIP 不算失败）；1 = 有失败项或脚本内部错误。
"""

from __future__ import annotations

import sys

import harness
import checks_b1
import checks_b2
import checks_m1a


def pipeline(ctx, args) -> None:
    """按「同一个测试 agent 的生命周期」编排各套件。"""
    suites = set(ctx.suites)

    # 1) M1a 基线：agent 启动 + 基础协议行为（0 ~ 7b）
    checks_m1a.run_agent_phase(ctx)

    # 2) 需要 M1a 测试 agent 在跑的扩展项（共用同一个 agent，不另外起进程）
    if "b1" in suites:
        checks_b1.run_agent_phase(ctx)
    if "b2" in suites:
        checks_b2.run_agent_phase(ctx)

    # 3) M1a 收尾：Ctrl+Break 优雅退出 + 退出后离线 + 日志检查（8a/8b/9/9b/9c）
    checks_m1a.run_shutdown_phase(ctx)

    # 4) 不需要 agent 的扩展项
    if "b1" in suites:
        checks_b1.run_offline_phase(ctx)
    if "b2" in suites:
        checks_b2.run_cross_user_phase(ctx)
        checks_b2.run_default_config_phase(ctx)
        checks_b2.run_events_phase(ctx)

    # 5) 清场 + 残留/红线/隔离自检（必须最后）
    checks_m1a.run_final_phase(ctx)


if __name__ == "__main__":
    sys.exit(harness.run_main(sys.argv[1:], pipeline))
