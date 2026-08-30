# EC2 t3.small：从部署、Paper 到实盘的操作手册

> **适用范围**：本仓库 `user_strategy` + Interactive Brokers（IBKR）股票策略。
> **当前状态**：代码提供受控的 Paper/Live 启动路径；Live 仍默认锁定，必须同时通过独立的部署授权变量、账户/client ID 白名单、Paper 回归和人工批准。详细门禁见 [Live 解锁与运行手册](live_unlock_runbook.md)。

## 0. 运行边界与上线原则

目标架构如下。策略与 Gateway 必须同机，API socket 仅监听回环地址；不将 4001/4002/7496/7497、VNC 或 Jupyter 暴露到公网。

```text
你 -- SSH(22, 固定源 IP) --> EC2 Ubuntu t3.small
                                        |-- IB Gateway（Paper 或 Live）
                                        |-- user_strategy（单实例、人工 start）
                                        '-- EBS：配置、状态、日志、备份
```

`t3.small` 为 2 vCPU、2 GiB 内存的突发型实例；它只适合一个 Gateway、一个策略进程和少量标的。若长期内存吃紧、CPU credit 不足、订阅增多或需要并行策略，应在进入实盘前升至至少 `t3.medium`。参见 [AWS EC2 规格](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html)。

全程遵守以下不变量：

- 仅一份配置、一个 `strategy_id`、一个 `state_path`、一个 Gateway client ID 对应一个策略进程。
- 新进场只由人工输入 `start` 解锁；重连、重启或 systemd 重启后不得自动恢复交易。
- `pause` 优先于排障；账户、订单、成交和持仓未完成对账时绝不 `start`。
- Broker 持仓与活动订单是交易真值；本地状态、日志和策略推断不能覆盖它。
- 不在 Git、YAML、shell 历史或日志中保存 IBKR 密码、2FA 代码、AWS access key 或私钥。

## 1. 简化的上线关口

| 阶段 | 目标 | 进入下一阶段前必须完成 |
| --- | --- | --- |
| 基础设施 | 安全、可登录的 EC2 | SSH、磁盘、swap、备份可用；API/VNC 未对公网开放 |
| Paper 联调 | 在 `PAUSED` 下确认连接 | 账户、合约、行情、订单/持仓对账正常 |
| Paper 验收 | 小额模拟运行 | 10 个交易日及故障演练有日志；不是“没下单”就算通过 |
| Live（受控） | 最小规模实盘 | 完成第 6 节验收，账户授权人解锁，首日全程在场并收盘对账 |

## 2. AWS 与 Ubuntu 初始化（阶段 A）

### 2.1 创建 EC2

1. 创建 Ubuntu 24.04 LTS、`t3.small`、30 GiB 以上 gp3 EBS 的实例；启用 EBS 加密和定期快照。记录区域、实例 ID、EBS volume ID 和 AMI 版本。
2. 使用 Elastic IP（可选）方便固定 SSH 地址。安全组只允许 TCP 22，来源限为自己的固定公网 IP/CIDR；不使用 `0.0.0.0/0`。
3. 交易进程不需要 AWS access key；不要把 access key 写入实例、配置或 shell 历史。
4. 在本地连接：

   ```bash
   ssh -i ~/.ssh/ibkr-ops.pem ubuntu@EC2_ELASTIC_IP
   ```

### 2.2 系统与资源准备

以下均在 EC2 上执行，将 `YOUR_TZ` 改为运维时区。交易时段判断仍由策略市场日历决定；系统时区仅影响日志显示。

```bash
sudo apt update
sudo apt -y full-upgrade
sudo apt install -y ca-certificates curl git tmux htop jq unzip \
  build-essential python3 python3-venv python3-pip python3-dev \
  libgl1 libegl1 libxrender1 libxext6 libxtst6 libxi6 libxrandr2 \
  fonts-dejavu-core xvfb
sudo timedatectl set-timezone YOUR_TZ
timedatectl status
```

为 t3.small 建立 2 GiB swap。先确认根目录至少剩余 3 GiB：

