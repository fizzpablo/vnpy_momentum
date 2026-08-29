# 策略逻辑与使用手册

## 目的与范围

这是一个仅做多股票的动量策略，运行在 vn.py + vnpy_ib + IBKR 上。它当前只允许
IBKR Paper Trading；不会自动切换到 Live。

本手册解释日常使用和策略行为。交易规则的正式来源是根目录
[`strategy-specification.md`](../strategy-specification.md)。手册不构成投资建议。

## 一句话逻辑

在允许的交易时段内，当某个白名单股票同时满足：

1. 相对前一交易日收盘价达到规定涨幅；
2. 当前秒的成交量显著高于近期每秒平均成交量；
3. 累计成交额严格高于流动性门槛；
4. 账户、合约、行情、日历、对账和策略状态全部确认正常；

策略以带滑点上限的限价 BUY 开始建仓。持仓后建立 IBKR 托管的 STOP MKT 保护单；
移动止损、浮盈回撤或全局损失熔断触发时，策略先处理保护 STOP，再按 broker 实际
剩余仓位市价 SELL。

任何关键输入未知时，策略不会新开仓或加仓。

## 标的与运行边界

- 仅交易 YAML `basket` 中的 symbol whitelist。
- 单一策略进程只能包含一个市场和一个报价币种：US/USD 或 HK/HKD。
- 标的必须收到与配置匹配的 `EQUITY` contract，才允许 `start`。
- 当前配置只接受 Paper TWS `7497` 或 Paper IB Gateway `4002` 端口。
- 策略使用配置中的单一 IBKR account；不匹配的账户不能使策略进入 `RUNNING`。
- 启动或重大重连后，若 basket 中已有无法解释的仓位或 active order，策略进入
  `HALTED`，等待人工处理。

## 策略状态

| 状态 | 含义 | 是否能发起新的 BUY |
| --- | --- | --- |
| `PAUSED` | 默认状态或人工暂停 | 否 |
| `RUNNING` | 已人工启动，所有 preflight 通过 | 是，但仍须满足个股信号和风控 |
| `HALTED` | 发现影响交易正确性的关键异常 | 否，须人工处理 |

每个标的还有独立状态：

| 标的状态 | 含义 |
| --- | --- |
| `IDLE` | 空仓，可等待下一次新 signal |
| `ENTERED` | broker 已确认有仓位，策略管理其 STOP 和退出 |
| `HALTED` | 标的已止损/异常，等待人工 `reset` |

`pause` 不会撤销已经存在的订单或 STOP；它只禁止新增开仓/加仓。`close_all` 的范围
只限本策略 basket 中明确管理的仓位。

## 交易信号

### 参考价与动量

参考价是前一个交易日的 regular-session close。若没有可靠的参考价，不产生信号。

动量条件：

```text
last_price / previous_close - 1 >= price_gain_threshold_pct
```

默认门槛为 `8%`。

### 成交量突发

策略按每秒累计成交量的增量建立最近 300 秒窗口。

- 至少收集 `volume_warmup_seconds` 秒；
- 每秒平均成交量必须不低于 `volume_min_avg`；
- 当前秒成交量必须**严格大于** `volume_surge_ratio × 平均每秒成交量`。

默认值是：预热 120 秒、突发倍数 3 倍。

### 成交额门槛

累计成交额必须**严格大于** `turnover_threshold`。行情时间戳超过
`turnover_max_age_sec`、价格/成交额无效、日历未知或市场关闭时，BUY gate 关闭。

当前实现不会用 `last_price × volume` 猜测成交额；只有 gateway 明确提供可靠的
`TickData.turnover` 或 `tick.extra['cumulative_turnover']` 时才允许该条件通过。
因此在尚未验证 vnpy_ib 的实际 Paper 行情字段前，策略可能一直保持不交易；这是预期
的 fail-closed 行为。

### 信号去重

同一 signal 不会因为重复 tick、重复回调、重连或重启而重复发 BUY。策略使用每个标的
的单调递增 signal sequence，并将已消费 sequence 持久化。

被拒绝或取消的订单不会对同一个 signal 自动重发；信号必须先失效、再重新成立，才会
形成新的 signal identity。

## 开仓与加仓

首次建仓和加仓都要求：

- 引擎为 `RUNNING`；
- 已完成 account、contract、broker state 对账；
- 市场日历确认开市；
- 没有同一标的的待处理 entry order；
- 信号、成交额和行情新鲜度全部通过；
- 风控上限允许。

BUY 使用 LIMIT：

```text
limit_price = last_price × (1 + slippage_pct)
```

数量按基础资金比例、单票上限、总资本、lot size、滑点和手续费准备金向下取整。

加仓还要求价格达到：

```text
last_price >= broker_avg_cost × (1 + scale_in_gain_pct)
```

部分成交是正常情况。策略只依据 IBKR/vn.py 返回的真实 `TradeData` 和
`PositionData` 管理已成交部分，并为实际持仓建立相同数量的保护 STOP。

## 退出与保护

### 本地退出信号

| 类型 | 触发条件 | 动作 |
| --- | --- | --- |
| 移动止损 | `price <= peak_price × (1 - stop_loss_pct)` | 平仓流程 |
| 浮盈回撤 | 当前浮盈从峰值回撤达到 `profit_drawdown_stop_pct` | 平仓流程 |
| 全局熔断 | 已实现加未实现 PnL 触及 `-aum_loss_limit_pct × capital` | 全部策略仓位退出并 `HALTED` |
| 人工 `close_all` | 用户命令 | 退出明确管理的策略仓位，随后 `PAUSED` |

本地软止损只使用新鲜行情；不以 stale tick 触发。

### 灾难 STOP

