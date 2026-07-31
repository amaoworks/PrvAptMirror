# 系统架构

## 项目目标

MVP 用于向 Debian 系操作系统发布由管理员提供的软件包，并支持多种 CPU 架构。系统应当可以在不同 VPS 之间方便迁移，能够放在现有反向代理之后，并且后续扩展时不需要替换仓库管理引擎。

## 系统边界

```text
管理员
  │ 通过 SCP/SFTP 上传 .deb
  ▼
var/incoming ──► repoctl 容器 ──► aptly 数据
                      │               │
                      │ GPG 签名      │ 创建快照
                      ▼               ▼
                  签名私钥        var/public
                                      │ 只读挂载
                                      ▼
                                 repo-web:8080
                                      │
                                   反向代理
                                      │ HTTPS
                                      ▼
                                  APT 客户端
```

外部反向代理、DNS 和 TLS 证书管理不属于本项目范围。本项目只暴露一个内部 HTTP 端口。APT 仓库内容仍然是静态文件，因此可以正常使用缓存。

## MVP 组件

### `repoctl`

这是一个按需运行的容器，内含 `aptly`、GnuPG、软件包检查工具和一个轻量的管理命令封装。只有这个组件可以写入仓库状态或访问签名私钥。

初始命令接口约定如下：

```text
repoctl init
repoctl repo create <family> <suite>
repoctl package add <family> <suite> <deb-path>
repoctl package list <family> <suite>
repoctl snapshot list <family> <suite>
repoctl rollback <family> <suite> [snapshot]
repoctl key export
```

`package add` 负责检查 DEB 元数据、导入本地仓库、创建带时间戳的快照、完成签名，并以原子方式首次发布或切换公开发行版。验证或发布失败时，客户端仍然能够使用上一个有效快照。

### `repo-web`

这是一个小型、非特权的 Web 容器，在 `8080` 端口提供 `var/public` 中的文件。它没有上传接口和管理 API，也不能访问 `var/incoming`、Aptly 数据库或签名私钥。

### 持久化数据

| 路径 | 写入者 | 读取者 | 内容 |
| --- | --- | --- | --- |
| `var/incoming` | 管理员 | `repoctl` | 等待导入的软件包 |
| `var/lib/aptly` | `repoctl` | `repoctl` | Aptly 数据库、包池和快照 |
| `var/lib/gnupg` | `repoctl` | `repoctl` | 仓库签名密钥环 |
| `var/public` | `repoctl` | `repo-web` | 已签名的公开 APT 目录和公钥 |

MVP 将以上四个路径作为持久化的宿主机挂载目录，便于查看、备份和迁移。后续可以根据部署需要改成 Docker 命名卷。

## 仓库组织方式

内部仓库名称采用 `<family>-<suite>` 格式，例如 `ubuntu-noble`、`debian-bookworm` 和 `lmde-lmde6`。对外路径按照操作系统家族隔离：

```text
/ubuntu/dists/noble
/debian/dists/bookworm
/lmde/dists/lmde6
```

对应的客户端配置示例：

```text
deb [signed-by=/etc/apt/keyrings/prvaptmirror.gpg] https://apt.example.com/ubuntu noble main
deb [signed-by=/etc/apt/keyrings/prvaptmirror.gpg] https://apt.example.com/debian bookworm main
```

MVP 的仓库策略如下：

- 组件为 `main`；
- 二进制架构为 `amd64`、`arm64` 和 `armhf`；
- 架构无关软件包使用 `all`；
- 源码包推迟到源码构建阶段实现；
- 每个已发布快照中，同一软件包名称、发行版和架构只保留一个版本；
- 管理员必须明确选择目标操作系统家族和发行版，因为 DEB 本身不能可靠声明它是为哪个 Ubuntu 或 Debian 版本构建的。

这里的“支持某个架构”表示仓库能够索引和分发相应软件包，并不意味着为某个架构或发行版构建的软件包可以在其他目标上正常运行。

## 发布流程

```text
上传 → 检查 → 导入 → 创建快照 → 签名 → 发布或切换 → 冒烟检查
```

快照一经创建便不可修改。更新失败不会改变当前公开快照。回滚操作通过 Aptly 将已发布发行版切换到旧快照完成，不手工重新生成软件包索引。

## 安全规则

- MVP 不提供匿名上传或基于 HTTP 的上传接口。
- 签名私钥只能挂载到 `repoctl`。
- `repo-web` 使用非特权用户运行；在运行环境支持时，根文件系统和公开仓库挂载都设置为只读。
- 客户端必须通过独立密钥文件和 `signed-by` 建立信任，不支持 `trusted=yes` 或 `apt-key`。
- 发布前检查软件包名称、版本、架构和控制文件的有效性。
- 仓库状态与签名密钥都需要备份；密钥备份必须加密，并存放在 VPS 之外。

## 延后实现的模块

自动抓取上游版本、镜像第三方 APT 源、源码构建、QEMU 或原生 ARM 构建节点、软件包晋升策略以及管理界面都不属于 MVP。项目预留了对应目录，使这些能力后续能够作为独立工作进程加入，同时不会获得公开 Web 服务的安全权限。

