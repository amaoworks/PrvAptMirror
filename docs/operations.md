# MVP 使用说明

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

私钥用于 VPS 上的自动签名，因此当前 MVP 不设置口令。必须限制 VPS 和该目录的访问权限，并将私钥加密备份到其他位置。

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
