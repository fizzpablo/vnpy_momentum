# Paper 策略 Docker 部署

本方案只容器化 `user_strategy`；IB Gateway/TWS 保持运行在 **Linux 宿主机**。这样保留其首次登录和二次验证的人工流程，同时把策略代码与 Python 依赖固化成镜像。策略仍然只接受 `environment: paper` 和端口 `4002`/`7497`。

## 前置条件

1. 使用 Linux 主机和 Docker Engine（本 Compose 文件使用 `network_mode: host`）。Docker Desktop、macOS 和 Windows 不适用此网络方式。
2. 在宿主机配置并启动 IB Gateway Paper，使 API **仅监听** `127.0.0.1:4002`（或 TWS Paper 的 `127.0.0.1:7497`）。不得向公网、Docker 网桥或私网开放该端口。
3. 安装 Docker Compose v2，并在项目根目录执行以下命令。

## 首次部署

创建运行期目录。运行账户需能写入 `state` 和 `logs`；下面示例假定容器用户 UID 为 `10001`：

```bash
mkdir -p runtime/config runtime/state runtime/logs
cp runtime/config/paper.yaml.example runtime/config/paper.yaml
chmod 600 runtime/config/paper.yaml
sudo chown -R 10001:10001 runtime/config runtime/state runtime/logs
```

镜像内策略以 UID `10001` 运行，故 `config` 也必须归该 UID 所有；文件仍为 `0600`，且容器将其只读挂载。编辑 `runtime/config/paper.yaml`，填入实际 Paper 账户号、核准的标的与资金上限。配置中的状态路径已指向独立挂载卷 `/runtime/state`，不要改为容器内路径。

构建镜像并先校验配置（该命令不连接 Gateway）：

```bash
docker compose build
docker compose run --rm --no-deps --entrypoint python paper-strategy -c "from user_strategy.config import load_config; print(load_config('/runtime/config/paper.yaml'))"
```

确认 Gateway 正在宿主机环回地址监听：

```bash
ss -ltnp | grep -E '127.0.0.1:(4002|7497)'
```

## 运行与停止

在前台启动，以便值班人员完成核对后手工输入 `start`。容器启动后默认仍是 `PAUSED`，绝不会自动开始交易：

```bash
docker compose run --rm paper-strategy
```

输入 `pause`、`close_all`、`reset`、`quit` 与原运行方式一致。`close_all` 是交易操作，先人工确认 Paper 订单与持仓。

如需后台保持交互会话，使用：

```bash
docker compose up -d
docker attach vnpy-paper-strategy
```

按 `Ctrl-p`、`Ctrl-q` 脱离而不停止容器；停止时先在策略内 `pause` 并完成 OMS 对账，再执行 `docker compose stop`。Compose 明确设置 `restart: "no"`，以免崩溃或主机重启绕过人工 `start` 门禁。

## 迁移与恢复

迁移时重建镜像，并安全复制 `runtime/config`、`runtime/state`、`runtime/logs`；配置文件含账户号，应走受控的密钥/文件传输渠道。新主机必须重新完成 Gateway Paper 登录与 API 的本机监听检查。恢复状态后，保持策略 `PAUSED`，核对 Gateway 中的订单、成交和持仓后才可手工 `start`。

`runtime/state` 是关键持久化数据，建议纳入加密备份；日志保留周期可按运维要求管理。镜像版本应使用 Git commit 或镜像 digest 记录，而不是长期依赖 `latest` 标签。
