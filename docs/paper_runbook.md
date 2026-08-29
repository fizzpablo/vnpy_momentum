# Paper 运行、故障演练与 Live 解锁

## 前提

- 使用 Paper TWS 端口 `7497` 或 Paper IB Gateway 端口 `4002`。
- 配置账户必须是指定 Paper 账户；`account` 是该进程的单账户白名单。配置解析器拒绝其他端口与任何 `live` 环境。
- 安装与本仓库版本匹配的 `vnpy_ib`、IB API、`pandas_market_calendars` 和策略运行依赖。
- 首次仅配置一个 US 或一个 HK 标的；不得混合市场或币种。
- 在 TWS/IB Gateway 中确认真实行情权限。没有可验证成交额时，策略应保持无 BUY。

## 启动

1. 用 `pytest tests/test_user_strategy*.py -q` 通过离线测试。
2. 配置 YAML 的 Paper 端口、账户、basket、资本和 state path。
3. 启动：`python -m user_strategy.run_paper path/to/paper.yaml`。
4. 观察日志：账户、唯一 Equity contract、参考价、行情与 OMS 快照必须已到达；否则保持 `PAUSED`。
5. 人工键入 `start` 后，确认只有全部 BUY gate 为真时才可能发送限价 BUY。

禁止通过修改端口、环境变量或 monkey patch 启动 Live。

手动交易或撤改单的安全流程见 [manual_intervention.md](manual_intervention.md)。

每个部署只允许一个进程；launcher 会在 state 文件旁创建本地 lock。若 lock 已存在，
先确认旧进程确已停止并检查其 broker 状态，不能直接启动第二个实例。

## Live 前 preflight

在每次 Paper 演练或未来提交 Live 解锁申请前逐项确认：

- 当前配置仍为 `environment: paper`，端口为 `7497` 或 `4002`；
- 启动输出中的账户与配置的单账户白名单完全一致；
- 每个 basket 标的已收到唯一的 `EQUITY` contract；
- 参考价、行情时间戳、市场日历和成交额 gate 均已实际验证；
- 无本策略无法解释的持仓或 active order；
- state 文件可写，本地 instance lock 不存在冲突；
- 单笔金额、单票上限、总资本和 active-order 上限已复核；
- 当前 Git commit、vn.py、vnpy_ib、IB Gateway 和 Python 依赖版本已记录；
- 磁盘空间和 vn.py 日志目录已检查。

未满足任何一项时保持 `PAUSED`。不要将“没有产生订单”误认为 preflight 通过；
应确认每项输入确实被 gateway/OMS 正常提供。

## 连续 Paper 验收

建议连续运行至少 10 个正常交易日，并保留每次启动的日志和策略状态文件。验收记录应逐项包括：

- 启动、暂停、恢复、正常退出；
- 每笔 `signal → order request → order event → trade event → position → STOP`；
- 无行情、延迟/未知成交额、收市、午休时零 BUY；
- 断线恢复后对账及人工重新 `start`；
- 订单拒绝、取消、部分成交和 STOP 替换；
- 每日核对 TWS Paper 的订单/成交/持仓与策略日志。

任何一项不一致均为 `HALTED`，不得继续该次验收。

## 人工故障演练

每项均在 Paper 执行并记录屏幕/日志证据。

| 演练 | 操作 | 预期结果 |
| --- | --- | --- |
| 断线 | 断开 TWS/Gateway API | 新 BUY 立即暂停；不得重发旧 identity |
| 重连 | 恢复 API | 完成 OMS 对账后仍为 `PAUSED`，需人工 `start` |
| 重启 | 持仓或 open STOP 时重启进程 | 发现 basket 持仓/open order 一律 `HALTED`，人工认领/处理 |
| 部分成交 | Paper 以可部分成交的限价单测试 | 按 broker `PositionData` 建立同数量 STOP，不把 partial 当故障 |
| 拒单 | 使用 Paper 可控的拒单条件 | 订单终态为拒绝；同一 identity 不重试 |
| 撤单 | 撤销 entry 或 STOP | entry 仅等待新的 rising edge；STOP 撤销未确认时绝不发第二笔 SELL |
| 账户错误 | 使用错误账户配置 | 拒绝 `start`，无订单 |
| 收市/午休 | 在不可交易时段发出有效信号 | 无 BUY；已有退出逻辑仍可运行 |

## Live 解锁（当前未授权）

当前代码没有 Live 配置路径。任何 Live 工作都必须在新的用户指令中明确授权，且应逐项附上：

1. Paper 验收和故障演练完整证据；
2. 已确认的账户、标的、币种、风险上限与交易时段；
3. 对 `vnpy_ib` 行情数据契约的 Paper 实测证据；
4. 指定的 Live 端口和独立的、不可复用的授权文本；
5. 人工监控、紧急平仓和回滚负责人。

没有这份独立授权，不实施、也不建议实施 Live 解锁。
