# 统一多容器编排与首次设置

状态：**检查点 2 已实现**。浏览器上传和 Worker 业务任务仍按后续检查点推进。

本文记录 PrvAptMirror 第二阶段已经落地的部署体验。系统没有把所有进程塞进一个容器，而是把多个安全边界封装成一个 Compose 项目、一个数据根目录和一套统一命令，让日常使用者不需要理解每个内部容器。

## 1. 实现原则

- 保留 `repo-web`、`admin-web` 和 `repo-worker` 的独立容器；
- 增加一次性、幂等的 `bootstrap` 初始化容器；
- 用户只操作一个 Compose 项目和 `./scripts/prvaptmirror` 入口；
- 浏览器和 APT 客户端只使用一个 Web 域名，管理页面固定在 `/admin/`；
- 所有 PrvAptMirror 持久化数据位于 `.env` 指定的单一目录；
- `.env` 只保存端口、域名、路径、密钥身份等配置，不保存密码、会话令牌或签名私钥；
- 首次启动不设置默认管理员密码，管理员在浏览器首次设置页面中创建密码；
- 首次设置必须验证自动生成的一次性设置令牌，不能采用“第一个访问者成为管理员”的不安全模式；
- 仓库 OpenPGP 签名密钥由无网络初始化流程自动生成，或复用数据目录中已有的密钥环；
- 不使用 SSH 密钥代替 APT 仓库的 OpenPGP 签名密钥。

## 2. 用户体验目标

首次部署流程为：

```text
复制 .env.example 为 .env
        │
        ▼
修改数据目录、域名和签名身份
        │
        ▼
./scripts/prvaptmirror up
        │
        ├─ bootstrap 创建目录、设置权限、生成设置令牌和 GPG 密钥
        └─ 启动 admin-web、repo-worker、repo-web
        │
        ▼
./scripts/prvaptmirror setup
        │ 显示一次性设置令牌和访问方式，不显示或生成默认密码
        ▼
浏览器首次设置页面：输入令牌并创建管理员密码
        │
        ▼
正常登录并使用管理页面
```

首次设置完成后，`setup` 命令只报告“已完成初始化”，首次设置端点返回 `404` 或 `410`，一次性令牌立即失效并从磁盘删除。

日常操作收敛为：

```bash
./scripts/prvaptmirror up
./scripts/prvaptmirror status
./scripts/prvaptmirror logs
./scripts/prvaptmirror stop
```

现有 `repoctl` 命令继续保留，作为故障恢复和高级运维接口，不作为日常 Web 操作的必经步骤。

## 3. 单一数据根目录

`.env` 使用一个明确的绝对路径或项目内相对路径：

```dotenv
PRVAPTMIRROR_DATA_DIR=./data
```

根目录 `compose.yaml` 使相对路径自然以项目根目录为基准解析。

目录结构为：

```text
data/
├── admin/
│   ├── auth/
│   │   ├── password-hash
│   │   └── setup-token            # 仅首次设置前存在
│   ├── uploads/
│   ├── jobs/
│   └── audit/
├── aptly/
├── gnupg/
├── incoming/
├── public/
└── state/
    └── bootstrap-version
```

约束如下：

- 项目不在宿主机 `/etc`、`/usr`、`/var/lib` 或用户主目录创建应用数据；
- Compose 的所有宿主机绑定挂载都必须位于 `PRVAPTMIRROR_DATA_DIR`；
- 日志默认输出到容器标准输出；需要文件审计的内容只写入 `data/admin/audit`；
- 数据目录整体可迁移，但备份时仍必须单独加密保护 `gnupg` 和 `admin/auth`；
- Docker Engine 自身仍会在 Docker 数据根目录保存镜像、容器元数据和日志，这不属于应用可以重定向的数据；
- TLS、DNS 和宿主机现有反向代理配置仍由外部部署环境负责，不由项目写入系统目录。

## 4. `.env` 的边界

配置示例：

```dotenv
PRVAPTMIRROR_DATA_DIR=./data

REPO_HTTP_BIND=127.0.0.1
REPO_HTTP_PORT=28080
ADMIN_PUBLIC_ORIGIN=https://apt.example.com

ADMIN_USERNAME=admin
ADMIN_SESSION_TTL=28800

REPO_GPG_NAME=PrvAptMirror Archive
REPO_GPG_EMAIL=apt@example.com
REPO_GPG_EXPIRE=2y
REPO_GPG_MODE=generate
```

以下内容禁止直接写入 `.env`：

