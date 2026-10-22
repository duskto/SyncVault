# HTTP 协议与一致性

所有业务接口使用 `Authorization: Bearer <token>`。`SYNCVAULT_TOKENS` 是 token → owner 的
JSON 映射；`/health` 不需要 token。未知或无权访问的资源统一返回 404。命名空间在 owner 内隔离。
可在 `/docs` 查看 FastAPI 的交互接口文档。

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| POST | `/api/v1/uploads` | 固定参数初始化，支持 `Idempotency-Key` |
| GET | `/api/v1/uploads?idempotency_key=KEY` | 按当前用户的幂等键找回会话，不创建或修改会话 |
| GET | `/api/v1/uploads/{id}` | 会话状态、服务端确认的块索引 |
| PUT | `/api/v1/uploads/{id}/chunks/{index}` | 原始二进制，`application/octet-stream` |
| POST | `/api/v1/uploads/{id}/complete` | 校验、合并、比较基准并提交版本 |
| DELETE | `/api/v1/uploads/{id}` | 取消 OPEN 会话，重复取消成功 |
| GET | `/api/v1/files?namespace=personal` | 已发布文件，可用 logical_path 精确过滤 |
| GET | `/api/v1/files/{id}/versions` | 历史版本 |
| GET | `/api/v1/versions/{id}/content` | 不可变版本内容，附带 SHA-256 和 ETag |

两个列表接口支持 `offset` 和 `limit`，默认及最大 limit 均为 1000。CLI 自动翻页。
初始化需要 namespace、logical_path、size_bytes、sha256、chunk_size，base_version_id 可为空。
新文件基准为空；已有文件基准必须是客户端上次确认的版本，禁止自动更新基准以绕开冲突。
size 上限默认 100 GiB，块大小 1 byte–64 MiB，最多 100000 块，默认会话有效期 24 小时。

状态流转：`OPEN → FINALIZING → COMPLETED`；另外有 `EXPIRED`、`CANCELLED`、`FAILED`。
查询时即时反映过期状态，维护任务将过期持久化。缺块 complete 保持 OPEN 并返回缺失索引。
空文件 total_chunks=0，仍须调用 complete 校验空内容 SHA-256。

块写唯一临时文件并流式算哈希，校验长度和可选 `X-Chunk-SHA256` 后发布。
OPEN 状态下，同索引同内容幂等成功，不同内容返回 409。完成后拒绝写块。
幂等初始化键在 owner 内唯一；复用键但参数不一致返回 409；已成功 complete 返回相同版本。
会话与幂等键元数据保留，不会因清理块而消失。

complete 用本地 POSIX flock 排除同会话的发布和合并，合并时不长期持有数据库事务。
对象固定为 `objects/<upload-id>`，先落盘再以数据库条件 UPDATE 比较 current_version_id。
只有基准一致才在同一事务中写入版本、更新当前指针、完成会话。并发不同会话只能一个提交成功。
冲突对象及快照保留，不自动覆盖或重试冲突。

客户端 `pending retry --base-version ID` 允许用户明确选择当前远端基准，旧任务及其快照进入本地
`upload_archive`，新任务使用新的幂等键。排队不更新 `file_manifest`，只有完整提交成功后才更新。
提交时仍执行基准比较，不提供强制绕过 CAS 的接口。`--from-source` 创建新快照；默认复用原快照。
`pending cancel` 取消 OPEN 会话后只暂停本地任务；COMPLETED 会话核验后补记本地清单，
FINALIZING 会话拒绝取消。所有修改本地任务的命令与 push 共用目录互斥锁。

`verify --remote-content` 使用现有不可变版本下载接口，流式检查命名空间内当前版本的大小和
SHA-256，不写本地文件，不包含历史版本。默认 verify 仍只检查远端元数据。

进程退出会释放 flock，维护任务可接管 FINALIZING；恢复时重新校验已有对象再提交。
锁文件不能删除，否则可能创建两个互不互斥的锁域。实现只面向同一 POSIX 主机上的持久卷，
不保证 NFS、跨主机锁或多个独立存储卷的正确性。虽然使用 fsync，尚未做断电持久性验证。

错误格式：

```json
{"error":{"code":"MISSING_CHUNKS","message":"仍有缺失块","missing_chunks":[1]}}
```

| 状态 | 常见含义 |
| --- | --- |
| 202 | 其他 complete 正在执行，带 Retry-After |
| 401 / 404 | 认证失败 / 不存在或不可见 |
| 409 | 幂等键、块、路径、版本或状态冲突；缺块 |
| 410 | OPEN 会话过期 |
| 413 / 415 / 422 | 文件过大 / 块类型错误 / 参数、块或整文件校验失败 |
| 503 / 507 | 存储暂不可用 / 磁盘满 |

客户端对网络异常、408、429、500、502、503、504 以及 complete 的 202 最多尝试 6 次。
遵守 Retry-After（秒数或 HTTP 日期）；超过 60 秒时退出并提示稍后重试，避免提前请求。
其他 4xx 不盲目重试。下载每次重试完整的固定版本，未实现断点下载。
