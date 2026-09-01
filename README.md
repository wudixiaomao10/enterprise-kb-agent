# 企业知识库 Agent

面向企业内部文档的问答与研究系统：先按身份过滤权限，再检索资料，最后只返回有引用、可核验的答案。

它不是把文档整篇丢给大模型。浏览器登录后，后端用服务端身份目录决定「当前用户能看哪些资料」，在这个范围内做关键词 + 向量混合检索，生成结构化结论，再用 Citation Binder 和 Evidence Verifier 核对引用是否真实、是否仍有效、是否属于该用户权限。证据不足或无权访问时，系统会明确拒答，而不是编造内容。

完整实现说明见 [`docs/企业知识库Agent详细讲解.md`](docs/企业知识库Agent详细讲解.md)。

## 解决什么问题

企业内部通常同时存在产品手册、HR 制度和财务密件。销售可以问产品怎么部署，但不能通过「问得像一点」读到年假制度；HR 可以回答年假问题，并看到引用的是哪份文档、哪个版本、第几页。

系统同时处理六件事：

| 目标 | 做法 |
| --- | --- |
| 相关 | PostgreSQL 全文检索 + pgvector 语义召回，再经 Reranker 排序 |
| 权限 | 检索前用 ACL 过滤，部门/角色来自身份目录，不信任客户端自称 |
| 真实 | 每条结论必须绑定真实 Chunk，验证器检查支持度和覆盖度 |
| 时效 | 引用绑定到文档版本；过期版本不能充当有效证据 |
| 可靠 | 耗时任务进 Dramatiq 队列，失败进入 DLQ，可重放或丢弃 |
| 可观察 | OpenTelemetry 记录各阶段耗时与 token，不把密钥和全文打进指标 |

## 一次提问怎么走

快速问答走 `POST /chat/query`：

```text
登录拿到 JWT
  -> 校验签名 / issuer / audience / 有效期
  -> 用 issuer + subject 查服务端身份目录
  -> 得到 SubjectScope（用户、部门、角色）
  -> 只检索该范围内的 Chunk
  -> 混合检索 + 重排
  -> 生成 Claims
  -> Citation Binder 绑定引用
  -> Evidence Verifier 核验证据
  -> 返回答案，或返回拒答原因
```

复杂问题走 `POST /research/jobs`。API 先返回 `job_id`，Worker 用 LangGraph 执行：

```text
plan -> retrieve -> assess -> expand/retrieve -> synthesize
```

每一轮检索都带上当前用户的 ACL。最终输出复用与快速问答相同的引用绑定和证据验证。客户端轮询 `GET /research/jobs/{job_id}`。

文档进入知识库的路径是：上传 → 安全检查 → 对象存储 → 解析/OCR → 切块 → Embedding → 索引。身份同步可走飞书通讯录、Microsoft Graph 增量或 SCIM；运维侧用 Redis 作队列传输、PostgreSQL 存任务与 DLQ。

## 技术栈

- 后端：Python 3.12、FastAPI、Uvicorn
- 存储：PostgreSQL 16 + pgvector、可选 SQLite / 内存库
- 队列：Dramatiq + Redis
- 对象存储：MinIO / S3，或本地目录
- 解析：Docling + RapidOCR（PDF），pypdf / PyMuPDF 作辅助
- 研究图：LangGraph（checkpoint 可写入 PostgreSQL）
- 登录：本地开发 Token，或 Microsoft Entra ID（OIDC / MSAL）
- 前端：`backend/app/static`（MSAL Browser、Lucide，由 esbuild 打包）

## 仓库结构

```text
backend/app/api            FastAPI 路由与页面
backend/app/ingestion      解析、切块、索引
backend/app/retrieval      Embedding、混合检索、重排
backend/app/security       登录、ACL、上传安全
backend/app/agent          Claims、Citation Binder、Evidence Verifier
backend/app/research       LangGraph 多轮研究
backend/app/identity       身份目录、飞书、Graph、SCIM
backend/app/jobs           异步任务、重试、DLQ
backend/app/repositories   Memory / SQLite / PostgreSQL
backend/app/storage        本地或 S3/MinIO 对象存储
backend/app/static         浏览器前端
docs/                       教程、评测集、身份目录示例
migrations/                 Alembic 数据库迁移
scripts/                    冒烟测试、备份、同步与评测
tests/                      单元测试与前端 auth 测试
```

密钥只放在本地 `.env`，仓库里只有 `.env.example`。不要提交 `.env` 或 `.env.knowledge`。

## 快速开始

### 1. 安装

```powershell
conda env create -f environment.yml
conda activate enterprise-kb-agent
python -m pip install -r requirements.txt
npm install
copy .env.example .env
```

环境已存在时，激活后执行 `python -m pip install -r requirements.txt` 即可。在 `.env` 中填写 `OPENAI_API_KEY`；使用兼容接口时同时设置 `OPENAI_BASE_URL`。

### 2. 最小可运行模式（不依赖 Docker）

适合先看页面和接口，不接远程模型：

```powershell
$env:KNOWLEDGE_STORE="memory"
$env:KNOWLEDGE_EMBEDDING_PROVIDER="local"
$env:KNOWLEDGE_LLM_PROVIDER="none"
$env:KNOWLEDGE_RERANKER_PROVIDER="lexical"
$env:KNOWLEDGE_OBJECT_STORAGE="local"
$env:KNOWLEDGE_JOB_MODE="inline"
$env:KNOWLEDGE_AUTH_MODE="local"
$env:KNOWLEDGE_AUTH_ALLOW_DEV_TOKEN="1"
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8010
```

