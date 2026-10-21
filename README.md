# SyncVault

按《文件同步与备份系统_开发指南》实现的 Python 单向备份项目：将本地普通文件备份到 HTTP
服务端，保留不可变历史版本，支持分块上传、断点续传、冲突检测和校验恢复。第一版不传播删除。

**当前交付状态：** 指南第一版的客户端、服务端、迁移和 Docker 部署已实现。
SQLite 和 PostgreSQL 分别通过 54 项测试；真实 HTTP 验收覆盖客户端强制退出后只补传缺块、
历史版本恢复及完整哈希校验，另完成了 9 组分块/并发基准。
环境、实测数据和验证边界见 [验证报告](docs/validation.md)，接口见 [协议说明](docs/protocol.md)。

## 环境与安装

使用 Python 3.12+、uv；此次已通过 `uv venv` 创建 `.venv`，实际解释器为 Python 3.13.5。
第一版客户端及服务端支持 Linux/POSIX，使用 flock、O_NOFOLLOW 和原子硬链接发布恢复文件；
Windows 不能直接运行本实现。用户文件名仍按可移植规则检查。

在允许联网的环境，从项目根目录执行：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
uv venv .venv --python 3.13     # 尚未创建虚拟环境时执行
uv sync --locked              # 按已提交的 uv.lock 安装依赖
uv lock --check
uv run pytest
```

项目已提供 uv 生成的 `uv.lock`；Docker 构建也使用该锁文件。
虚拟环境激活方式为 `source .venv/bin/activate`。客户端支持环境变量配置的 HTTP/SOCKS 代理；
连接本机服务时应将 `localhost,127.0.0.1` 加入 `NO_PROXY`。

## 本地服务端

SQLite 用于快速开发和测试；正式部署配置使用 PostgreSQL。令牌由环境变量提供，服务端没有默认令牌。
请将下例中的令牌替换为自己生成的随机长字符串。

```bash
export SYNCVAULT_TOKENS='{"replace-with-a-random-token":"personal"}'
export SYNCVAULT_DATABASE_URL='sqlite:///.syncvault-server/metadata.db'
export SYNCVAULT_STORAGE_ROOT='.syncvault-server/data'
uv run alembic upgrade head
uv run uvicorn syncvault.server.main:app --host 127.0.0.1 --port 8000
```

`GET /health` 检查数据库连接；`/docs` 提供 API 文档。不要对真实元数据目录使用测试夹具。
`SYNCVAULT_AUTO_CREATE=1` 仅供临时开发环境使用，正常部署应执行 Alembic 迁移。

## 客户端与演示

另开终端，从项目根目录执行。样例脚本只生成合成数据，若同名文件存在会拒绝覆盖。

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
export SYNCVAULT_TOKEN='replace-with-a-random-token'
uv run python scripts/generate_sample.py ./demo-documents --large-mib 64
uv run syncvault init ./demo-documents --server http://localhost:8000 --namespace personal
uv run syncvault plan ./demo-documents
uv run syncvault push ./demo-documents --concurrency 4
uv run syncvault status ./demo-documents

# 修改后再次备份，旧版本仍然保留
printf 'second version\n' >> ./demo-documents/hello.txt
uv run syncvault push ./demo-documents --full
uv run syncvault versions hello.txt --root ./demo-documents

# 将上一步输出中的旧 version_id 填入下方
uv run syncvault restore hello.txt --version '<version-id>' \
  --root ./demo-documents --output ./restored/hello-old.txt
uv run syncvault verify ./demo-documents --full
```

`versions` 和 `restore` 可通过 `--root` 定位配置；也可在已初始化目录或其子目录中运行。
恢复必须指定输出路径，默认不覆盖；只有显式 `--overwrite` 才会替换已有文件。下载中断或哈希失败时，
原目标不变。恢复不改变备份基线，恢复到备份目录后再次 push 会产生一个新版本。

上传大文件途中终止进程，再执行同一个 push 即可续传；客户端先读取服务端确认块，只发缺块。
Ctrl-C 时已有线程可能先完成当前请求；故障测试包含服务端落盘后进程退出的场景。
`--chunk-size` 单位为字节，默认 4194304；总并发默认 4，范围 1–32。每次只处理一个文件，
其块使用同一个有界线程池，不会为每个文件另建 P 个并发请求。

默认快速扫描使用 size 和 mtime，元信息相同不证明内容相同。`plan/push/verify --full` 会重新计算
所有扫描到的普通文件的 SHA-256。`verify` 比较本地内容、清单和远端版本元数据，不下载全部远端对象；
若需要核验某个服务端对象的真实字节，请按该不可变版本恢复并校验。

## 支持范围与数据语义

- 普通文件和零字节文件；自动跳过符号链接、特殊文件、`.syncvault` 以及 `.tmp/.part/.swp/~` 结尾项，
  plan/push 输出跳过原因。扫描失败会报错，不按缺失文件传播删除。
- 本地删除不删除远端文件或历史。不保存空目录、ACL、硬链接关系、权限或平台扩展属性。
- 逻辑路径为相对路径，最多 1024 UTF-8 字节，每个分量最多 255 字节；拒绝 `..`、绝对路径、NUL、
  Windows 保留名及非 NFC 名称。大小写和文件/目录冲突报错，不自动改名。
- 上传先复制稳定快照，比较复制前后的 inode、size、mtime、ctime，并校验整文件哈希。
  备份期间应用不应修改文件；普通复制不能保证活动数据库或多文件应用的一致性。