每个已确认持仓必须有 IBKR 托管的 `STOP MKT`：

```text
stop_price = broker_avg_cost × (1 - catastrophe_stop_pct)
```

`catastrophe_stop_pct` 必须严格大于 `stop_loss_pct`，使本地移动止损在正常连接时先
起作用，broker STOP 则保护进程断线或崩溃情形。

退出时，策略先确认撤销保护 SELL STOP；只有撤单终态明确后，才按 broker 当前剩余
数量发送 MARKET SELL，以避免意外做空。

## 风控与异常处理

除策略信号外，系统还有简单硬限制：

- symbol whitelist；
- 单笔最大名义金额：`max_order_notional`；
- 单票最大配置与 `max_position_pct`；
- 总资本限制；
- 最大 active orders：`max_active_orders`；
- lot size 向下取整。

下列情况禁止新开仓，通常保持 `PAUSED`：行情 stale、市场关闭、日历不确定、账户/合约
未确认、资金条件不满足、重连后未完成对账。

下列关键异常进入 `HALTED`：无法解释的 broker 仓位或订单、broker/local position
mismatch、负仓位、保护 STOP 缺失且仍有仓位、退出订单被拒绝、状态文件损坏、全局熔断。

普通行情缺失或短暂市场关闭不是自动 `HALTED` 的理由；它们只关闭新开仓 gate。

## 日常怎么用

1. 从 [`user_strategy/paper.example.yaml`](../user_strategy/paper.example.yaml) 复制一个
   私有 Paper 配置，填写 Paper account、basket、资本与风险上限。
2. 不要把账户凭据或 `.env` 提交 Git。
3. 启动前运行测试：

   ```text
   python -m pytest tests/test_user_strategy.py tests/test_user_strategy_market_data.py tests/test_user_strategy_lock.py -q
   ```

4. 启动 Paper：

   ```text
   python -m user_strategy.run_paper path/to/paper.yaml
   ```

5. 程序启动后默认 `PAUSED`。确认账户、合约、行情、参考价和对账均正常后，手工输入：

   ```text
   start
   ```

6. 可用命令：

   | 命令 | 作用 |
   | --- | --- |
   | `start` | 在 preflight 完整时进入 `RUNNING` |
   | `pause` | 停止新的开仓/加仓 |
   | `close_all` | 退出明确管理的策略仓位 |
   | `reset` | 仅在 `HALTED` 且全部确认空仓时回到 `PAUSED` |
   | `quit` / `exit` | 正常关闭进程 |

7. 手动介入时，请遵循 [`manual_intervention.md`](manual_intervention.md)，不要在策略
   `RUNNING` 时直接人工交易 basket 标的。

## 经常调整的参数

下列参数是策略使用者通常会调整的业务参数；每次修改后应重新运行测试，并先在 Paper
观察行为。

| 参数 | 用途 | 常见调整原因 |
| --- | --- | --- |
| `basket` | 股票白名单、交易所、币种、lot、单票额度 | 更换或增删标的 |
| `capital` | 策略总资金 | 改变整体规模 |
| `max_allocation` | 单票名义上限 | 控制个股集中度 |
| `max_order_notional` | 单笔名义上限 | 防止数量/价格异常扩大订单 |
| `max_active_orders` | 同时 active order 上限 | 约束操作复杂度 |
| `price_gain_threshold_pct` | 动量阈值 | 调整入场强度 |
| `volume_surge_ratio` | 量能突发倍数 | 调整信号敏感度 |
| `volume_warmup_seconds` | 成交量预热时间 | 改变启动后等待时间 |
| `volume_min_avg` | 最低平均每秒量 | 过滤低流动性标的 |
| `turnover_threshold` | 成交额流动性闸门 | 适配市场/标的流动性 |
| `turnover_max_age_sec` | 允许的行情新鲜度 | 适配行情质量，通常不应放得过宽 |
| `base_position_pct` | 首次/每次加仓的基础比例 | 调整建仓速度 |
| `max_position_pct` | 单票最大总比例 | 控制集中度 |
| `scale_in_gain_pct` | 相对均价的加仓涨幅 | 调整加仓条件 |
| `slippage_pct` | BUY 限价上浮 | 控制成交概率和价格保护 |
| `estimated_fee_pct` | 数量计算/PnL 的费用准备金 | 适配市场费用 |
| `stop_loss_pct` | 自峰值回撤止损 | 调整风险容忍度 |
| `profit_drawdown_stop_pct` | 峰值浮盈允许回撤 | 调整利润保护强度 |
| `catastrophe_stop_pct` | broker STOP 相对均价距离 | 必须大于 `stop_loss_pct` |
| `aum_loss_limit_pct` | 策略级损失熔断 | 调整单策略最大损失 |

## 通常不应频繁更改的参数

- `environment`：当前必须为 `paper`；不可改为 `live`。
- `port`：只应为 Paper 端口 `7497` 或 `4002`。
- `account`、`gateway_name`、`host`、`client_id`：属于部署身份；修改后必须完整重启并
  重新对账。
- `state_path`：迁移前先停止策略并备份 state 文件。
- `symbol`、`exchange`、`currency`、`market`：属于 contract identity；修改后要重新
  验证 IBKR contract。

## 修改参数的安全流程

1. `pause` 并确认没有待处理的新 entry order；
2. 备份当前 YAML 和 state；
3. 只修改需要变化的参数；
4. 运行配置加载和策略测试；
5. 重启 Paper 策略，完成 preflight；
6. 观察日志和 broker 状态后再人工 `start`。

风险参数、basket、账户、交易所、币种或 port 的任何变化，都不应在持仓或 active
order 尚未解释清楚时进行。
