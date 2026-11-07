# Fire Loop（火环）第一阶段方案

更新日期：2026-11-07
仓库：D:\ds植入式
当前阶段：M1a（本地管道 + CLI），不接模型、不写注册表、不装服务。

---

## 0. 项目定位

Fire Loop（中文名：火环）是一套装在个人 Windows 11 电脑上的用户态常驻 agent。

- 系统能直接调用她：命名管道 + 用户级协议 + 右键菜单（M1b）。
- 她能感知系统：文件、剪贴板、前台窗口事件（M1b 起）。
- 能用 API 的地方直接调 API，不模拟人点击。

**本项目为个人自用，不考虑对外发布。** 不做：安装包、代码签名、自动更新、多用户分发、商业化、跨机器兼容性承诺。所有设计以"在这台机器上长期稳定好用"为第一优先级。

旧项目（小鲸鱼）与本项目没有任何关联，不 import、不复制、不参考其代码结构。旧交接报告只作为历史，已于 2026-11-07 移入回收站。

---

## 1. 命名与路径（定死，不再改）

| 项 | 值 |
|---|---|
| 显示名 | Fire Loop |
| 中文名 | 火环 |
| 代码标识 | fireloop |
| Python 包 | src/fireloop/ |
| 常驻进程入口 | `python -m fireloop.agent` |
| CLI 入口 | `python -m fireloop.cli` |
| 命名管道 | `\\.\pipe\fireloop-implant` |
| 单实例互斥体 | `Local\FireLoopAgent` |
| 数据目录 | `%LOCALAPPDATA%\FireLoop\` |
| 日志目录 | `%LOCALAPPDATA%\FireLoop\logs\` |
| 运行时目录 | `%LOCALAPPDATA%\FireLoop\run\` |
| token 文件 | `%LOCALAPPDATA%\FireLoop\agent.token` |
| 协议 scheme | `fireloop://`（M1b 注册） |
| 注册表键 | `HKCU\Software\Classes\fireloop`（M1b） |
| 右键菜单 | `HKCU\Software\Classes\*\shell\FireLoop`（M1b） |
| 开机自启 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\FireLoop`（M1b） |

测试可用环境变量 `FIRELOOP_HOME` 覆盖数据目录，避免污染真实目录、避免多实例互踩。

---

## 2. 边界

### 第一阶段做

- 用户登录后常驻 agent（用户态，无 Windows 服务）；
- 命名管道服务端 + CLI 客户端；
- 单实例、token 首帧鉴权、超时不挂死；
- M1b：用户级协议、文件/文件夹右键、登录自启、文件/剪贴板/前台窗口事件。

### 第一阶段不做

- Windows 服务、内核驱动、替换 explorer、登录凭据提供者、COM Shell Extension；
- 双进程拆分、"服务负责想、代理负责看"；
- 截图当眼睛、模拟键鼠（能用 API 先用 API）；
- 把任何旧项目的代码、记忆、UI、图标搬进来。

---

## 3. 技术路线（已拍板）

- **IPC：ctypes 直调 Win32 命名管道 + JSON Lines**（一行一条 UTF-8 JSON）。
  不用 pywin32（Python 3.15 的 wheel 可用性未验证）；不用 `multiprocessing.connection`（线协议不可控、拿不到 pipe handle，M1b 做 SID 校验要返工）。
- **单实例：ctypes `CreateMutexW`**，命名互斥体 `Local\FireLoopAgent`；重复启动直接退出。
- **鉴权：token 首帧**。首次启动用 `secrets.token_hex(32)` 生成并写入 `agent.token`；服务端校验。M1b 追加调用方 SID 校验。
- **日志：JSONL**，不记任务原文（只记长度和 sha256 前 8 位），写 `logs/agent-YYYYMMDD.jsonl`。
- **测试：标准库 `unittest`**，不引入 pytest；行为验收必须起真实进程，假测试只验逻辑。
- **Python：系统 `D:\Python\python.exe` 3.15.1，M1a 零第三方依赖，不建 venv。**
- 编码统一 UTF-8（Python 3.15 默认 UTF-8 mode），不用额外折腾。

---

## 4. 目录结构

```
D:\ds植入式\
  AGENTS.md
  README.md
  requirements.txt
  docs\
    fire-loop-方案.md      本文件，唯一权威
    协议-v1.md              管道协议规范
  src\fireloop\
    __init__.py             包信息、版本
    paths.py                路径与目录（LOCALAPPDATA / FIRELOOP_HOME）
    log.py                  JSONL 日志
    protocol.py             协议常量、消息构造与解析（纯逻辑，可单测）
    single_instance.py      ctypes 互斥体（T1）
    pipe_server.py          命名管道服务端（T1）
    agent.py                常驻入口：单实例 + 服务端循环（T1）
    client.py               客户端连接/收发（T2）
    cli.py                  CLI 入口（T2）
  tests\
    test_protocol.py        协议单测
    test_pipe_server.py     （T1）
    test_client.py          （T2）
  install\                  M1b 起放注册表/卸载脚本
