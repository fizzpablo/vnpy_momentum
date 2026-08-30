# Live 解锁与运行手册

Live 代码路径已经实现，但默认仍不可启动。它不是 Paper 验收的替代品；只有同一 Git
commit 完成 Paper 回归和人工故障演练后，才可由账户授权人解锁最小实盘。

## 不可绕过的启动门禁

Live 配置必须同时满足下列条件：

- `ibkr.environment: live`，且端口为 TWS `7496` 或 IB Gateway `4001`；
- `account` 出现在 `allowed_live_accounts`；`client_id` 出现在
  `allowed_live_client_ids`；两者不得与 Paper 共用；
- 真实部署环境中存在由 `live_approval_env` 指定的非空环境变量。变量值不写入 YAML、Git、
  shell history 或日志；
- 只有一个本机策略实例，独立 Live state、logs 和 backups 路径；
- 所有 basket 合约、账户、行情、参考价和 OMS 对账均已确认。程序启动及重连后保持
  `PAUSED`，永不自动 `start`。

仅修改端口、YAML 的 `environment`、或输入 IBKR 密码均不能跳过前述代码校验。

## 合约与连接

US 标的验证 symbol、exchange 与 `EQUITY`；配置固定 USD/US。HK 标的还必须在 YAML 固定
`conid`、`local_symbol`、`trading_class`，并由 `ContractData.extra` 返回一致值，否则策略
`HALTED`。现有 vn.py `ContractData` 没有这些原生字段；如实际 vnpy_ib 未填充 `extra`，HK
Live 不能解锁，不能猜测或放宽检查。

策略监听 vn.py 原生 `EVENT_LOG` 中属于 IB gateway 的连接/断线文字：断线立即 `PAUSED`；
重连会重做 OMS 快照对账且仍为 `PAUSED`，只能由人工重新 `start`。若实际 Gateway 版本的
日志文字不匹配，Paper 故障演练必须判为失败，不可进入 Live。

## Telegram、备份与恢复

配置 `alerts.telegram_token_env` 和 `alerts.telegram_chat_id`。token 只放在部署环境变量中。
`HALTED`、拒单、断线、损失熔断、异常退出会尝试 Telegram 通知。通知仅包含固定事件名，
不含账户、标的、订单号、价格、数量、错误原文、路径或策略参数；详细信息只写本机日志。
通知失败绝不解除交易门禁。每 60 秒检查 state 所在磁盘，低于 `disk_free_min_mb` 时
`HALTED`。

使用启动命令的 `--backup-dir` 和 `--log-dir`，启动前与退出后复制 state 和 `*.log`：

```text
python -m user_strategy.run_strategy /srv/ibkr/config/live-us.yaml \
  --backup-dir /srv/ibkr/backups/live-us --log-dir /srv/ibkr/logs
```

恢复只可在停策略、保存当前 broker 订单/成交/持仓证据后进行。恢复 state 不会替代对账；
带仓或有活动订单时，必须保持 `HALTED` 并人工处理。

## Live 前验收与首日

1. 运行离线测试；同一 release 在 Paper 回归：

   ```text
   python -m pytest tests/test_user_strategy.py tests/test_user_strategy_market_data.py tests/test_user_strategy_lock.py -q
   ```

2. Paper 演练断线、重连、重启、拒单、撤单、部分成交、未知订单、保护 STOP 失败、日损失
   熔断、错误账户和错误合约；保存日志和 IBKR 截图/导出。
3. 记录 Git commit、镜像 ID（如使用 Docker）、IB Gateway/vn.py/vnpy_ib 版本、配置校验和及
   Paper 证据。容器镜像构建后固定 ID，运行中不安装依赖。
4. 创建非 Git 的 Live YAML（可从 `user_strategy/live.example.yaml` 起步）、独立 state/logs，
   设置 approval 与 Telegram 环境变量。启动后确认仍为 `PAUSED`。
5. 首日只允许一个标的、一个 active entry、最小 `max_order_notional`；全程人工在场。每笔
   成交核对订单、成交、仓位和保护 STOP 数量。任一差异执行 `kill`/`pause` 并人工接管。

Live 解锁是账户授权人的单独操作。代码存在 Live 路径并不代表已经获得实盘批准。