- pending 和快照放在根目录 `.syncvault`，成功后才原子更新 SQLite 清单并清理快照。
  恢复 pending 会重新校验快照，丢失或损坏则停止，不使用新源文件向旧会话续传。
- 两客户端使用同一基准并发提交，仅一个成功；冲突快照和服务端对象保留。没有自动强制覆盖、
  文本合并或双向同步。发生冲突时先用 status 找到快照并复制出来，查看/恢复远端版本后人工处理；
  可将选定内容备份到新的目录及命名空间。不要直接删除状态目录来绕过冲突。
- token 不写入客户端配置。HTTP 仅供本机演示；远程部署应在反向代理上配置 HTTPS。

## PostgreSQL 与 Docker Compose

从项目根目录执行（首次构建需要下载基础镜像和锁定依赖）：

```bash
cp .env.example .env
# 编辑 .env，设置随机的 POSTGRES_PASSWORD 和 SYNCVAULT_TOKENS
docker compose up --build -d
curl --fail http://localhost:8000/health
docker compose logs api
```

若下载依赖需要代理，在 shell 配置 `http_proxy`、`https_proxy` 后，将其传给构建阶段：

```bash
docker compose build --build-arg HTTP_PROXY="$http_proxy" --build-arg HTTPS_PROXY="$https_proxy"
docker compose up -d
```

代理地址应能从 Docker 构建容器访问；这些构建参数无需写进镜像或服务运行环境。

示例密码必须是 URL-safe 字符；若包含特殊字符，需要在数据库连接 URL 中正确编码。
API 只绑定本机 8000，数据库不暴露主机端口；两个命名卷分别保存 PostgreSQL 元数据和文件对象。
默认单 API 实例、单 worker。扩展到跨主机、多存储卷或 NFS 不在此版支持范围内。
备份服务自身时必须同时保存数据库和对象卷；只有其中一个无法完整恢复。

## 崩溃恢复与清理

```bash
uv run syncvault-maintain --cleanup --grace-seconds 3600
# Compose 部署
docker compose exec api syncvault-maintain --cleanup --grace-seconds 3600
```

维护任务先恢复 FINALIZING，再标记过期并清理已完成/过期/取消会话的块和无引用临时文件。
可通过 cron 定期执行。正在合并的会话会被互斥锁跳过；进程退出后可立即接管，无需猜测租约是否过期。
已完成版本对象和冲突/校验失败对象不自动删除。锁文件、版本、文件和会话元数据也不自动删除。
存储错误时排除磁盘空间/权限问题后再执行维护和 push；数据库不能先指向不完整对象。
对象缺失或损坏会报错，当前没有自动副本修复。

## 测试、基准与结构

```bash
uv run pytest
uv run ruff check .
export SYNCVAULT_TOKEN='replace-with-a-random-token'
uv run python -m scripts.smoke --server http://localhost:8000 --output smoke-results.json
uv run python -m scripts.benchmark --server http://localhost:8000 \
  --size-mib 32 --network-note 'loopback; record CPU/disk/network here'
```

HTTP 基准组合 1/4/8 MiB 块和 1/2/4 并发，记录用时、续传新增确认净负载、客户端进程累计峰值 RSS；
服务端峰值内存需要独立观测；本次报告另记录 API 进程的累计峰值。基准有意在半数块后重新查询进度，
并非真实断网或性能最优证明。smoke 则在一个块得到确认后强制退出客户端进程，重新启动并核对补传字节。
两个脚本只生成合成数据，分别在随机 `smoke-*` 和 `benchmark` 命名空间留下历史版本。
实测结果：[HTTP 验收](docs/http-smoke.json)、[HTTP 基准](docs/http-benchmark.json)。

已配置 GitHub Actions：Python 3.12/3.13 的 SQLite 测试及 PostgreSQL 17 测试。
本次验证在本机执行，尚未运行远端 CI。

PostgreSQL 集成验证可设置 `SYNCVAULT_TEST_DATABASE_URL` 后执行 pytest。**必须指向专用、可销毁的
测试数据库**，测试夹具会清空 SyncVault 表。默认测试使用临时 SQLite 和临时对象目录。

```text
syncvault/client/        CLI、扫描、计划、快照、SQLite、传输、恢复
syncvault/server/        FastAPI、ORM、块与版本服务、本地存储、维护
syncvault/shared/        路径规范、上传请求模型、协议错误
migrations/             Alembic 初始迁移
tests/unit/             字节、扫描、状态、HTTP 重试、恢复、CLI
tests/integration/      上传协议、认证、历史、并发与冲突
tests/faults/           丢响应、磁盘满、进程退出、续传、损坏检测
scripts/                合成数据、HTTP 基准、本地字节基准
docs/                   协议、API 示例、实际验证记录
```

默认最大文件 100 GiB、最多 100000 块、会话 24 小时，可用 `SYNCVAULT_MAX_FILE_SIZE`、
`SYNCVAULT_MAX_CHUNKS`、`SYNCVAULT_SESSION_TTL` 修改。客户端需要最多一份当前文件快照的额外磁盘，
服务端合并期间同时保留块和完整对象；块缓冲约 O(P×C)，列表和扫描仍使用内存。
新建文件时的服务端目录冲突检查扫描命名空间中的路径，超大清单需要后续索引优化。
未实现账户/空间配额、全局去重、文件内差分、断点下载、双向同步和完整断电验证。
