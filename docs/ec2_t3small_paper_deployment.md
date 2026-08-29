# EC2 t3.small 部署至 IBKR Paper 操作手册

本文将本仓库的 `user_strategy` 从一台全新的 Ubuntu EC2 `t3.small` 部署到 IBKR Paper Trading。范围止于 Paper；本仓库会拒绝 Live 环境，本文也不包含任何 Live 操作。

## 1. 目标架构与边界

```text
运营人员 SSH ──> EC2 (Ubuntu, t3.small)
                      ├─ IB Gateway / TWS（Paper 登录，API 仅监听本机）
                      └─ user_strategy（连接 127.0.0.1:4002 或 :7497）
                                      └─ IBKR Paper account
```

建议使用 **IB Gateway Paper**，其默认 API 端口为 `4002`；若使用 TWS Paper，则使用 `7497`。策略配置严格只接受这两个端口。不要把 API 端口开放到互联网：策略与 Gateway 同机时应始终使用 `127.0.0.1`。

`t3.small` 为 2 vCPU / 2 GiB 内存，能够运行单实例、少量标的的 Paper 策略，但 IB Gateway 的 Java 进程在内存紧张时可能不稳定。创建 2 GiB swap、只运行一个 Gateway 和一个策略实例，并持续监控内存。多策略、多账户或大量行情订阅请升配到至少 `t3.medium`。

## 2. AWS 创建与最小安全配置

1. 在目标区域创建 Ubuntu 24.04 LTS 的 `t3.small`，磁盘至少 30 GiB gp3；启用 EBS 加密、自动备份或快照策略。
2. 安全组入站只放行 TCP 22，**来源限定为运营人员的固定公网 IP/CIDR**。不要添加 `0.0.0.0/0`，不要暴露 4002/7497、VNC、Jupyter 或 Gateway 管理端口。
3. 绑定 Elastic IP（便于固定 SSH 来源规则），在 EC2 详情页记录实例 ID、区域、私网 IP 和 EBS 卷 ID。
4. 在 IAM 中使用最小权限的运维身份；策略程序不需要 AWS access key，因此不要把 access key 写入实例、YAML 或 shell 历史。
5. 在本地私钥权限正确后连接：

   ```bash
   ssh -i ~/.ssh/ibkr-paper-ec2.pem ubuntu@EC2_ELASTIC_IP
   ```

> 若公司通过 VPN 或堡垒机访问，安全组应只允许该出口地址；这是优先于“方便远程登录”的要求。

## 3. 操作系统初始化

以下命令在 SSH 会话中执行。把 `YOUR_TZ` 替换为实际交易/运维时区，例如 `America/New_York`；系统时区只影响日志显示，不改变交易所时区。

```bash
sudo apt update
sudo apt -y full-upgrade
sudo apt install -y ca-certificates curl git tmux htop jq unzip \\
  build-essential python3 python3-venv python3-pip python3-dev \\
  libgl1 libegl1 libxrender1 libxext6 libxtst6 libxi6 libxrandr2 \\
  fonts-dejavu-core xvfb
sudo timedatectl set-timezone YOUR_TZ
timedatectl status
```

创建 swap（仅首次）。执行前确认根分区至少有 3 GiB 空闲：