```bash
df -h /
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

创建运行账户和目录。之后 Gateway 与策略均以 `trader` 身份运行，绝不以 root 运行：

```bash
sudo adduser --disabled-password --gecos '' trader
sudo install -d -o trader -g trader -m 0750 /opt/vnpy /srv/ibkr/{config,state,logs,releases}
sudo -iu trader
```

## 3. 部署代码与验证（阶段 A）

策略部署有两种**二选一**的方式：原生 Python（本节 3.1）或 Docker Compose（本节 3.2）。两者均使用宿主机的 IB Gateway，均必须保留人工 `start` 门禁；不得在同一账户、`strategy_id` 或 state 文件上同时运行两个版本。

### 3.1 原生 Python

以 `trader` 身份执行。固定 Git commit 和依赖版本；生产主机不做无审计的 `pip install -U`。

```bash
git clone <只读仓库地址> /opt/vnpy
cd /opt/vnpy
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -e .
python -m pip install vnpy_ib ibapi PyYAML pandas_market_calendars pytest
pytest tests/test_user_strategy.py tests/test_user_strategy_market_data.py tests/test_user_strategy_lock.py -q
git rev-parse HEAD
python --version
python -m pip freeze | tee /srv/ibkr/logs/pip-freeze-$(date +%F).txt
```

部署时至少记录 Git commit、Python/vn.py/vnpy_ib/IB Gateway 版本和配置校验和。升级前先跑测试和 Paper，保留上一个可运行版本以便回滚。

### 3.2 Docker Compose（容器化策略，推荐固化依赖时使用）

仓库的 `Dockerfile` 与 `compose.yaml` 只容器化策略；**IB Gateway 不在容器中**，继续运行在宿主机。这保留 Gateway 的首次登录、二次验证和人工会话确认。Compose 使用 `network_mode: host`，故容器内的 `127.0.0.1:4002` 就是宿主机 Gateway；不要添加 `ports:` 映射，也不要将 Gateway 绑定到 Docker 网桥、私网或公网。

先按 Docker 官方 Ubuntu 安装说明安装 Docker Engine 和 Compose v2，再确认：

```bash
docker version
docker compose version
```

`docker` 组相当于宿主机 root 权限；个人使用时直接通过 `sudo docker` 执行即可。t3.small 内存有限：首次 `docker compose build` 在非交易时段执行，并保留第 2 节的 swap。记录镜像 ID，方便回滚。

以固定的 Git commit 检出代码后，创建运行期目录并设置持久化卷权限：

```bash
cd /opt/vnpy
sudo install -d -m 0700 runtime/config runtime/state runtime/logs
sudo cp runtime/config/paper.yaml.example runtime/config/paper.yaml
sudo chmod 600 runtime/config/paper.yaml
sudo chown -R 10001:10001 runtime/config runtime/state runtime/logs
```

编辑 `runtime/config/paper.yaml`，填入 Paper 账户与已批准的限额；它的 `state_path` 必须保持指向 `/runtime/state` 挂载卷。构建镜像后进行离线解析，并记录 image ID/digest：

```bash
sudo docker compose build
sudo docker compose run --rm --no-deps --entrypoint python paper-strategy \
  -c "from user_strategy.config import load_config; print(load_config('/runtime/config/paper.yaml'))"
sudo docker image inspect vnpy-paper-strategy:local --format '{{.Id}}'
```

Compose 已设置只读根文件系统、`/tmp` tmpfs、删除 Linux capabilities、`no-new-privileges` 和 `restart: "no"`；state 与日志是唯一可写的持久化路径。不要以覆盖 Compose 配置的方式恢复自动重启或增加特权。完整的容器运行、脱离终端和恢复说明见 [Paper 策略 Docker 部署](docker_paper_deployment.md)。

## 4. 安装与保护 IB Gateway（阶段 B）

优先用 IB Gateway 而不是 TWS。根据 IBKR 官方 [API 配置说明](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/)，默认端口为：

| 平台 | Paper | Live |
| --- | ---: | ---: |
| TWS | 7497 | 7496 |
| IB Gateway | 4002 | 4001 |

从 IBKR 官方渠道下载当前 Linux 版 IB Gateway，按其安装说明完成安装。首次登录、二次验证、每日重认证由账户授权人执行；不可尝试自动绕过。

在 Gateway 的 API Settings 中完成并在重启后复核：

1. 启用 socket API；取消 `Read-Only`（否则 API 下单会被拒绝）。
2. API 仅允许 `localhost` / `127.0.0.1`；如界面提供 `Allow localhost only`，启用它。IBKR 的 API 设置也明确支持 `allowLocalhostOnly` 与可信 IP 配置，见 [API 设置字段](https://www.interactivebrokers.com/docs/tws-api/protobuf/api-settings-config)。
3. Paper 阶段登录 Paper 账户、使用 `4002`；若使用 TWS 则为 `7497`。
4. 关闭断线后自动重投递/自动交易类选项，保留 API message log 供故障分析。

无桌面服务器仅为首次设置临时启动 Xvfb；不要暴露 VNC：

```bash
tmux new -s gateway-setup
export DISPLAY=:99
Xvfb :99 -screen 0 1280x800x24 &
<IB_GATEWAY_INSTALL_DIR>/ibgateway
```

确认端口只在环回地址监听：

```bash
ss -ltnp | grep -E ':(4001|4002|7496|7497)'
```

预期只出现 `127.0.0.1:<port>`；出现 `0.0.0.0`、私网 IP 或公网 IP 即停止并修正配置。

## 5. Paper 配置、启动与验收（阶段 B/C）

原生 Python 部署时，创建运行时配置，权限为运行账户独占。使用仓库的 [示例](../user_strategy/paper.example.yaml) 起步；账户号必须是 Paper 账户。Docker 部署则使用上一节创建的 `runtime/config/paper.yaml`，不要创建第二份同策略配置。

```bash
umask 077
nano /srv/ibkr/config/paper-us.yaml
chmod 600 /srv/ibkr/config/paper-us.yaml
```

```yaml
ibkr:
  environment: paper
  gateway_name: IB
  host: 127.0.0.1
  client_id: 31
  account: DU1234567
  port: 4002

