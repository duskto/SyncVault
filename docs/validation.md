# 第一版验证记录

日期：2026-10-21。已完成指南第一版的功能实现、本机测试和 Docker/HTTP 验收。第二版双向同步不在本次范围内。

## 环境与依赖

- 客户端：Linux / WSL2，Python 3.13.5，uv 0.12.11，uv 创建的 `.venv`。
- 服务端：Docker、Python 3.13.16、PostgreSQL 17.11，单 API 实例、单 worker、本地持久卷。
- `uv sync` 已安装依赖并生成真实 `uv.lock`；`uv lock --check --offline` 通过。
- 真实 CLI 首次运行发现 SOCKS 代理缺少依赖，已改用 `httpx[socks]`，重新锁定后验收通过。
- 初期沙箱网络和 Docker socket 限制已通过执行授权解决。WSL Docker 构建 DNS 另需临时
  `build.extra_hosts` 映射到主机解析出的 PyPI 地址；DNS 恢复后已移除此临时映射。
  最终依赖下载使用用户提供的代理作为 Docker 构建参数，未写入正式 Compose 或镜像环境。

## 已执行结果

| 验证 | 结果 |
| --- | --- |
| SQLite 完整测试 | 54 passed，20 subtests passed，1.96 秒 |
| PostgreSQL 专用测试库完整测试 | 54 passed，20 subtests passed，4.37 秒 |
| Ruff | 全部通过 |
| Alembic | SQLite 升级、检查、降级、重新升级通过；Docker PostgreSQL 检查无差异 |
| Docker Compose | 镜像构建成功，API 与 PostgreSQL 健康 |
| 最终镜像复核 | 代理下载依赖后重建成功；SOCKS 依赖、既有版本哈希、迁移检查通过 |
| API 示例 | `docs/api-example.sh` 连续执行两次，分别生成合法空文件版本 |
| 真实 CLI/HTTP | 空文件、修改产生两版、恢复旧版、删除保留历史、中断续传、全量校验通过 |
| 持久卷重启 | API/数据库同时重启后，12 MiB 版本仍可下载，大小和 SHA-256 一致 |
| 维护任务 | 容器内 cleanup 成功，保留已发布版本 |
| HTTP 基准 | 32 MiB 文件，1/4/8 MiB 块 × 1/2/4 并发，9 组完成 |

测试有一条 Starlette TestClient 对 httpx 的弃用提示，不影响断言通过；未来升级测试依赖时需处理。
GitHub Actions 已配置 Python 3.12/3.13 与 PostgreSQL 17 任务，尚未在远端运行；
本次主机测试解释器为 Python 3.13.5。

测试检查数据库与真实文件，覆盖字节边界、路径、快照、幂等、越权、过期与取消、
重复/乱序/缺失块、并发冲突，以及以下故障：

- 合并对象落盘后、数据库提交前子进程退出；恢复后仅一个版本。
- 磁盘满不发布版本，空间恢复后可继续；损坏块拒绝合并。
- 块响应或 complete 响应丢失时重试，块记录和版本不重复。
- pending 续传使用原快照；快照丢失拒绝混入新源文件；过期后重新初始化。
- 下载中断或校验失败保留原目标；清理过期块保留已发布对象。

## 真实中断、恢复与重启

原始报告：[http-smoke.json](http-smoke.json)。12 MiB 文件含 12 个不同内容的 1 MiB 块。
确认第一块后客户端强制退出（退出码 24）；重新启动查询服务端后只传剩余 11 块，
新增确认字节 **11534336**。恢复后 SHA-256：
`ded3055308dcb447b61ad0e04ec45060f57b6d8b1acdc722d612ac62ca9f31b5`。

同一验收通过 CLI 完成旧版本恢复、本地删除后历史保留及 `verify --full`。
同时重启 API 和数据库后，重新下载既有版本并核验大小与哈希。
测试 Compose 的随机端口在重启后被重新分配，初次探测旧端口失败；重新查询映射后验证通过。
正式 Compose 固定绑定本机 8000。

服务启动且 `SYNCVAULT_TOKEN` 已配置后，复现命令：

```bash
uv run python -m scripts.smoke --server http://localhost:8000 --output smoke-results.json
```

脚本只生成临时合成数据并终止自己的上传子进程；本地临时文件自动清理，远端历史保留。
在专用测试部署中重启容器后，可按报告的 `retained_version_id` 下载
`/api/v1/versions/{id}/content`，比较 `file_size` 与 `restored_sha256`。

## HTTP 性能实测

原始报告：[http-benchmark.json](http-benchmark.json)。WSL2 loopback 到 Docker 发布端口，
本地持久卷、热文件系统缓存，每组 32 MiB 合成数据。半数块确认后查询权威进度，再传剩余块。
计时包含初始化、两段上传、查询、完整校验与版本提交，不含扫描和本地快照创建。

| 块 MiB | 并发 | 完整用时 秒 | 续传至提交 秒 | 续传确认 MiB | 客户端累计峰值 KiB |
| --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1.592 | 0.764 | 16 | 40672 |
| 1 | 2 | 0.938 | 0.489 | 16 | 44128 |
| 1 | 4 | 0.575 | 0.308 | 16 | 49120 |
| 4 | 1 | 1.027 | 0.580 | 16 | 61792 |
| 4 | 2 | 0.533 | 0.310 | 16 | 71112 |
| 4 | 4 | 0.393 | 0.235 | 16 | 88008 |
| 8 | 1 | 0.839 | 0.474 | 16 | 103152 |
| 8 | 2 | 0.491 | 0.254 | 16 | 111776 |
| 8 | 4 | 0.492 | 0.284 | 16 | 119968 |

API 进程在 smoke 和整轮基准后的 VmHWM 为 **115256 KiB**。客户端 RSS 也是同一进程累计峰值，
不能解释为每组独立内存成本。服务端逐组峰值未采集，JSON 相应字段保留 null，另提供整轮累计值。
每组初始和续传各确认 16 MiB；这是文件净负载，不是网卡流量，不含 HTTP 开销及丢响应后的重复接收。
这是一轮本机测量，不能证明最佳参数或公网吞吐。

```bash
uv run python -m scripts.benchmark --server http://localhost:8000 \
  --size-mib 32 --network-note '填写实际 CPU/磁盘/网络与缓存条件' \
  --output benchmark-results.json
```

早期本地 16 MiB 切分/重组结果保留在 [local-baseline.json](local-baseline.json)，
不含网络与数据库，不作为端到端备份性能。

## 复现与边界

```bash
uv sync --locked
uv run ruff check .
uv run pytest -q
# 必须使用专用、可销毁测试库；夹具会清空 SyncVault 表
SYNCVAULT_TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost/syncvault_test' \
  uv run pytest -q
```

验收使用独立的 `syncvault-acceptance` Compose 项目和随机令牌，测试库与 API 元数据库分离。
仅使用合成数据，令牌未写入报告。启动和使用步骤见 [README](../README.md)。
验收结束后已停止并移除临时容器和网络，镜像及合成数据卷保留；当前未运行正式服务。

验证包括进程强制退出、故障注入和容器重启；没有验证真实断电、跨主机共享卷、NFS、
生产高负载或活动数据库应用一致性。第一版是 POSIX 单向文件备份，不传播删除，不实现双向同步，
也不是完整系统备份。资源上限与部署约束见 README。
