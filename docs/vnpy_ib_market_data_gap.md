# vnpy_ib 行情与账户缺口：最小增强契约及测试计划

## 范围与约束

本文件只定义为 `user_strategy` 提供 Paper Trading 所需数据时，`vnpy_ib`
应公开的最小数据契约和测试矩阵。

- `vnpy` core 不修改；现有 `TickData.extra`、`BarData.turnover` 和
  `AccountData.extra` 已足以承载 gateway 专有元数据。
- 不新建 IB socket/client，不替代 `IbGateway` 的订单、持仓、成交或重连机制。
- 策略层只订阅 vn.py 事件并消费原生对象；它不读取 `gateway.api` 等私有属性。
- 本阶段仅设计和测试计划；本文件本身不授权任何 gateway 实现改动。

## 现状

`TickData` 有 `volume`、`turnover`、`last_price`、`last_volume` 与 `datetime`
字段，但当前 `vnpy_ib`：

- 订阅时的 generic tick 列表为空；
- 处理累计量（IB tick 8）、最新价和最新量；
- 仅将 `LAST_TIMESTAMP` 写入 tick 时间；
- 不填充 `turnover`，不公开行情类型，也不解析 RT Volume；
- 历史 `BarData` 不填充 `turnover`；
- 账户映射未公开 `CashBalance`。

因此，纯策略层不能辨别实时/延迟行情，也无法得到可靠累计成交额。不得以
`last_price * TickData.volume` 代替：它不是 VWAP 成交额，无法验证数据类型和
更新时间，并可能错误地放开 BUY 闸门。

## IB 数据来源

IB generic tick `233` 的 RT Volume 包含当日累计成交量、VWAP 和成交时间。US
股票量以 100 股 lot 报告，故必须由每个标的的经过 Paper 验证的
`volume_multiplier` 显式换算。RT Volume 和 RT Trade Volume 的包含范围不同；
策略默认使用 RT Trade Volume，只有其不可用时才使用 RT Volume，并在日志写明
所用来源。

来源：

- <https://interactivebrokers.github.io/tws-api/tick_types.html>
- <https://interactivebrokers.github.io/tws-api/md_receive.html>

## 最小 gateway 契约

### 1. 实时行情元数据

`IbGateway.subscribe()` 为股票请求 generic tick `233`。收到 IB 回调后，发布
普通 `EVENT_TICK`/`EVENT_TICK + vt_symbol`，且 `TickData.extra` 至少包含：

```python
{
    "ib_market_data_type": int,       # 仅 1 可作为实时数据
    "ib_rt_volume": float | None,     # RT Volume 的当日累计量
    "ib_rt_trade_volume": float | None,
    "ib_vwap": float | None,
    "ib_rt_time": datetime | None,    # timezone-aware，统一为 UTC 或保留明确 tz
}
```

不为缺失数据编造默认值。解析失败、数据无效或未知时置为 `None`。`extra` 必须为
新的 dict/copy，避免后续 gateway 回调修改已投递事件。

`ib_market_data_type` 必须来自 IB `marketDataType` 回调，而不能由端口、时间戳或
是否收到价格推断。策略仅接受 `1`；任何 delayed、frozen、未知或非整数值均禁止
新开仓和加仓。

### 2. 历史成交额

`IbGateway.query_history(HistoryRequest)` 对 `TRADES` 的一分钟 bar，使用 IB bar
的 `wap * volume` 填入原生 `BarData.turnover`。数值无效时应使该 bar 的
`turnover` 保持 0，并以日志明确说明原因；策略把所需 session 内任一无效 bar
视为 seed 失败而不是 0 成交额。

这只用于 US 在 pre/regular/post 中途启动时，对已完成的一分钟 bar 做 session
seed。HK 直接以 RT 累计日成交额为准，午休不重置。

### 3. CashBalance

接收 IB `updateAccountValue` 的 `CashBalance` 时，在对应账户/币种的
`AccountData.extra` 放入：

```python
{"ib_cash_balance": float}
```

`AccountData.accountid` 保持现有 `{account}.{currency}` 约定，不改变 `balance`
含义。每次账户时间更新发布该账户对象，使 OMS 和策略可通过 `EVENT_ACCOUNT` 或
`MainEngine.get_all_accounts()` 消费。策略只接受配置账户、配置报价币种（HK 为
HKD）且恰有一条有效 `CashBalance`；其他情形 fail closed。

