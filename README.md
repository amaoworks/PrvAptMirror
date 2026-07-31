# PrvAptMirror

PrvAptMirror 是一个容器化、带签名的 APT 软件仓库，用于发布管理员手工上传的软件包。第一版使用 `aptly`，不提供公开上传接口，也不包含软件包构建服务。

当前项目处于架构骨架阶段，还不能直接运行。实现约定参见 [`docs/architecture.md`](docs/architecture.md)，分阶段计划参见 [`docs/roadmap.md`](docs/roadmap.md)。

## MVP 简述

管理员通过 SSH 上传 `.deb`，运行一条管理命令，系统便生成带 GPG 签名的 APT 仓库，并通过 Web 容器的内部端口交给现有反向代理对外提供服务。

## 目录结构

| 路径 | 用途 | 是否属于 MVP |
| --- | --- | --- |
| `deploy/compose/` | Docker Compose 部署入口 | 是 |
| `containers/repoctl/` | 包含 `aptly`、GPG 和管理命令的镜像 | 是 |
| `containers/repo-web/` | 只读的 APT 下载服务 | 是 |
| `config/aptly/` | Aptly 默认配置与仓库策略 | 是 |
| `config/web/` | 内部 Web 服务配置 | 是 |
| `scripts/` | 宿主机侧的便捷命令 | 是 |
| `docs/` | 架构、运维和路线文档 | 是 |
| `tests/smoke/` | 发布与安装冒烟测试 | 是 |
| `var/` | 本地运行数据，内容禁止提交 | 是 |
| `catalog/` | 上游软件及更新规则的声明式定义 | 后续 |
| `automation/fetch/` | GitHub、普通网站和 APT 源抓取器 | 后续 |
| `automation/build/` | 隔离的源码到 DEB 构建流程 | 后续 |
| `automation/publish/` | 验证和发布晋升策略 | 后续 |
| `packaging/` | 各软件独立的 Debian 打包配方 | 后续 |

## 运行数据

`var/lib/gnupg` 保存仓库签名私钥，必须进行安全备份，并且绝不能暴露给 `repo-web`。Web 容器只能以只读方式挂载 `var/public`。