```bash
df -h /
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

创建非 root 运行账户和目录；以下假定代码目录为 `/opt/vnpy`、运行期数据为 `/srv/ibkr-paper`：

```bash
sudo adduser --disabled-password --gecos '' trader
sudo install -d -o trader -g trader -m 0750 /opt/vnpy /srv/ibkr-paper/{config,state,logs}
sudo -iu trader
```

后续所有 Gateway 与策略命令都使用 `trader`，不要使用 `root`。

## 4. 部署项目 Python 环境

若使用容器化策略服务，请改按 [Paper 策略 Docker 部署](docker_paper_deployment.md) 操作；IB Gateway 仍按本手册安装在宿主机。以下内容是非容器化部署方式。

仍以 `trader` 身份执行：

```bash
git clone <本仓库的只读 Git URL> /opt/vnpy
cd /opt/vnpy
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -e .
python -m pip install vnpy_ib ibapi PyYAML pandas_market_calendars pytest
pytest tests/test_user_strategy.py tests/test_user_strategy_market_data.py -q
```

若仓库有锁定依赖或内部包源，必须改用团队指定的版本/源。首次部署后记录：

```bash
git rev-parse HEAD
python --version
python -m pip freeze | tee /srv/ibkr-paper/logs/pip-freeze-$(date +%F).txt
```

不要在生产 Paper 主机上执行不受控的 `pip install -U`；升级应先在独立环境回归测试，并记录 commit 与依赖差异。

## 5. 安装、配置和验证 IB Gateway（Paper）

从 Interactive Brokers 官方下载 Linux 版 **IB Gateway**，并选择 Paper Trading 登录。安装包、版本和安装路径会随 IBKR 发布变化，按官方下载页的当前安装说明执行；不要从不明镜像下载。

在 Gateway 中完成以下设置，然后退出并重启一次验证设置已保存：

1. 用 Paper 账户登录，确认界面账户是 Paper 账户，不是 Live 账户。
2. 开启 API socket 客户端连接。
3. API 端口设为 `4002`（Paper Gateway）；若改用 Paper TWS 则设为 `7497`。
4. 仅允许本机连接：Trusted IP 仅填 `127.0.0.1`；关闭任何“允许外部/只读以外”例外配置。
5. 关闭 API 的 read-only 模式，否则 Paper 下单也会被拒绝。
6. 关闭自动重启后未经人工确认的交易行为；任何重连后策略仍会处于 `PAUSED`。

图形界面只用于首次登录与 API 设置。无桌面 EC2 可以用临时 Xvfb 会话启动：

```bash
tmux new -s gateway-setup
export DISPLAY=:99
Xvfb :99 -screen 0 1280x800x24 &
<IB_GATEWAY_INSTALL_DIR>/ibgateway
```

通过 SSH 本地转发查看首次设置界面（只允许本机浏览器访问），或按团队已审计的 Gateway 自动化流程运行。不要暴露 VNC 端口。首次登录如遇到二次验证、每日重认证或会话超时，须由账户授权人完成；这不能、也不应由策略自动绕过。

Gateway 正常运行后，在 EC2 上确认端口仅监听环回地址：

```bash
ss -ltnp | grep -E ':(4002|7497)'
```

输出应出现 `127.0.0.1:4002` 或 `127.0.0.1:7497`，不得出现 `0.0.0.0` 或 EC2 私网/公网地址。

## 6. 创建 Paper 策略配置

敏感账户号不可提交 Git。创建 `trader` 可读、其他用户不可读的 YAML：

```bash
umask 077
nano /srv/ibkr-paper/config/paper-us.yaml
chmod 600 /srv/ibkr-paper/config/paper-us.yaml
```

最小示例（仅示例值；`account` 必须替换为实际 **Paper** 账户号，标的和资金须经负责人批准）：

```yaml
ibkr:
  environment: paper
  gateway_name: IB
  account: DU1234567
  port: 4002

strategy:
  strategy_id: us-paper-001
  capital: 100000
  state_path: /srv/ibkr-paper/state/us-paper-001.json
  # 其余阈值使用代码安全默认值；生产启用前应明确复核。

basket:
  - symbol: AAPL
    exchange: SMART
    currency: USD
    market: US
    max_allocation: 10000
    lot_size: 1