## 策略侧薄 adapter

`user_strategy.market_data.MarketDataAdapter` 只消费 `EVENT_TICK`、历史
`BarData` 与 `EVENT_ACCOUNT`：

1. 验证 `last_price > 0`、行情类型为 1、`ib_rt_time` 带时区且未过期；
2. 取 `rt_trade_volume`，仅在其缺失时取 `rt_volume`；
3. 计算 `cumulative_turnover = volume * ib_vwap * volume_multiplier`；
4. 将数值按 NYSE/HKEX calendar 的 session key 累计：US 分 pre/regular/post，
   HK 跨午休按本地交易日累计；
5. 对累计量/额回退、session 不匹配、时间未来或异常、calendar 未知，立即使
   BUY gate 无效并发出 alert；
6. adapter 不调用下单 API，亦不管理连接或重连。

## Paper-only 启动契约

- 配置仅接受端口 `7497`（TWS Paper）或 `4002`（IB Gateway Paper）。
- 配置不提供 `live` 分支；任何其他端口均拒绝启动。
- 重连后，策略仍必须等待 gateway 数据、订单、持仓、账户 reconciliation 明确
  完成才允许 `RUNNING`。

端口是本项目确定的 Paper 互锁；它不替代 TWS/IB Gateway 的实际配置审查。

## 日历契约

使用 `pandas_market_calendars`，US 为 `NYSE`，HK 为 `HKEX`。它是随 Python
依赖安装后离线调用的日历源，负责休市、早收市和 schedule；策略代码不硬编码
假日日期。日历不可导入、schedule 查询失败或时段不明确时，禁止新开仓并告警。

来源：<https://pandas-market-calendars.readthedocs.io/en/stable/calendars.html>

## 测试计划（先于实现）

### gateway 级

| 场景 | 断言 |
| --- | --- |
| 订阅股票 | `reqMktData` 请求包含 `233`，仍保持原有行情订阅行为 |
| RT Volume 有效 | 发布 tick 的 `extra` 有累计量、VWAP、aware 时间与 data type |
| RT Trade Volume 与 RT Volume 同时存在 | 两者均保留，策略可优先前者 |
| malformed RT 字符串 | 不抛异常、不虚构数值，字段为 `None` 并记录错误 |
| delayed/frozen/unknown data type | 原样公开状态，不标成实时 |
| callback copy 语义 | 旧 tick 事件的 `extra` 不会被后续回调改变 |
| 一分钟历史 bar | `turnover == wap * volume`；无效 WAP/量不会变为有效成交额 |
| CashBalance | 仅正确 account/currency 的账户事件带 `ib_cash_balance`；不覆盖 `balance` |

### adapter / 策略级

| 场景 | 最终断言 |
| --- | --- |
| realtime + 新鲜 + 严格高于阈值 | BUY gate 可通过（其他入场条件满足时） |
| 等于成交额阈值 | 不产生订单 |
| delayed、frozen、未知、无时间、过期 tick | 无新订单，策略状态保持或进入规定暂停/告警状态 |
| US pre/regular/post 边界 | 各自独立累计；中途启动只使用完整分钟 seed |
| HK 午休 | 禁止 BUY，但同一日成交额不重置 |
| 成交额或累计量回退 | BUY gate 失效，直到按明确规则重新建立基线 |
| 缺失/重复/错误币种 CashBalance | 无新订单 |
| calendar 不可用或 schedule 异常 | `PAUSED`/alert，绝不发 BUY |
| gateway disconnect/reconnect | 重连后对账完成前无新订单，旧 signal identity 不重发 |
| duplicate signal | 同一 identity 只产生一个意图和一个订单 |
| partial fill | 以真实 `TradeData`/`PositionData` 管理已成交数量并建立等量 STOP；未成交部分不导致直接 HALT |

## Signal identity

策略持久化：

```text
strategy_id + symbol + turnover_session_key + reference_trade_date
+ signal_rising_edge_sequence
```

同一 identity 的 rejected/cancelled 订单不自动重试。仅当完整入场条件先变为 false、
再变为 true 时，才生成新的 rising edge sequence；partial fill 使用原 identity
接管已成交仓位，不能被重复 tick 重复下单。

## 非授权项

本文件不授权修改 `vnpy/trader/*`，不授权搬运 `ib_async`，不授权连接 IBKR 或向
任何账户下单。实际 gateway 改动须在本测试计划被实现为失败测试后，单独确认。
