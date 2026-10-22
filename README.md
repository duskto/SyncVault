# SyncVault

**支持断点续传和历史版本恢复的文件备份工具。**

SyncVault 将本地目录中的文件备份到 HTTP 服务端。你可以预览变更、分块上传大文件、
查看历史版本，并在需要时恢复指定版本。每次成功备份都保留独立版本，本地删除文件不会删除远端历史。

客户端使用 Python、Typer 和 httpx；服务端使用 FastAPI、SQLAlchemy 和 PostgreSQL，
文件内容保存在本地持久卷中。适用于个人文档、项目资料和静态文件的自托管备份。

[快速开始](#快速开始) · [命令参考](#命令参考) · [Docker 部署](#docker-部署) · [协议说明](docs/protocol.md) · [测试与验证](#测试与验证)

## 核心功能

- **变更预览**：扫描目录，区分新增、修改和跳过的文件；支持全量 SHA-256 检查。
- **分块与续传**：上传中断后查询服务端已确认的块，只补传缺失部分；支持有界并发和有限重试。
- **历史版本**：修改文件后生成新版本，旧版本继续保留，可按版本 ID 恢复。
- **完整性校验**：上传时校验分块与整文件；恢复时先下载到临时文件，校验通过后再发布到目标路径。
- **冲突检测**：多个客户端基于同一版本提交时，只允许一个成功，保留冲突内容供人工处理。
- **崩溃恢复**：维护工具可接管中断的版本提交，并清理过期上传块和临时文件。

当前采用单向备份模式，不传播本地删除，不执行双向同步。

## 快速开始

需要 **Linux / POSIX、Python 3.12+ 和 uv**。项目默认使用 Python 3.13；
Windows 用户可在 WSL2 中运行，暂不支持原生 Windows。

### 1. 安装

```bash
git clone https://github.com/duskto/SyncVault.git
cd SyncVault
uv sync --locked
```

uv 会创建项目虚拟环境并按 `uv.lock` 安装依赖。后续命令通过 `uv run` 执行，无需手动激活环境。

### 2. 启动本地服务

下面使用 SQLite，适合本地体验和开发。使用 PostgreSQL 的部署方式见 [Docker 部署](#docker-部署)。

先生成一个随机令牌，并妥善保存，客户端需要使用同一令牌：

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

将生成的值替换到下面的 `YOUR_TOKEN`，然后启动服务：

```bash
export SYNCVAULT_TOKENS='{"YOUR_TOKEN":"personal"}'
export SYNCVAULT_DATABASE_URL='sqlite:///.syncvault-server/metadata.db'
export SYNCVAULT_STORAGE_ROOT='.syncvault-server/data'

uv run alembic upgrade head
uv run uvicorn syncvault.server.main:app --host 127.0.0.1 --port 8000
```

服务启动后，可访问健康检查 `http://127.0.0.1:8000/health` 和交互式 API 文档
`http://127.0.0.1:8000/docs`。

### 3. 备份目录

另开终端，进入项目目录，设置同一令牌。以下命令生成一组示例文件；也可以将
`./demo-documents` 替换为自己的备份目录，并跳过生成步骤。

```bash
export SYNCVAULT_TOKEN='YOUR_TOKEN'

uv run python -m scripts.generate_sample ./demo-documents --large-mib 64
uv run syncvault init ./demo-documents --server http://127.0.0.1:8000 --namespace personal
uv run syncvault plan ./demo-documents
uv run syncvault push ./demo-documents
uv run syncvault status ./demo-documents
```

`init` 在备份根目录创建 `.syncvault/`，保存配置、SQLite 清单和未完成上传的快照。
访问令牌只从环境变量读取，不写入该配置。示例生成器不会覆盖已有同名文件。

### 4. 查看历史并恢复

修改文件后再次备份，查看它的历史版本：

```bash
printf 'second version\n' >> ./demo-documents/hello.txt
uv run syncvault push ./demo-documents --full
uv run syncvault versions hello.txt --root ./demo-documents
```

从输出中选取旧版本的 `version_id`，替换下方的 `VERSION_ID`：

```bash
uv run syncvault restore hello.txt \
  --version VERSION_ID \
  --root ./demo-documents \
  --output ./restored/hello-old.txt

uv run syncvault verify ./demo-documents --full
```

恢复默认不覆盖已有目标；需要替换时显式添加 `--overwrite`。下载中断或哈希不匹配时，
原目标文件保持不变。恢复不会修改备份基线；将旧内容恢复到备份目录后再次上传，会产生新版本。

## 命令参考

所有命令均可通过 `uv run syncvault <command> --help` 查看参数。

| 命令 | 用途 |
| --- | --- |
| `init ROOT --server URL --namespace NAME` | 初始化备份目录并检查服务连接与认证 |
| `plan ROOT [--full]` | 预览新增、修改与跳过的文件 |
| `push ROOT [--full]` | 先续传未完成快照，再上传新增或修改的文件 |
| `status ROOT` | 查看本地清单和待完成的上传 |
| `versions PATH --root ROOT` | 查询文件的历史版本 |
| `restore PATH --version ID --output FILE --root ROOT` | 恢复指定版本 |
| `verify ROOT [--full]` | 核对本地文件、清单和远端版本元数据 |
| `pending list --root ROOT` | 查看活动、暂停及归档的上传任务 |
| `pending export PATH --output FILE --root ROOT` | 校验并导出待处理快照，不覆盖已有文件 |
| `pending cancel PATH --root ROOT` | 取消可取消的远端会话并暂停本地任务，保留快照 |
| `pending retry PATH --root ROOT` | 使用原快照重新排队，等待下一次 push |

`versions`、`restore` 和 `pending` 的路径相对于备份根目录；在已初始化目录或其子目录内运行时，可省略 `--root`。

**续传与并发。** 上传中断后，重新执行相同的 `push` 命令即可。默认块大小为 4 MiB，
并发数为 4，可用 `--chunk-size`（字节）和 `--concurrency`（1–32）调整：

```bash
uv run syncvault push ./demo-documents --chunk-size 4194304 --concurrency 4
```

每次处理一个文件，其分块共享并发上限。续传依赖 `.syncvault/` 中的原始快照；
请保留该目录。快照丢失或损坏时会停止续传，避免将新旧内容混入同一版本。

**快速检查与完整检查。** 默认通过文件大小和修改时间判断变化；`--full` 会重新计算扫描到的
普通文件的 SHA-256。`verify` 检查远端版本元数据，不下载全部远端内容；
添加 `--remote-content` 会逐个下载并计算命名空间中所有当前版本的哈希，包括本地已删除的文件，
流式处理且不保存下载副本。这会消耗相应下载流量，发现内容损坏或对象缺失时返回非零退出码。
历史版本仍可通过 `restore` 单独核验。

```bash
uv run syncvault verify ./demo-documents --full --remote-content
```

## 失败任务与版本冲突

默认 `push` 遇到错误即停止。若希望继续备份其他文件，可使用：

```bash
uv run syncvault push ./demo-documents --continue-on-error
uv run syncvault pending list --root ./demo-documents
```

该模式保留失败任务，并在输出中列出成功版本和失败原因；只要有文件失败，退出码仍为 1。
不会为同一个失败文件在本轮重新创建快照，目录扫描失败等全局错误仍会中止。

处理某个失败文件时，先导出快照，再取消任务。取消后，该文件会被后续 `push` 跳过，直到显式重试：

```bash
uv run syncvault pending export hello.txt --root ./demo-documents --output ./restored/pending-hello.txt
uv run syncvault pending cancel hello.txt --root ./demo-documents
```

取消操作需要连接服务端。若服务端已完成上传，会核对回执并更新本地清单；正在合并的会话拒绝取消。
初始化回执丢失时，客户端使用原幂等键查询会话，再取消它；查询不会创建新会话。
这组任务管理命令需要配套使用支持按幂等键查询的新版服务端。

修复网络或存储问题后，使用原快照和原基准重新排队：

```bash
uv run syncvault pending retry hello.txt --root ./demo-documents
uv run syncvault push ./demo-documents
```

若原因是版本冲突，先查看或恢复远端版本。在确认要以当前远端版本为基准提交保留的快照后，
显式传入该版本 ID：

```bash
uv run syncvault versions hello.txt --root ./demo-documents
uv run syncvault pending retry hello.txt --root ./demo-documents --base-version CURRENT_VERSION_ID
uv run syncvault push ./demo-documents
```

客户端不会自动修改基准；指定 ID 必须是远端当前版本。排队后若其他客户端再次提交，仍会返回冲突。
远端已有历史版本继续保留。若需要改用当前源文件（例如原快照丢失），显式添加 `--from-source`；
它会新建快照和会话，不向旧会话混入新内容。

每次重新排队会将原任务归档。`pending list` 输出 `archive_id`，可用
`pending export PATH --archive ARCHIVE_ID --output FILE --root ROOT` 导出归档快照。
归档快照不会被上传后的自动清理删除，需为它们保留磁盘空间；已丢失或损坏的快照不能通过归档恢复。

## Docker 部署

Compose 提供 API 和 PostgreSQL 17，分别使用对象卷和数据库卷持久化数据。

```bash
cp .env.example .env
# 编辑 .env，设置 POSTGRES_PASSWORD 和 SYNCVAULT_TOKENS
docker compose up --build -d

curl --fail http://127.0.0.1:8000/health
docker compose logs api
```

数据库密码应使用随机的 URL-safe 字符串；若使用其他特殊字符，需要正确编码数据库连接 URL。
`SYNCVAULT_TOKENS` 的格式为 `{"随机令牌":"用户标识"}`。客户端仍需单独设置
`SYNCVAULT_TOKEN`，不会自动读取 Compose 的 `.env`。

默认 API 只监听本机 8000 端口，数据库不暴露主机端口。远程访问应通过反向代理配置 HTTPS。
部署采用单 API 实例、单 worker 和本地持久卷，不支持跨主机共享存储或 NFS。
备份服务自身时，需要配套保存 PostgreSQL 元数据和对象卷。

### 代理下载

客户端支持 HTTP/SOCKS 代理环境变量。连接本机服务时，将 `localhost,127.0.0.1`
加入 `NO_PROXY`。Docker 构建需要代理时，先设置 shell 中的 `http_proxy`、`https_proxy`，再执行：

```bash
docker compose build \
  --build-arg HTTP_PROXY="$http_proxy" \
  --build-arg HTTPS_PROXY="$https_proxy"
docker compose up -d
```

代理地址必须能从构建容器访问，构建参数无需写入服务的运行环境。

### 服务端配置

| 环境变量 | 含义 | 默认值 |
| --- | --- | --- |
| `SYNCVAULT_TOKENS` | 访问令牌到用户标识的 JSON 映射 | 无可用令牌 |
| `SYNCVAULT_DATABASE_URL` | SQLAlchemy 数据库连接 URL | `sqlite:///.syncvault-server/metadata.db` |
| `SYNCVAULT_STORAGE_ROOT` | 分块和版本对象目录 | `.syncvault-server/data` |
| `SYNCVAULT_SESSION_TTL` | 上传会话有效期，秒 | `86400` |
| `SYNCVAULT_MAX_FILE_SIZE` | 最大文件大小，字节 | `107374182400`（100 GiB） |
| `SYNCVAULT_MAX_CHUNKS` | 单次上传最大块数 | `100000` |

Compose 已指定 PostgreSQL 连接和 `/data` 对象目录。文件大小同时受最大块数与所选块大小限制。
正常启动使用 Alembic 迁移；`SYNCVAULT_AUTO_CREATE=1` 仅用于临时开发环境。

## 维护与使用边界

定期运行维护命令，恢复中断的版本提交并清理上传残留：

```bash
docker compose exec api syncvault-maintain --cleanup --grace-seconds 3600

# 非 Docker 部署：先设置与服务端相同的环境变量
uv run syncvault-maintain --cleanup --grace-seconds 3600
```

维护任务跳过正在合并的会话，保留已发布版本及冲突、校验失败的对象。
版本、会话元数据和锁文件不会自动删除；目前没有历史版本保留期限或自动副本修复功能。

- **文件类型**：支持普通文件和空文件；跳过符号链接、特殊文件、`.syncvault` 和
  `.tmp/.part/.swp/~` 结尾项。不保存空目录、权限、ACL、硬链接关系或平台扩展属性。
- **路径规则**：仅接受相对路径，最多 1024 UTF-8 字节，每个分量最多 255 字节。
  拒绝路径穿越、Windows 保留名和非 NFC 名称；大小写及文件/目录冲突会报错。
- **输入一致性**：上传使用本地快照，但备份期间仍应避免修改源文件。
  活动数据库和多文件应用应先导出或创建应用一致的快照。
- **版本冲突**：通过 `pending export/cancel/retry` 显式处理，保留双方内容；不要删除状态目录来绕过冲突。
- **资源占用**：客户端需要容纳未完成及归档快照的额外磁盘空间；服务端合并期间同时保留块和完整对象。
  块缓冲随并发数与块大小增长，超大目录的扫描和路径冲突检查也会增加内存与处理开销。

目前未提供双向同步、文件内差分、全局去重、断点下载或账户空间配额。
崩溃恢复已覆盖进程退出和容器重启场景，尚未验证真实断电恢复。

## 测试与验证

```bash
uv run pytest
uv run ruff check .
```

默认测试使用临时 SQLite 数据库和对象目录。测试覆盖分块边界、幂等重试、权限隔离、
并发冲突、磁盘满、进程退出、快照损坏及恢复文件校验。GitHub Actions 配置了
Python 3.12/3.13 和 PostgreSQL 17 测试任务。

测试 PostgreSQL 时，设置 `SYNCVAULT_TEST_DATABASE_URL` 后运行 pytest。
**必须使用专用、可销毁的测试数据库，测试夹具会清空 SyncVault 表。**

服务启动并设置 `SYNCVAULT_TOKEN` 后，可执行端到端验收和性能基准：

```bash
uv run python -m scripts.smoke \
  --server http://127.0.0.1:8000 --output smoke-results.json

uv run python -m scripts.benchmark \
  --server http://127.0.0.1:8000 --size-mib 32 \
  --network-note '填写实际 CPU、磁盘、网络和缓存条件'
```

验收脚本会在一个块确认后强制退出上传子进程，再核对续传字节和恢复内容；还会演示两个客户端
的版本冲突、继续上传其他文件、快照导出、取消、显式选择基准后重试，以及远端内容校验。

基准比较 1/4/8 MiB 块与 1/2/4 并发，每组使用独立客户端进程，分别记录峰值 RSS。
若 API 运行在同一 Linux 主机，可添加 `--server-pid PID`，按 10 ms 间隔采样该进程每组的
基准 RSS 和峰值 RSS；它包含服务端已有内存，可能漏掉短时峰值，不代表增量内存。
不提供 PID 时，服务端内存字段为 null。这些脚本只生成合成数据，但会在远端留下测试版本。

- [验证报告](docs/validation.md)：测试环境、故障场景、性能数据与验证边界。
- [HTTP 验收记录](docs/http-smoke-20261022.json) · [HTTP 基准数据](docs/http-benchmark-20261022.json)。
- [HTTP 协议](docs/protocol.md) · [curl API 示例](docs/api-example.sh)。

## 项目结构

```text
syncvault/
  client/       CLI、扫描、快照、本地状态、上传与恢复
  server/       API、元数据、对象存储、版本提交与维护
  shared/       路径规则和协议模型
migrations/     Alembic 数据库迁移
tests/          单元、集成与故障测试
scripts/        示例数据、端到端验收与性能基准
docs/           协议说明与验证记录
```
