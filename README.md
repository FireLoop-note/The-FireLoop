# Fire Loop（火环）

个人自用的 Windows 11 常驻 agent：系统能直接调用她，事件能直接通知她，能用 API 的地方不模拟人点击。

当前阶段：**M2（接模型：右键文件处理）**——DeepSeek `deepseek-flash`；右键文件 → 火环读文件、调模型摘要 → 弹结果窗口。模型可在 config 里关闭（关闭时退回 M1 回显）。M1b 的系统身份和事件已落地，事件默认关闭。

本项目为个人自用，不考虑对外发布（不做安装包、代码签名、自动更新、多用户分发）。

## 文档

- [第一阶段方案](docs/fire-loop-方案.md)——唯一权威文档，架构、命名、边界、验收都以它为准
- [管道协议 v1](docs/协议-v1.md)——T1 服务端和 T2 客户端共同遵守的线协议
- [模型接入](docs/模型接入.md)——DeepSeek 客户端、key 存放与隐私、成本控制
- [安装与卸载](docs/安装.md)——HKCU 注册表、右键、自启、启动器、dry-run 用法
- [事件配置](docs/事件.md)——文件/剪贴板/前台窗口事件开关与隐私口径
- [验收](docs/验收.md)——48 项验收套件、隔离原理、跨用户 SID 实测
- [开发规则](AGENTS.md)

## 开发期快速开始

```powershell
cd D:\ds植入式
$env:PYTHONPATH = "$PWD\src"

python -m fireloop.agent        # 启动 agent（占一个窗口）
python -m fireloop.cli ping     # 返回 pong
python -m fireloop.cli task "测试"
python -m fireloop.cli file "D:\资料\a.txt"
python -m fireloop.cli uri "fireloop://task?text=%E4%BD%A0%E5%A5%BD"
```

注册表入口统一走启动器 `install\launch.py`，不依赖 PYTHONPATH：
`pythonw install\launch.py cli ping`。

日常使用：右键任意文件 →「显示更多选项」→「交给火环」，几秒后弹出结果窗口。
模型开关在 `%LOCALAPPDATA%\FireLoop\config.json` 的 `model.enabled`；API key 在
`secrets.json`（不进仓库）。

测试：

```powershell
python -m unittest discover -s tests -v
```

## 验收（随时可跑，不影响正在运行的 agent）

```powershell
$env:PYTHONPATH = "$PWD\src"
python tests\acceptance\run_m1a_acceptance.py
```

每次运行自动生成隔离管道、隔离互斥体和临时数据目录，真实 agent 正在跑也不冲突、不会被杀掉；输出 20 项 PASS/FAIL，`--json` 可拿机器可读结果。详见 [验收文档](docs/验收.md)。

环境：Windows 11，Python 3.15.1（`D:\Python\python.exe`），M1a 零第三方依赖。

## 目录

```
src/fireloop/   代码
tests/          测试
docs/           文档
install/        注册表/卸载脚本（M1b 起）
```
