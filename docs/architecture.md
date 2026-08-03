# 系统架构

## 项目目标

PrvAptMirror 用于向 Debian 系操作系统发布由管理员提供的软件包，支持多发行版、多 CPU 架构、GPG 签名、不可变快照和原子回滚。系统可以在不同 VPS 之间迁移，放在现有反向代理之后，并在不替换 Aptly 仓库引擎的情况下逐步增加 Web 管理、自动获取和构建能力。

第一阶段的命令行仓库基础已经完成。当前第二阶段增加单管理员 Web 管理，并把部署体验收敛为一个 Compose 项目、一个数据根目录和一套统一命令，同时保持公开下载、管理入口和签名执行三个权限域相互隔离。详细部署范围参见 [统一多容器编排与首次设置](unified-deployment.md)。

## 当前阶段架构

```text
bootstrap（一次性、无网络）
  ├─ 创建数据目录与权限
  ├─ 创建首次设置令牌
  └─ 初始化或复用 GPG 密钥

管理员浏览器
  │ HTTPS + 首次设置/密码登录
  ▼
管理反向代理
  │
  ▼
admin-web ─────► 上传目录与结构化任务目录
                           │
                           ▼
                    repo-worker
                    （无网络、串行）
                      │        │
                      ▼        ▼
                  Aptly 数据  GPG 私钥
                      │
                      ▼
                  data/public
                      │ 只读
                      ▼
APT 客户端 ─HTTPS─► 公开反向代理 ─► repo-web
```

外部 DNS、TLS 证书和反向代理由宿主机负责。公开仓库与管理入口应使用不同域名或清晰隔离的路由；公开仓库继续保持匿名只读，管理入口的所有路径都必须先通过身份认证。

全部应用持久化目录从 `.env` 中的 `PRVAPTMIRROR_DATA_DIR` 派生。项目不向其他宿主机系统目录写应用状态；Docker Engine 自身的镜像和容器元数据以及外部反向代理配置不属于该数据根目录。

## 组件

### `bootstrap`（第二阶段）

`bootstrap` 是每次完整栈启动前执行的幂等一次性容器，不暴露端口且不访问网络。它创建数据目录、校验布局版本和权限，在尚未设置管理员时创建一次性设置令牌，并初始化或复用仓库 OpenPGP 签名密钥。

它不能覆盖已有管理员密码哈希或已有签名密钥。完成后退出，常驻服务只有在其成功时才启动。

### `repoctl`

`repoctl` 内含 Aptly、GnuPG、软件包检查工具和仓库管理封装。现有命令接口为：

```text
repoctl init
repoctl repo create <family> <suite>
repoctl package add <family> <suite> <deb-path>
repoctl package list <family> <suite>
repoctl snapshot list <family> <suite>
repoctl rollback <family> <suite> [snapshot]
repoctl key export
```

第一阶段由 `docker compose run --rm repoctl ...` 按需运行。第二阶段保留这些命令作为运维和恢复接口，同时由 `repo-worker` 复用相同校验与发布逻辑。Web 层不能传入任意命令或任意参数。

`package add` 检查 DEB 元数据和架构，导入本地仓库，创建带时间戳的不可变快照，签名并原子切换公开发行版。验证或发布失败时，客户端继续使用上一个有效快照。所有写操作共用文件锁。

### `admin-web`（第二阶段）

`admin-web` 提供首次密码设置、单管理员登录、管理页面、上传、只读查询以及结构化任务创建。它只能访问自己的认证状态、管理状态、上传和任务目录，不能访问 Docker Socket、GPG 私钥、Aptly 数据库或公开仓库写权限。

首次设置使用自动生成的一次性令牌，管理员在浏览器中创建密码；认证只保存 Argon2id 哈希并使用服务端会话，不保存明文密码。状态变更请求必须具有 CSRF 防护、来源检查、登录限速和审计记录。详细约束参见 [单管理员 Web 管理](web-admin.md)。

### `repo-worker`（第二阶段）

`repo-worker` 不暴露端口并使用 `network_mode: none`。它串行读取符合固定 Schema 的任务，重新校验所有参数，然后执行初始化、仓库创建、软件包发布或回滚。

只有 Worker 可以读写 Aptly 数据、访问签名私钥并写入公开仓库。Worker 不信任 `admin-web` 传入的路径、文件名、包元数据或命令文本。

### `repo-web`

`repo-web` 是非特权静态文件容器，只读挂载数据根目录中的 `public` 并向公开反向代理提供内部 HTTP 端口。它没有上传接口、管理 API、任务目录、Aptly 数据库或签名私钥。

## 持久化数据