```

公共底座 `paths.py` / `log.py` / `protocol.py` 由 Lead 独占维护，子 agent 只引用不修改。

---

## 5. 里程碑与验收

### M1a 管道 + CLI（当前）

验收（全部真跑，只认真实输出）：

- [ ] `python -m fireloop.agent` 启动后，`python -m fireloop.cli ping` 输出 pong；
- [ ] `python -m fireloop.cli task "测试"` 把任务送到 agent 并回显；
- [ ] agent 没在跑时 CLI 2 秒内明确报"agent 没在跑"，退出码非 0，不挂死；
- [ ] token 不对时 CLI 明确报错，退出码非 0；
- [ ] 第二个 agent 实例启动被单实例锁挡住，退出码非 0；
- [ ] 非法 JSON、超长消息、缺字段不导致 agent 崩溃；
- [ ] 0 模型调用、不写注册表、不装服务。

### M1b 系统身份 + 事件（下一阶段）

- HKCU 注册 `fireloop://` 协议、文件/文件夹右键、登录自启；
- 文件事件 `ReadDirectoryChangesW`、剪贴板 `AddClipboardFormatListener`、前台窗口 `SetWinEventHook`；
- 事件去抖、去重、配额、`event_dispatch` 日志；
- 管道调用方 SID 校验。

### 服务化

第一阶段不做，甚至不做。只有确实需要"登录前也能跑"才重新评估。

---

## 6. 团队与工作方式（Lead 职责）

本项目由 Lead（项目负责人）统一调度，子会话 agent 按模块开发。

### Lead 职责（写死，防止上下文遗忘）

1. 拆分任务：写清目标、独占文件范围、依赖、验收命令，上团队任务板。
2. 公共底座（`paths/log/protocol`、文档、打包/提交）由 Lead 独占，不外包。
3. 分发任务给子 agent；一个子 agent 同一时间只做一个任务。
4. 子 agent 交付后，Lead 审 `git diff`、亲自重跑验收；不合格写清单打回，最多两轮，第三轮 Lead 接手。
5. 每个里程碑一个中文提交，不混提交。
6. 完成后向用户报告固定五段：改了什么 / 证据 / 没解决或风险 / 回退方法（系统级改动时）/ 成本变化。
7. 用户新安排的改动：先给拆分方案和计划，用户拍板后才分发；不先斩后奏。

### 子 agent 硬规则

- 只改任务书写明的文件，绝不碰范围外文件；
- 禁止碰注册表、服务、开机自启、模型 API；
- 交付必须报真实命令输出；没跑等于没完成；
- 做完只反馈 Lead，由 Lead 决定接受/打回/继续优化，子 agent 不自行扩大改动。

---

## 7. 风险与待办

| 项 | 方案 | 状态 |
|---|---|---|
| 标准用户测试账号 | 建本地标准用户 `fl_test`（随机密码），M1b 验证管道 ACL/SID 用；**测完必须删除账号** | 已批准，M1b 前执行 |
| 远程仓库 | https://github.com/FireLoop-note/The-FireLoop.git，每个里程碑 push | 配置中 |
| pywin32 兼容性 | M1a 不用；M1b 前用 `pip download pywin32 --no-deps` 验证 cp315 wheel，没有就继续全 ctypes | 待做 |
| 代码签名 / 安装包 | 个人自用，不做，不阻塞 | 已关闭 |
| 沙箱受限模式 pwsh 故障 | 开发期间使用完全访问模式；不处理 | 已关闭 |

---

## 8. 文档索引

- `docs/fire-loop-方案.md`：本文件，唯一权威
- `docs/协议-v1.md`：管道协议规范
- `README.md`：项目入口说明
- `AGENTS.md`：开发规则与协作约定
