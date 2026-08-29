# IBKR 手动介入安全操作手册

## 基本规则

策略运行期间，不要在 TWS、IB Gateway、IBKR Client Portal 或其他程序中手动交易
策略 basket 内的标的，也不要修改策略发出的订单。

- 非 basket 标的：不属于本策略范围，但仍应避免与策略使用相同的专用账户。
- basket 标的：任何手动买入、卖出、撤单或修改 STOP 都可能改变策略预期的仓位和
  保护订单数量。
- `pause` 只停止新的开仓/加仓；它**不会**撤销已存在的限价单或灾难 STOP。
- `close_all` 只处理策略明确管理的 basket 标的，不清空整个 IBKR 账户。

遇到不确定情况，优先停止策略和人工核对，不能猜测订单归属或仓位数量。

## 正常手动退出：优先使用策略命令

如果只是希望停止新交易或退出策略仓位：

1. 在策略终端输入 `pause`。
2. 在日志和 TWS 中确认没有新的 BUY 被发送。
3. 需要退出时输入 `close_all`。
4. 等待策略先确认撤销保护 STOP，再按 broker 实际仓位发送市价 SELL。
5. 在 TWS 和策略日志中确认：相关订单终态明确、basket 仓位为零。
6. 保持 `PAUSED`；若要重新开始，确认行情、账户和对账正常后再输入 `start`。

这是首选路径，不需要手工在 IBKR 提交相同标的的订单。

## 必须手工操作时

例如策略终端不可用、出现异常订单，或你必须立即采取人工措施。

### 操作前

1. 若策略仍可响应，先输入 `pause`。
2. 记录当前时间、标的、IBKR account、broker 持仓数量、均价及所有 active orders。
3. 确认该标的是 basket 标的；若是，假定存在策略管理的 STOP 或未成交 entry order。
4. 不要同时让策略和人工对同一标的发送 SELL。

### 人工操作原则

- 不要手动修改策略的限价 BUY、STOP 或 exit order；如必须撤单，先记录 IBKR order id。
- 若手动卖出策略持仓，先确认是否仍有策略 SELL STOP。未处理的 STOP 在你卖出后仍可能
  触发，导致意外空头风险。
- 对无法确认的 STOP 撤单状态，不再发送第二笔 SELL；保留人工处置并核对 broker。
- 任何手工买入/卖出后，都不得直接恢复策略。

### 操作后恢复

1. 停止策略进程，或让其保持 `HALTED`；不要直接输入 `start`。
2. 在 IBKR 中核对该策略 basket 的：positions、open orders、executions。
3. 确认每个 basket 标的均满足其一：
   - 仓位为零且无相关 active order；或
   - 已由人工明确接管，策略不再管理它。
4. 若存在无法解释的仓位、订单或成交，保持 `HALTED` 并人工处理；不要使用 `reset`。
5. 只有在 broker 与策略状态均确认空仓、无 relevant open order 时，执行 `reset`，随后
   仍需人工 `start`。
6. 在策略日志中记录：人工介入原因、时间、IBKR order id、最终仓位和恢复决定。

## 紧急情况

### 发现可能重复下单、仓位不一致或订单归属不明

1. 立即 `pause`；若策略无响应，停止策略进程。
2. 不要立刻重复发送订单或反向下单“修正”。
3. 在 IBKR 核对 orders、executions、positions。
4. 若已存在 SELL STOP 或 SELL order，先确认其终态，再决定是否需要人工卖出。
5. 保持 `HALTED`，直到仓位与订单可解释。

### IBKR/Gateway 断线

1. 不手工补发策略原本计划的订单。
2. 等待 gateway 重连并完成对账；策略不会自动恢复 `RUNNING`。
3. 若你在断线期间手工操作，按“操作后恢复”完整处理。

## 不允许的操作

- 策略 `RUNNING` 时在 IBKR 手工交易同一 basket 标的；
- 手动卖出后忽略仍可能有效的策略 STOP；
- 因 timeout、断线或看不到回报而立刻重复下单；
- 未完成对账就 `reset → start`；
- 将 `close_all` 误解为清空整个账户。
