# PrvAptMirror

PrvAptMirror 是一个容器化、带签名的 APT 软件仓库，用于安全发布管理员提供的软件包。仓库引擎使用 `aptly`，公开下载服务与管理权限相互隔离。

第一阶段的命令行发布基础已经完成并通过端到端测试。项目当前进入**第二阶段：单管理员 Web 管理**，目标是在浏览器中提供密码登录、软件包上传、仓库管理、签名发布、快照查看和回滚能力。

架构与安全边界参见 [系统架构](docs/architecture.md)，当前阶段范围参见 [单管理员 Web 管理](docs/web-admin.md)，统一编排设计参见 [统一多容器编排与首次设置](docs/unified-deployment.md)，部署和命令行操作参见 [运维与使用说明](docs/operations.md)，后续计划参见 [实施路线](docs/roadmap.md)。

## 当前能力

- 创建面向 Ubuntu、Debian、LMDE 等发行版的独立仓库；
- 校验并导入 `amd64`、`arm64`、`armhf` 和 `all` DEB；
- 创建不可变快照并生成 GPG 签名的 APT 元数据；
- 原子发布新版本和回滚到旧快照；
- 通过非特权、只读 Web 容器提供软件包下载、公开接入说明和单一站点入口；
- 通过一个 Compose 项目编排 bootstrap、管理端、无网络 Worker 和公开下载服务；
- 将全部应用状态收敛到 `.env` 指定的单一数据根目录；
- 幂等生成目录权限、一次性设置令牌、GPG 密钥和公开密钥；
- 在浏览器首次创建管理员密码，不提供默认密码；
- 提供单管理员登录、安全会话和公开仓库只读概览；
- 使用隔离的端到端测试验证签名、多架构、更新和回滚。

## 当前阶段正在增加

- 浏览器上传 DEB、预检元数据和计算 SHA-256；
- 仓库、软件包、快照、公钥和任务状态页面；
- 在网页执行仓库创建、导入发布和回滚；
- 无网络 Worker、串行任务和追加式审计日志；
- 使用一个对外 Web 入口，并在内部隔离管理页面与公开 APT 下载权限。

当前阶段不包含多用户、注册、角色、审批流、自动下载上游软件或源码构建。

## 目录结构

| 路径 | 用途 | 状态 |
| --- | --- | --- |
| `compose.yaml` | 完整多容器部署入口 | 已实现 |
| `containers/repoctl/` | Aptly、GPG 和仓库管理命令 | 已实现 |
| `containers/repo-web/` | 只读 APT 下载服务 | 已实现 |
| `config/aptly/` | Aptly 配置与仓库策略 | 已实现 |
| `config/web/` | 公开下载服务配置 | 已实现 |
| `scripts/` | 宿主机便捷命令 | 已实现 |
| `docs/` | 架构、运维、安全和路线文档 | 持续更新 |
| `tests/smoke/` | 发布与安装冒烟测试 | 已实现 |
| `data/` | 单一运行数据根目录，内容禁止提交 | 已实现 |
| `containers/admin-web/` | 首次设置、单管理员登录与只读概览 | 第二阶段检查点 2 已实现 |
| `containers/repo-worker/` | 无网络 Worker 容器基础 | 编排已实现，业务任务待接入 |
| `catalog/` | 上游软件及更新规则 | 第三阶段 |
| `automation/fetch/` | GitHub、网站和 APT 源抓取器 | 第三阶段 |
| `automation/build/` | 隔离的源码到 DEB 构建流程 | 第四阶段 |
| `automation/publish/` | 验证和发布晋升策略 | 后续阶段 |
| `packaging/` | Debian 打包配方 | 第四阶段 |

## 安全边界

`data/gnupg` 保存仓库签名私钥，必须进行加密备份。公开 `repo-web` 只能只读挂载 `data/public`，并把 `/admin/` 转发给内部管理服务；`admin-web` 也不能访问签名私钥、Aptly 数据库或 Docker Socket。只有无网络的 `repo-worker`/高级 `repoctl` 可以执行签名和发布写操作。

浏览器只需访问 `ADMIN_PUBLIC_ORIGIN`：根路径是仓库首页和 APT 接入说明，`/admin/` 是受密码保护的管理入口，`/ubuntu`、`/debian` 和 `/lmde` 是实际软件源路径。

## 基础命令

```bash
cp .env.example .env
./scripts/prvaptmirror up
./scripts/prvaptmirror setup
./scripts/prvaptmirror status
```

第二阶段检查点 2 已实现统一编排、首次 Web 设置、安全登录和只读仓库概览；上传、Worker 业务任务及发布/回滚页面尚未接入。在完整写操作通过安全测试前，生产发布继续使用 [运维与使用说明](docs/operations.md) 中的高级 `repoctl` 流程。
