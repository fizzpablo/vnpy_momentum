# 项目策略专用回测操作手册

本项目新增的 `user_strategy.backtesting` 是一个命令行专用回测入口。它使用 `vnpy.alpha.strategy.BacktestingEngine` 读取本地 K 线、回放策略、统一撮合并导出日报、订单、成交和绩效统计。

它与 Fusion 的【CTA回测】窗口是两条独立路径：无需将策略放入 CTA 界面，也不会从 CTA 数据库读取数据。

## 适用范围

该脚本只能运行继承 `AlphaStrategy` 的策略类，并要求策略依据**已生成的信号表**和日线或分钟线交易。例如示例配置使用 `EquityDemoStrategy`，按 `signal` 列排序并进行调仓。

项目中的 `user_strategy.engine.StrategyEngine` 是纸面交易的 Tick/OMS 事件策略，它依赖逐秒累计成交量、累计成交额、订单状态和保护性止损状态。因此不能直接交给此回测引擎运行。若要评估这套策略，必须先将入场规则转成无未来函数的 K 线/分钟线信号，再使用本脚本回测；若必须复刻逐 Tick 的量能触发和订单生命周期，则应另建 Tick 级事件回测器。

## 文件位置

- 回测脚本：`user_strategy/backtesting.py`
- 配置样例：`user_strategy/backtest.example.json`
- Alpha 策略示例：`vnpy/alpha/strategy/strategies/equity_demo_strategy.py`

## 前置准备

### 1. 安装 Alpha 依赖

在项目环境中安装 Alpha 可选依赖：

```bash
python -m pip install -e ".[alpha]"
```

这会提供 `polars`、`pyarrow` 等读取 Parquet 信号和历史 K 线所需的依赖。

### 2. 准备 AlphaLab 目录和 K 线数据

脚本使用 `AlphaLab` 目录，而不是 CTA 回测窗口的数据库。目录结构如下：

```text
alpha_lab/
├── contract.json
├── daily/                 # 日线：<vt_symbol>.parquet
├── minute/                # 分钟线：<vt_symbol>.parquet
└── signal/                # 信号：*.parquet
```

日线示例中 `AAPL.SMART` 的文件名必须为 `daily/AAPL.SMART.parquet`。K 线应通过 `AlphaLab.save_bar_data()` 写入；该方法会保留 `datetime`、OHLC、成交量、成交额和持仓量字段。

### 3. 设置合约交易成本

每一个参与回测的 `vt_symbol` 都必须在 `contract.json` 中有设置；否则脚本会在启动时中止，避免使用缺失成本的结果。可用以下一次性初始化代码生成或更新配置：

```python
from vnpy.alpha import AlphaLab

lab = AlphaLab("alpha_lab")
lab.add_contract_setting(
    vt_symbol="AAPL.SMART",
    long_rate=0.0005,
    short_rate=0.0005,
    size=1,
    pricetick=0.01,
)
```

`long_rate` 和 `short_rate` 是按成交金额计算的费率；`size` 是合约乘数；`pricetick` 是最小价格跳动。请按实际市场、券商和账户口径填写。

该引擎没有独立的“滑点”配置字段。策略调用 `execute_trading(bars, price_add)` 时，`price_add` 会影响限价委托的报价和可成交性；应在策略参数中采用保守取值，并通过成交明细复核实际撮合价格。

### 4. 准备信号表

信号可使用 Parquet 或 CSV，必须含有以下列：

| 列名 | 类型 | 说明 |
| --- | --- | --- |
| `datetime` | 日期时间 | 必须和回测 K 线时间戳完全对应；CSV 使用 ISO-8601 格式 |
| `vt_symbol` | 字符串 | 本地代码，例如 `AAPL.SMART` |
| `signal` | 数值 | 供策略使用的信号值；示例策略按从大到小排序 |