打开 http://127.0.0.1:8010/ ，健康检查为 http://127.0.0.1:8010/health 。本地开发可用 `POST /auth/dev-token` 签发 JWT。

### 3. 完整本地栈（PostgreSQL + Redis + MinIO）

```powershell
docker compose -f docker-compose.postgres.yml up -d
conda activate enterprise-kb-agent
python -m dramatiq backend.app.jobs.tasks --processes 1 --threads 2
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8010
```

默认端口：Postgres `5432`，Redis `6380`，MinIO S3 `9000`、控制台 `9001`。`.env.example` 里的 `KNOWLEDGE_STORE=postgres` 与上述地址一致。生产环境请关闭 `KNOWLEDGE_AUTH_ALLOW_DEV_TOKEN`，改用 OIDC，并把 JWT / MinIO / SCIM 密钥换成足够长的随机值。

改过 `backend/app/static` 下的源文件后：

```powershell
npm test
npm run build
```

仓库中的 `app.bundle.js` 是已打包的 MSAL 与 Lucide，生产 OIDC 把缓存放在每个标签页的 `sessionStorage`，过期前静默续期。

## 主要接口

| 用途 | 方法 |
| --- | --- |
| 快速问答 | `POST /chat/query` |
| 提交 / 查询 / 取消研究任务 | `POST /research/jobs`，`GET /research/jobs/{job_id}`，`POST /research/jobs/{job_id}/cancel` |
| 上传文档 | `POST /documents/upload`，`POST /documents/upload-file` |
| 当前用户 | `GET /auth/me` |
| 本地开发 Token | `POST /auth/dev-token` |
| 身份目录 | `GET /admin/directory`，`POST /admin/directory/sync` |
| 飞书同步与 Webhook | `POST /admin/directory/feishu/sync`，`POST /webhooks/feishu` |
| Graph 同步与 Webhook | `POST /admin/directory/graph/sync`，`POST /webhooks/microsoft-graph` |
| SCIM 2.0 | `http://127.0.0.1:8010/scim/v2`（使用 `KNOWLEDGE_SCIM_TOKEN`） |
| 死信队列 | `GET /admin/jobs/dead-letter`，replay / discard |
| 运行诊断 | `GET /admin/observability`，`GET /admin/embedding`，`GET /admin/pipeline` |

导入一份示例身份目录：

```powershell
python scripts/sync_identity_directory.py docs/identity-directory.example.json --dry-run
python scripts/sync_identity_directory.py docs/identity-directory.example.json
```

### 飞书通讯录身份源

飞书以新增 Provider 的方式接入，不会替换现有 Entra OIDC、Microsoft Graph 或 SCIM。飞书管理后台需要创建企业自建应用，授予通讯录用户/部门只读权限，并订阅 `contact.user.*_v3`、`contact.department.*_v3` 和 `contact.scope.updated_v3` 事件。

在 `.env.knowledge` 中填写 `.env.example` 的 `KNOWLEDGE_FEISHU_*` 参数后，执行首次全量同步：

```powershell
D:\Anaconda\envs\enterprise-kb-agent\python.exe scripts/sync_feishu_directory.py
```

`KNOWLEDGE_FEISHU_DEPARTMENT_ID_MAP` 把飞书 `open_department_id` 映射到文档 ACL 使用的稳定部门 ID。同步时会把用户的直接部门展开为完整祖先链，因此授权给上级部门的资料也对其子部门成员生效；无部门用户不会获得部门权限。

事件回调地址为 `https://你的域名/webhooks/feishu`。生产环境应设置 `KNOWLEDGE_JOB_MODE=dramatiq`，由 Webhook 验证 Verification Token、签名、时间窗口并解密 AES 消息后入队；另用 cron 或计划任务定时运行上述全量同步命令作为兜底。身份目录模式要求登录 Token 的 `issuer + subject` 与同步结果一致，默认 subject 是飞书 `open_id`。

## 测试与评测

```powershell
python -m unittest discover -s tests -p "test_*.py"
python scripts/run_security_eval.py
python scripts/run_retrieval_eval.py
python scripts/pipeline_smoke_test.py
```

安全评测覆盖 ACL 隔离、停用账号、文档 Prompt Injection、伪造文件类型、活动 PDF、宏 DOCX 和压缩包滥用。`--strict` 还要求配置病毒扫描命令；扫描拒绝或不可用的文件会进入隔离区，不会被索引。

## 生产前注意

- 显式执行迁移：`python -m alembic -c alembic.ini upgrade head`，然后设 `KNOWLEDGE_AUTO_MIGRATE=0`。
- 备份：`python scripts/backup_restore.py backup --output <目录>`；恢复必须加 `--confirm`。
- Redis 只是 Dramatiq 传输层；任务状态和 DLQ 在 PostgreSQL。故障时先恢复库和对象存储，再重放任务。
- 更换向量维度时先停 API，再运行 `python scripts/migrate_embedding_dimensions.py --confirm-clear-vectors`。

Graph 订阅需要可公网访问的 HTTPS Webhook，并定期执行 `python scripts/maintain_graph_subscriptions.py`。Nginx 示例在 `deploy/nginx/`。

飞书事件订阅同样需要公网 HTTPS。Nginx 示例已经放行精确路径 `/webhooks/feishu`；正式启用前应先完成域名备案、证书配置、飞书应用发布与通讯录权限审批。
