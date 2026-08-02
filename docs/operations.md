# 运维与使用说明

第一阶段的命令行仓库管理已经稳定可用。项目当前进入单管理员 Web 管理阶段；在 Web 管理功能完成并通过安全测试前，生产环境继续使用本文的命令行流程。第二阶段功能和密码登录要求参见 [单管理员 Web 管理](web-admin.md)。

## 环境要求

VPS 需要安装 Docker Engine、Docker Compose 插件以及用于上传文件的 SSH 服务。TLS 和域名反向代理由宿主机现有环境负责。

## 1. 配置环境

在项目根目录复制示例配置：

```bash
cp .env.example .env
```

至少应修改 `.env` 中的签名身份：

```text
REPO_GPG_NAME=你的仓库名称
REPO_GPG_EMAIL=你的邮箱
```

Web 服务默认只监听 `127.0.0.1:8080`，适合由宿主机反向代理访问。只有反向代理运行在其他主机或隔离网络中时，才需要调整监听地址。

## 2. 构建镜像

默认从 Docker Hub 获取基础镜像。如果服务器无法访问 Docker Hub，可以在 `.env` 中配置镜像代理：

```text
REPO_WEB_BASE_IMAGE=docker.m.daocloud.io/nginxinc/nginx-unprivileged:1.27-alpine
REPOCTL_BASE_IMAGE=docker.m.daocloud.io/library/debian:bookworm-slim
REPOCTL_APT_MIRROR=http://mirrors.163.com
```

```bash
./scripts/prvaptmirror build
```

## 3. 初始化签名密钥

```bash
./scripts/prvaptmirror repoctl init
```

初始化命令会在 `var/lib/gnupg` 中创建一个无交互签名密钥，并把以下公钥文件发布到 `var/public`：

```text
repository-key.gpg
repository-key.asc
```

私钥用于 VPS 上的自动签名，因此签名密钥当前不设置交互式口令。它与第二阶段的 Web 管理员登录密码是两类凭据：管理员密码只用于登录管理页面，不能用于解密或导出签名私钥。必须限制 VPS 和该目录的访问权限，并将私钥加密备份到其他位置。

## 4. 创建发行版仓库

例如创建 Ubuntu 24.04 Noble 仓库：

```bash
./scripts/prvaptmirror repoctl repo create ubuntu noble
```

其他示例：

```bash
./scripts/prvaptmirror repoctl repo create ubuntu jammy
./scripts/prvaptmirror repoctl repo create debian bookworm
./scripts/prvaptmirror repoctl repo create lmde lmde6
```

系统不会判断一个 DEB 适用于哪个发行版。创建和导入时必须由管理员选择正确目标。

## 5. 上传并发布软件包

通过 SCP 将软件包放入 `var/incoming`：

```bash
scp example_1.0.0_arm64.deb user@vps:/path/to/PrvAptMirror/var/incoming/
```

然后导入并发布：

```bash
./scripts/prvaptmirror repoctl package add \
  ubuntu noble /incoming/example_1.0.0_arm64.deb
```

该命令会依次完成元数据检查、架构检查、导入、创建快照、签名和原子发布。默认允许 `amd64`、`arm64`、`armhf` 和 `all`。

第二阶段将提供浏览器上传、DEB 元数据预检、SHA-256 展示和发布确认。在该功能正式上线前，不应临时增加匿名 HTTP 上传接口。

同一时间只能执行一个会修改仓库的 `repoctl` 命令。程序会使用共享文件锁阻止并发写入，遇到锁冲突时等待当前任务结束后重试即可。

查看软件包和快照：

```bash
./scripts/prvaptmirror repoctl package list ubuntu noble
./scripts/prvaptmirror repoctl snapshot list ubuntu noble
```

## 6. 启动下载服务

```bash
./scripts/prvaptmirror up
./scripts/prvaptmirror status
```

健康检查地址为：

```text
http://127.0.0.1:8080/healthz
```

将现有反向代理的上游设置为该地址对应的端口即可。反向代理需要允许 `GET` 和 `HEAD`，不需要开放写请求。

## 7. 配置客户端

以下示例假设仓库域名为 `apt.example.com`，目标仓库为 Ubuntu Noble：

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.example.com/repository-key.gpg \
  | sudo tee /etc/apt/keyrings/prvaptmirror.gpg >/dev/null

echo 'deb [signed-by=/etc/apt/keyrings/prvaptmirror.gpg] https://apt.example.com/ubuntu noble main' \
  | sudo tee /etc/apt/sources.list.d/prvaptmirror.list

sudo apt update
```

Debian 和 LMDE 客户端分别使用 `/debian` 和 `/lmde` 路径。

## 8. 回滚

不指定快照时回滚到当前快照之前的版本：

```bash
./scripts/prvaptmirror repoctl rollback ubuntu noble
```

也可以先列出快照，再指定目标：

```bash
./scripts/prvaptmirror repoctl snapshot list ubuntu noble
./scripts/prvaptmirror repoctl rollback ubuntu noble ubuntu-noble-20260801T120000123456789Z
```

## 9. 备份

至少需要备份：

```text
var/lib/aptly
var/lib/gnupg
```

`var/public` 可以根据 Aptly 状态重新发布，但备份它能缩短恢复时间。`var/incoming` 只保存待导入文件，不应作为正式软件包归档。

## 10. 测试

运行不需要 Docker 的静态检查：

```bash
./tests/smoke/static.sh
```

在安装 Docker 的环境中运行完整端到端测试：

```bash
./tests/smoke/run.sh
```

端到端测试会在临时目录中生成两个测试 DEB，验证初始化、签名、首次发布、更新、回滚以及 Web 下载服务。

第二阶段还必须增加密码登录、登录限速、会话、CSRF、上传路径穿越、超大文件、伪造 DEB、重复提交、并发任务和审计日志测试，完成标准参见 [单管理员 Web 管理](web-admin.md)。

## 11. 启用管理 Web（检查点 1）

检查点 1 只提供安全登录和公开仓库只读概览，不提供上传或发布写操作。管理服务使用显式 `admin` profile，不会随公开下载服务自动启动。

先在 `.env` 设置浏览器实际访问的 HTTPS Origin：

```text
ADMIN_PUBLIC_ORIGIN=https://apt-admin.example.com
ADMIN_USERNAME=admin
ADMIN_HTTP_BIND=127.0.0.1
ADMIN_HTTP_PORT=28081
```

生成 Argon2id 密码哈希。密码通过终端无回显输入，重定向文件中只有哈希：

```bash
umask 077
./scripts/prvaptmirror admin hash-password \
  > var/admin/secrets/password-hash
sudo chown 10001:10001 var/admin/secrets/password-hash
chmod 0400 var/admin/secrets/password-hash
```

启动并检查服务：

```bash
./scripts/prvaptmirror admin up
./scripts/prvaptmirror admin status
```

将独立管理域名反代到 `127.0.0.1:28081`，并配置有效 TLS。应用会严格校验 `Origin` 是否等于 `ADMIN_PUBLIC_ORIGIN`。管理端不能直接暴露内部端口，也不能与匿名 APT 下载路径共用鉴权规则。

当前会话存放在单个 Gunicorn 进程内存中，服务重启会使全部会话失效，这是单管理员阶段的安全默认。运行认证测试：

```bash
./scripts/prvaptmirror admin test
```