strategy:
  strategy_id: us-paper-001
  capital: 100000
  state_path: /srv/ibkr/state/us-paper-001.json
  max_order_notional: 300
  max_active_orders: 1

basket:
  - symbol: AAPL
    exchange: SMART
    currency: USD
    market: US
    max_allocation: 650
    lot_size: 1
```

当前 `user_strategy.config` 会拒绝 `environment: live`、非 `7497/4002` 端口、空账户以及市场/币种不匹配。先做不连接券商的解析：

```bash
cd /opt/vnpy
. .venv/bin/activate
python -c "from user_strategy.config import load_config; print(load_config('/srv/ibkr/config/paper-us.yaml'))"
```

每日 Paper 启动流程：

1. 确认 Gateway 登录的是目标 Paper 账户，行情权限正常，且无无法解释的订单/仓位。
2. 检查资源和本机监听：

   ```bash
   free -h
   df -h / /srv
   ss -ltnp | grep ':4002'
   ```

3. 仅按所选部署方式启动一个策略；程序启动后应显示 `state=PAUSED`：

   ```bash
   # 原生 Python
   tmux new -s paper-us
   cd /opt/vnpy && . .venv/bin/activate
   python -m user_strategy.run_paper /srv/ibkr/config/paper-us.yaml \
     2>&1 | tee -a /srv/ibkr/logs/paper-us-$(date +%F).log
   ```

   ```bash
   # Docker：保持前台交互，以便你人工输入 start
   cd /opt/vnpy
   sudo docker compose run --rm paper-strategy
   ```

4. 确认账户白名单、唯一股票合约、参考价、实时行情、成交额、OMS 对账和 state 文件均正常。
5. 你人工输入 `start`；以 `pause` 停止新增仓位，以 `close_all` 执行平仓，以 `reset` 仅在已确认空仓后解除 `HALTED`。

Paper 验收至少持续 10 个正常交易日，并按 [Paper 故障演练](paper_runbook.md) 留存证据。必须成功演练：断线、重连后对账、重启、部分成交、拒单、撤单、账户错误、闭市/午休。每笔交易保留 `signal -> order -> trade -> position -> stop -> exit` 的日志与 Gateway 订单/成交/持仓导出。

## 6. 进入实盘前的工程门禁（阶段 D，必须验证）

仓库已具备受控 Live 启动路径，但严禁只把 YAML 改为 `live` 或把端口改成 `4001/7496`。必须完成相同 release 的测试和 Paper 回归，并按 [Live 解锁与运行手册](live_unlock_runbook.md) 留存证据：

1. **明确 Live 模式**：在配置模型中显式区分 Paper/Live，而非通过端口猜测；Live 必须要求第二个独立的、不会提交到 Git 的确认凭据或部署时批准标识。
2. **账户与合约白名单**：Live 账户、市场、币种、标的、交易所和 client ID 全部显式校验；HK 标的还应固定并校验 `conId`、`localSymbol` 与 `tradingClass`。
3. **连接健康事件**：把 Gateway 的断线和重连事件实际接到 `notify_gateway_disconnected()` / `notify_gateway_reconnected()`；断线立即暂停新增订单，重连必须完成 OMS 对账且保持 `PAUSED`。
4. **启动对账**：请求并等待账户、仓位、活动订单和合约快照到齐；任何非策略可解释的持仓、订单、空头或状态漂移都进入 `HALTED`，不得继续交易。
5. **风险与熔断**：增加经过测试的单笔金额、活动委托数、日损失和 kill switch 限制；策略内限额不是唯一防线。
6. **告警、备份与恢复**：至少通知到你常看的渠道（进程退出、断线、`HALTED`、磁盘不足、拒单、日损失）；备份 state/logs，并在 Paper 演练恢复。Docker 则记录并固定镜像 ID，不在运行中的容器内安装依赖。

验收标准是：离线单元测试覆盖 Live 拒绝/授权、错误账户、错误合约、断线、重连、未知订单、部分成交、保护单失败、熔断与重启；随后以相同 release 在 Paper 完整回归。没有完整证据，不设置 Live approval 环境变量。

## 7. 实盘部署与首日运行（阶段 E）

在完成第 6 节验收后执行。实盘账户应使用独立的配置、state、日志和 client ID；绝不复用 Paper 状态文件。当前 Compose 文件名为 `paper-strategy`、入口为 `run_paper`，不能原样用于 Live；容器化 Live 也需要独立的配置和固定镜像 ID。

```text
/srv/ibkr/config/live-us.yaml      # chmod 600，非 Git
/srv/ibkr/state/live-us-001.json   # 不与 Paper 共用
/srv/ibkr/logs/live-us-*           # 不与 Paper 混写
```

1. 创建 EBS 快照，记录候选 release 的 Git commit，并确认自己知道如何暂停和手动平仓。
2. 登录 **Live** Gateway。IB Gateway 的默认 Live socket 为 `4001`，TWS 为 `7496`；端口必须和 Gateway 设置匹配，且仍仅绑定 `127.0.0.1`。端口对应关系以 [IBKR 官方说明](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/) 为准。
3. 启动 Live 程序后，保持 `PAUSED`。逐项核对账户号、环境、合约、币种、限额、可用现金、行情新鲜度、活动订单=0、策略持仓=0、STOP 配置和告警。
4. 仅在你能全程看盘时人工 `start`。首日只允许最小名义金额、一个标的、一个活动入场订单；不进行参数优化、代码升级或增加标的。
5. 每次成交后立即核对 IBKR 订单、成交、持仓和保护 STOP 数量，及策略日志/状态是否一致。任何差异、拒单、无行情、断线或未知订单：执行 `pause`，转人工处理。
6. 收盘后 `pause`，确认无未预期订单/仓位，保存日志和订单/成交/持仓导出；隔天复核无误后再逐步提高额度或扩大标的池。

## 8. 日常运行、故障与回滚（阶段 F）

每日开盘前检查：内存/磁盘、Gateway 登录账户与 API、本机监听、行情权限，以及账户/订单/持仓是否为空或已解释。常用命令：

```bash
sudo -iu trader tmux ls
ps -u trader -o pid,etime,%mem,%cpu,cmd --sort=-%mem
tail -n 100 /srv/ibkr/logs/live-us-$(date +%F).log
df -h / /srv
free -h
ss -ltnp | grep -E ':(4001|4002|7496|7497)'
```

| 事件 | 立即动作 | 允许恢复的条件 |
| --- | --- | --- |
| Gateway/API 断线 | `pause`；不重发旧订单 | Gateway 恢复，订单/成交/仓位完成对账，人工 `start` |
| 不明订单、持仓、空头或 `HALTED` | 停止自动交易，人工接管 | Broker 与本地状态完全一致 |
| 行情过期/权限异常 | 保持 `PAUSED` | 实时行情、参考价、成交额与时段闸门验证通过 |
| 无保护 STOP 或数量不一致 | `pause`，人工核对订单 | 保护单已由 broker 确认且数量精确 |
| 内存/磁盘/CPU 异常 | `pause`，保留日志，必要时停策略 | 根因消除、容量恢复、Paper 回归适用时完成 |
| 凭据或 SSH 密钥疑似泄露 | 撤销/轮换凭据，收紧安全组 | 已确认只剩自己的有效访问方式 |

回滚指“停止新交易并恢复已验证 release”，不是盲目重启：`pause` -> 在 IBKR 中确认并人工管理订单/仓位 -> 保存日志与 state 副本 -> 停策略 -> 切回上一 release -> 在 Paper 验证 -> 仅获批准后再次进入 Live。任何仍有持仓或活动订单的场景都不能用删除 state、强制重启或第二个策略实例解决。

## 9. 实盘自检记录

每次 Live 首次上线或扩大额度前，写一份简短记录，方便日后回看：

```text
账户与环境：Live / <账户掩码>
候选 release（Git commit）：
IB Gateway、vn.py、vnpy_ib 版本：
Paper 验收证据位置：
允许标的、市场、币种：
单笔 / 单票 / 全策略 / 日损失限额：
交易时段：
告警渠道、紧急平仓步骤：
EBS 快照 / 回滚版本：
日期：
```

任何一项不清楚，就继续保持 Paper 或 `PAUSED`。