信号生成必须只使用该时点及以前可获得的数据。若日线信号使用了当日收盘价，成交应安排在下一根 K 线；否则会产生未来函数。当前 `BacktestingEngine` 在每根 K 线开始时撮合上一根 K 线提交的订单，因此应结合策略下单时点仔细验证成交时序。

## 配置与运行

复制 `user_strategy/backtest.example.json` 为本地配置文件后，按实际路径和参数修改。所有路径都相对于配置文件所在目录解析。

```json
{
  "lab_path": "../alpha_lab",
  "signal_path": "../alpha_lab/signal/my_signal.parquet",
  "vt_symbols": ["AAPL.SMART", "MSFT.SMART"],
  "interval": "d",
  "start": "2024-01-01T00:00:00",
  "end": "2024-12-31T23:59:59",
  "capital": 1000000,
  "strategy": {
    "class": "vnpy.alpha.strategy.strategies.equity_demo_strategy:EquityDemoStrategy",
    "setting": {"top_k": 10, "n_drop": 2, "min_days": 3}
  },
  "output_dir": "backtest_output/2024"
}
```

关键字段：

- `interval`：`d` 表示日线，`1m` 表示分钟线；必须与 AlphaLab 数据目录和信号时间戳一致。
- `strategy.class`：使用 `Python模块路径:类名`。目标类必须继承 `AlphaStrategy`。
- `strategy.setting`：传递给策略类的参数；仅填写该策略实际定义的参数。
- `annual_days`：年度交易日数，股票日线通常可设为 `252`；未填写时使用默认值 `240`。
- `risk_free`：年化无风险利率小数，例如 `0.02` 表示 2%。

从仓库根目录运行：

```bash
python -m user_strategy.backtesting user_strategy/backtest.local.json
```

如需在运行结束后打开 Plotly 图表：

```bash
python -m user_strategy.backtesting user_strategy/backtest.local.json --show-chart
```

脚本会在终端输出统计指标，并在 `output_dir` 写入以下文件：

- `statistics.json`：起止日期、收益、最大回撤、夏普比率、手续费和成交数；
- `daily_results.csv`：逐日盯市盈亏、成交额、手续费与净盈亏；
- `orders.csv`：全部委托及其最终状态；
- `trades.csv`：全部成交明细。

## 接入自定义策略

自定义策略必须继承 `AlphaStrategy` 并实现三个回调：

```python
class MyStrategy(AlphaStrategy):
    def on_init(self) -> None:
        pass

    def on_bars(self, bars) -> None:
        signal = self.get_signal()
        # 根据 bars、signal 和当前持仓调用 set_target()/execute_trading()

    def on_trade(self, trade) -> None:
        pass
```

在 `on_bars` 中通过 `self.get_signal()` 取得当前 K 线时间的信号，通过 `set_target(vt_symbol, volume)` 设置目标仓位，并调用 `execute_trading(bars, price_add)` 提交调仓委托。不要在这里读取未来日期的信号或 K 线。

将类的导入路径填入 `strategy.class` 后即可由脚本加载，无需修改回测脚本。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `Missing contract settings` | 为每个 `vt_symbol` 使用 `lab.add_contract_setting()` 配置费率、乘数和最小跳动。 |
| `No bar data was loaded` | 检查 `lab_path`、文件名、`interval`、日期范围和 K 线文件是否一致。 |
| 信号找不到 | 确保 `datetime` 与 K 线时间戳完全相同，且没有时区或日内收盘时刻差异。 |
| 回测无成交 | 检查信号是否覆盖回测日期、策略阈值、目标仓位和下单价格设置。 |
| 结果异常乐观 | 复核成交时点、费率、价格跳动、滑点假设和是否引用未来数据；增加样本外验证。 |

## 每次回测的归档要求

保存本次配置 JSON、策略代码版本、原始信号文件版本、K 线数据来源、`contract.json`、四份输出文件，以及样本内/样本外划分说明。回测结果只能作为研究验证，不应直接视为实盘收益承诺。