```

约束：单进程只能有同一市场、同一计价币种的 basket；US 使用 USD，HK 使用 HKD。任何 `environment: live`、端口不是 `4002/7497`、空账户或市场/币种不匹配都会被配置校验拒绝。

先做配置解析而不连接券商：

```bash
cd /opt/vnpy
. .venv/bin/activate
python -c "from user_strategy.config import load_config; print(load_config('/srv/ibkr-paper/config/paper-us.yaml'))"
```

## 7. 启动到 Paper 的标准流程

每个交易日按顺序执行；未通过任一步即停止，不要尝试绕过保护。

1. SSH 登录，确认 EC2、Gateway 和 Paper 账户状态正常：

   ```bash
   free -h
   df -h /
   date
   ss -ltnp | grep ':4002'
   ```

2. 在 Gateway 中确认账户号为 Paper、行情权限正常、无未认领订单/持仓。检查并清理上一次异常的 `HALTED` 事项后才继续。
3. 启动策略专用 tmux 会话：

   ```bash
   sudo -iu trader
   tmux new -s paper-us
   cd /opt/vnpy
   . .venv/bin/activate
   python -m user_strategy.run_paper /srv/ibkr-paper/config/paper-us.yaml 2>&1 | tee -a /srv/ibkr-paper/logs/paper-us-$(date +%F).log
   ```

4. 等待并核对日志中的账户、合约、参考价、行情和 OMS 快照。没有有效行情、成交额未知/过期、处于闭市时段，均应保持 `PAUSED`。
5. **只有值班人员已核对上述项目后**，在策略终端手工输入 `start`。程序不会自行 start。
6. 用 `Ctrl-b d` 脱离 tmux，不要关闭会话。复连：`tmux attach -t paper-us`。

策略启动后的命令为 `start`、`pause`、`close_all`、`reset`、`quit`。`close_all` 是交易操作，使用前必须确认 Paper 当前订单与持仓；异常时优先 `pause`，再人工核对订单和持仓。

## 8. 守护、重启与日常检查

不要为策略进程设置“崩溃即自动启动并自动 start”的 systemd 服务。这会绕过人工启动门禁。可以为 Gateway 设置受控的开机启动，但其登录/二次验证和每日会话恢复仍需人工检查。

每个交易日记录：策略日志、配置文件的校验和、Gateway 版本、Git commit、Paper 的订单/成交/持仓截图或导出。建议运行至少 10 个正常交易日，并完成仓库的 [Paper 故障演练](paper_runbook.md)。

基础巡检命令：

```bash
sudo -iu trader tmux ls
ps -u trader -o pid,etime,%mem,%cpu,cmd --sort=-%mem
tail -n 100 /srv/ibkr-paper/logs/paper-us-$(date +%F).log
df -h / /srv
free -h
```

重启顺序：先 `pause` 策略，确认无挂单/持仓或已经人工接管；退出策略；退出 Gateway；依次启动 Gateway 和策略；完成 OMS 对账后仍保持 `PAUSED`，由值班人员重新输入 `start`。

## 9. 故障处置与退出标准

| 情形 | 立即动作 | 恢复条件 |
| --- | --- | --- |
| Gateway/API 断线 | `pause`，不重发旧订单 | 重连、OMS 对账、人工 `start` |
| 不明订单、持仓或状态为 `HALTED` | 停止策略交易，人工在 Paper Gateway 核对 | 订单/成交/持仓与策略状态一致 |
| 无行情或成交额延迟 | 保持 `PAUSED` | 行情、参考价和成交额均恢复可验证 |
| 内存持续不足/OOM | `pause`，保留日志与状态，必要时停 Gateway | 找出原因、增加内存/换实例、完成回归验证 |
| SSH 密钥或账户疑似泄露 | 立即撤销/轮换密钥、收紧安全组、审计登录 | 安全负责人确认后恢复 |

Paper 通过的最低标准是连续 10 个正常交易日，以及 `docs/paper_runbook.md` 中所有故障演练有完整证据。达到该标准也**不等于**获准 Live；Live 需要新的、明确的书面授权和独立的风险审查。

## 10. 禁止事项清单

- 不在 YAML、Git、shell 历史或日志中写入账户密码、二次验证代码或私钥。
- 不对公网开放 4002、7497、VNC 或 Gateway 管理端口。
- 不把 `environment` 改为 `live`，不通过修改端口、环境变量或 monkey patch 绕过 Paper 限制。
- 不在 Gateway/策略断线或未完成 OMS 对账时输入 `start`。
- 不运行多个使用同一账户、同一策略 ID 或同一 state 文件的策略实例。
