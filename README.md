# Fire Loop（火环）

个人自用的 Windows 11 常驻 agent：系统能直接调用她，事件能直接通知她，能用 API 的地方不模拟人点击。

当前阶段：**M1a（本地管道 + CLI）**——不接模型、不写注册表、不装服务。

本项目为个人自用，不考虑对外发布（不做安装包、代码签名、自动更新、多用户分发）。

## 文档

- [第一阶段方案](docs/fire-loop-方案.md)——唯一权威文档，架构、命名、边界、验收都以它为准
- [管道协议 v1](docs/协议-v1.md)——T1 服务端和 T2 客户端共同遵守的线协议
- [开发规则](AGENTS.md)

## 开发期快速开始

```powershell
cd D:\ds植入式
$env:PYTHONPATH = "$PWD\src"

python -m fireloop.agent        # 启动 agent（占一个窗口）
python -m fireloop.cli ping     # 返回 pong
python -m fireloop.cli task "测试"
```

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