下表使用相对于 `PRVAPTMIRROR_DATA_DIR` 的目标路径。当前实现仍使用项目内 `var/`，统一编排批次会提供显式迁移检查，不会自动覆盖现有数据。

| 路径 | 写入者 | 读取者 | 内容 |
| --- | --- | --- | --- |
| `incoming` | 现有 SSH 流程、第二阶段上传流程 | `repoctl`/Worker | 等待导入的软件包 |
| `admin/auth` | `bootstrap`、首次设置中的 `admin-web` | `admin-web` | 临时设置令牌和管理员密码哈希 |
| `admin/uploads` | `admin-web` | `admin-web`、Worker | 上传临时文件、校验结果和待处理 DEB |
| `admin/jobs` | `admin-web`、Worker | `admin-web`、Worker | 结构化任务、状态和脱敏结果 |
| `admin/audit` | `admin-web`、Worker | `admin-web` | 追加式管理审计日志 |
| `aptly` | `repoctl`/Worker | `repoctl`/Worker | Aptly 数据库、包池、快照和发布状态 |
| `gnupg` | `bootstrap`、`repoctl`/Worker | `repoctl`/Worker | 仓库签名私钥环 |
| `public` | `bootstrap`、`repoctl`/Worker | `repo-web` | 已签名的公开 APT 目录和公钥 |
| `state` | `bootstrap`、迁移工具 | 所有服务按需只读 | 数据布局版本和初始化状态 |

持久化目录便于备份和迁移。上传临时文件、失败任务和审计日志必须分别设置保留策略；GPG 私钥备份必须加密并存放在 VPS 之外。

## 仓库组织方式

内部仓库名称采用 `<family>-<suite>`，例如 `ubuntu-noble`、`debian-bookworm` 和 `lmde-lmde7`。对外路径按系统家族隔离：

```text
/ubuntu/dists/noble
/debian/dists/bookworm
/lmde/dists/lmde7
```

客户端配置示例：

```text
deb [signed-by=/etc/apt/keyrings/prvaptmirror.gpg] https://apt.example.com/ubuntu noble main
deb [signed-by=/etc/apt/keyrings/prvaptmirror.gpg] https://apt.example.com/lmde lmde7 main
```

仓库策略如下：

- 组件为 `main`；
- 二进制架构为 `amd64`、`arm64` 和 `armhf`；
- 架构无关软件包使用 `all`；
- 同一仓库可以保留多个软件包版本；
- 快照用于完整回滚，不直接修改已经创建的快照；
- 管理员必须明确选择目标系统家族和发行版，DEB 本身不能可靠声明适用发行版。

“支持某个架构”表示仓库能够索引和分发该架构软件包，不意味着软件包能跨架构或跨发行版运行。

## 发布流程

```text
上传 → 临时隔离 → DEB 预检 → 管理员确认 → 串行任务
     → 导入 → 创建快照 → 签名 → 原子发布 → 冒烟检查 → 审计记录
```

快照一经创建便不可修改。更新失败不能改变当前公开快照。回滚通过 Aptly 切换到旧快照，不手工重建索引。

## 安全规则

- 公开仓库只允许 `GET` 和 `HEAD`，不提供匿名上传或管理 API；
- 管理入口必须使用 HTTPS、密码认证、安全会话、CSRF 防护和登录限速；
- 明文密码、会话密钥和签名私钥不得进入 Git、镜像、环境变量或日志；
- `.env` 只保存路径、端口、Origin、签名身份和生成策略，不直接保存任何秘密；
- 首次设置必须验证一次性高熵令牌，不能采用匿名“先到先得”的管理员注册；
- `admin-web` 不挂载 Docker Socket、签名私钥、Aptly 数据库或公开仓库写权限；
- 签名私钥只能提供给无网络、无对外端口的 `repoctl`/Worker；
- `repo-web` 使用非特权用户、只读根文件系统和只读公开仓库挂载；
- Worker 只接受固定结构的任务，禁止执行任意 Shell 命令；
- 上传文件使用随机磁盘名称，限制大小，校验真实 DEB 格式、包名、版本和架构；
- 客户端使用独立密钥文件和 `signed-by`，不支持 `trusted=yes` 或 `apt-key`；
- 仓库状态、管理状态和签名密钥都需要备份，密钥备份必须单独加密。

## 后续边界

自动获取上游版本、镜像外部 APT 源、源码构建、QEMU 或原生构建节点、多用户角色和审批流程不属于第二阶段。它们按 [实施路线](roadmap.md) 在后续阶段加入，并继续遵守公开下载、管理控制和高权限执行相互隔离的原则。