- 管理员明文密码或 Argon2id 哈希；
- 一次性设置令牌；
- 服务端会话令牌或未来使用的持久会话密钥；
- ASCII Armor 或二进制形式的 GPG 私钥；
- SSH 私钥。

原因是环境变量和 Compose 展开结果可能被 `docker inspect`、进程信息、诊断输出或自动化日志读取。`.env` 只决定“如何生成、保存到哪里和使用什么身份”，秘密本身保存在数据目录的受限文件中。

## 5. 首次 Web 设置密码

### 5.1 初始化状态

`bootstrap` 检查 `data/admin/auth/password-hash`：

- 已存在有效 Argon2id 哈希：视为已完成管理员初始化，不生成设置令牌；
- 尚不存在：使用系统 CSPRNG 生成至少 256 bit 的随机设置令牌，写入权限受限的 `setup-token`；
- 重复启动不得覆盖现有密码哈希，也不得无故轮换仍有效的设置令牌；
- 令牌文件和密码哈希必须由 `admin-web` 的固定 UID/GID 管理，避免依赖 Compose 本地 Secret 对 `uid/gid/mode` 的兼容性。

### 5.2 获取设置令牌

设置令牌不写入 `.env`，不进入普通容器日志。运维人员通过 SSH 在项目目录执行：

```bash
./scripts/prvaptmirror setup
```

该命令只读取 `admin/auth` 中的临时令牌并提示管理地址。令牌不会作为 URL 查询参数输出，避免进入浏览器历史、反向代理访问日志和 Referer。

### 5.3 浏览器设置流程

- 未初始化时，`admin-web` 只开放健康检查和首次设置页面，其他管理路径不可访问；
- 页面要求输入一次性设置令牌、管理员密码和确认密码；
- 设置请求必须校验 Origin、一次性 CSRF Token、请求体大小和令牌尝试次数；
- 密码至少 14 个字符，服务端使用 Argon2id 保存哈希，不保存明文；
- 写入使用临时文件、`fsync` 和原子重命名，目标权限为 `0400` 或 `0600`；
- 成功后删除设置令牌、记录审计事件，并跳转到正式登录页；
- 设置令牌连续失败达到上限后进行限速，但不能自动删除令牌造成远程拒绝服务；
- 已初始化后再次访问设置端点不能修改密码。

首次设置推荐通过已配置 TLS 的单一站点 `/admin/setup` 完成。尚未配置反向代理时，可以通过 SSH 本地端口转发访问仅绑定回环地址的临时入口；该模式只允许完成设置，不直接签发可在明文 HTTP 上使用的长期管理会话。

密码轮换不与首次设置同时实现。后续可以在登录后的安全页面中要求当前密码、CSRF 和二次确认后轮换，并使全部现有会话失效。

## 6. 仓库签名密钥初始化

`REPO_GPG_MODE=generate` 为默认模式：

- `bootstrap` 或无网络初始化任务复用现有 `repoctl init` 逻辑；
- 随机数来自容器内操作系统 CSPRNG，使用 GnuPG 生成 OpenPGP 签名密钥；
- 密钥身份和有效期来自 `.env`，私钥只保存到 `data/gnupg`；
- 自动生成前必须拒绝空身份和示例中的 `.invalid` 邮箱，避免生成后才发现永久签名身份错误；
- 公钥自动导出到 `data/public/repository-key.gpg` 和 `.asc`；
- 如果密钥环已有可用私钥，初始化必须幂等，不创建第二把密钥；
- 无人值守签名默认不使用交互式私钥口令，因此必须依靠文件权限、容器隔离和加密离线备份保护私钥。

本阶段不在 Web 页面提供签名私钥生成参数、私钥导入或私钥下载。导入已有密钥作为后续独立运维能力设计，不能通过 `.env` 传递私钥内容。

## 7. 多容器组成与权限

```text
                         单一 Compose 项目

bootstrap（一次性、无网络）
   ├─ 创建 data/ 目录与权限
   ├─ 创建首次设置令牌
   └─ 初始化或复用 GPG 密钥

浏览器与 APT 客户端 ─HTTPS─► repo-web（唯一入口）
                               │ /admin/
                               ▼
                            admin-web ─────► uploads / jobs / auth / audit
                                      │
                                      ▼
                              repo-worker（无网络、串行）
                                 │       │        │
                                 ▼       ▼        ▼
                               aptly   gnupg    public
                                                   │只读
APT 文件 ◄────────────── data/public（只读）
```

| 服务 | 生命周期 | 网络 | 可写目录 | 明确禁止 |
| --- | --- | --- | --- | --- |
| `bootstrap` | 启动前一次性执行 | 无网络 | 首次目录、`admin/auth`、`gnupg`、`public` | 对外端口、Docker Socket |
| `admin-web` | 常驻 | 仅 Compose 内部，由 `/admin/` 转发 | `admin/auth`、`uploads`、`jobs`、`audit` | GPG 私钥、Aptly 数据、公开仓库写权限、Docker Socket |
| `repo-worker` | 常驻串行 | `network_mode: none` | `jobs`、Aptly、GPG、公开仓库、审计 | 对外端口、任意网络、Docker Socket |
| `repo-web` | 常驻 | 唯一 Web 入口 | 无 | 管理数据、上传、Aptly、GPG 私钥 |

`admin-web` 对 `admin/auth` 的写权限仅用于首次设置的原子落盘和未来明确设计的密码轮换。即使管理服务被攻破，它仍不能直接读取签名私钥或写 Aptly/公开仓库。

## 8. Compose 行为

- `bootstrap` 必须幂等，正常完成后退出 `0`；
- 常驻服务通过 `depends_on.condition: service_completed_successfully` 等待初始化；
- 默认 `docker compose up -d` 启动完整栈，不再要求用户理解 `admin`、`tools` 等 profile；
- 一次性调试和测试服务可以继续使用 profile，但不能影响正常启动；
- 所有常驻容器使用固定非 root UID、只读根文件系统、`no-new-privileges` 和最小挂载；
- 服务停止只停止容器，不删除数据；清理数据必须使用单独、显式且带确认的命令；
- 升级前检查数据布局版本，迁移必须可备份、可重试，不能在失败时留下半迁移状态。

## 9. 已实现范围

检查点 2 已完成：

1. 已引入 `PRVAPTMIRROR_DATA_DIR` 并把全部应用绑定挂载收敛到该目录；
2. 已增加幂等 `bootstrap` 服务和数据布局版本；
3. 已自动初始化或复用 GPG 密钥并导出公钥；
4. 已实现一次性设置令牌和浏览器首次密码设置页面；
5. 已把正常启动收敛为完整多容器 Compose 编排；
6. 已统一 `up`、`stop`、`status`、`logs`、`setup` 和高级 `repoctl` 命令；
7. 已为重复初始化、令牌攻击、密码原子写入、容器挂载和无网络 Worker 增加测试。

该批次不包含：

- DEB 浏览器上传、预检和删除；
- 仓库创建、发布和回滚的 Web 操作；
- 多用户、注册、找回密码、OAuth/OIDC；
- GPG 私钥 Web 导入、导出或轮换；
- 自动签发 TLS、修改宿主机 Nginx、DNS 或防火墙；
- 自动备份到外部存储。

## 10. 验收标准

- 新机器只需项目目录、Docker、Compose 和一份 `.env` 即可启动；
- 除选定的数据根目录外，项目不向其他宿主机目录写应用数据；
- 首次启动没有默认密码，未持有设置令牌的访问者不能抢先创建管理员；
- 设置成功后磁盘上只保留 Argon2id 哈希，不保留设置令牌或管理员明文密码；
- `.env`、Compose 配置、镜像历史和普通日志中不存在密码或私钥；
- GPG 初始化可重复执行且不会覆盖已有密钥；
- `admin-web` 无法读取 GPG 私钥或 Aptly 数据，`repo-web` 只能读取公开目录；
- `repo-worker` 无网络且没有对外端口；
- `docker compose up -d`、停止、重启和升级不会破坏已有仓库或管理员认证状态；
- 现有签名、多架构、更新和回滚冒烟测试继续通过，并增加首次设置端到端测试。

## 11. 当前实现决策

当前实现采用以下决策：

1. 新部署默认数据目录为项目根目录下的 `./data`，也允许在 `.env` 中指定任意绝对目录；
2. 首次设置令牌由系统自动生成，通过 SSH 执行 `./scripts/prvaptmirror setup` 查看，再粘贴到浏览器表单；
3. GPG 默认沿用当前 `rsa3072`、仅签名、无交互口令和 `.env` 指定有效期的方案；
4. 正常 `up` 默认启动完整栈，不再要求为管理端启用 profile；
5. 项目直接使用新的 `data/` 布局，不保留开发期旧布局兼容代码；
6. 检查点 2 止于统一部署和首次设置，不同时实现上传、Worker 业务任务、发布或回滚页面。
