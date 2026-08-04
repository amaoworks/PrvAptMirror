# 运维与使用说明

PrvAptMirror 使用根目录 `compose.yaml` 编排 `bootstrap`、`admin-web`、`repo-worker` 和 `repo-web`。全部应用持久化状态位于 `.env` 指定的单一数据根目录；正常运维只使用 `./scripts/prvaptmirror`。

浏览器上传和发布页面尚未实现，因此日常发布暂时继续使用高级 `repoctl` 命令。统一部署与首次设置的安全设计参见 [统一多容器编排与首次设置](unified-deployment.md)。

## 1. 环境要求

- Docker Engine；
- Docker Compose 插件；
- 用于部署和读取首次设置令牌的 SSH；
- 宿主机现有的 DNS、TLS 和反向代理。

项目不会修改宿主机 Nginx、DNS、防火墙或系统目录。Docker Engine 仍会在自己的数据根目录保存镜像、容器元数据和日志。

## 2. 配置

在项目根目录执行：

```bash
cp .env.example .env
```

至少检查：

```dotenv
PRVAPTMIRROR_DATA_DIR=./data

REPO_HTTP_BIND=127.0.0.1
REPO_HTTP_PORT=28080
ADMIN_PUBLIC_ORIGIN=https://apt.example.com

REPO_GPG_NAME=PrvAptMirror Archive
REPO_GPG_EMAIL=apt@example.com
REPO_GPG_EXPIRE=2y
```

`PRVAPTMIRROR_DATA_DIR` 可以是项目根目录下的相对路径，也可以是其他磁盘上的绝对路径。所有应用绑定挂载都从这个目录派生。

`.env` 不得包含管理员密码、设置令牌、会话令牌、GPG 私钥或 SSH 私钥。

如果服务器无法访问默认镜像源，可以在 `.env` 中覆盖基础镜像和 Debian 镜像地址，示例见 `.env.example`。

## 3. 首次启动

```bash
./scripts/prvaptmirror up
./scripts/prvaptmirror status
```

`up` 会构建镜像并执行以下流程：

1. 无网络 `bootstrap` 创建数据布局和权限；
2. 自动生成一次性管理员设置令牌；
3. 自动生成或复用 RSA 3072 OpenPGP 仓库签名密钥；
4. 将公钥导出到 `data/public`；
5. 启动管理端、无网络 Worker 和只读下载服务。

`bootstrap` 正常状态为 `Exited (0)`；其他三个常驻服务应为 `Up` 和 `healthy`。

初始化是幂等的。重复启动不会覆盖管理员密码、设置令牌或已有 GPG 私钥。

## 4. 浏览器创建管理员密码

在 SSH 终端执行：

```bash
./scripts/prvaptmirror setup
```

命令会显示形如 `https://apt.example.com/admin/setup` 的管理设置地址和一个 64 位十六进制一次性令牌。打开管理页面，输入令牌并创建至少 8 个字符，且同时包含大写字母、小写字母和数字的管理员密码。

设置成功后：

- `data/admin/auth/password-hash` 只保存 Argon2id 哈希；
- `data/admin/auth/setup-token` 被删除；
- `/admin/setup` 永久关闭；
- 再次执行 `./scripts/prvaptmirror setup` 只会报告首次设置已完成。

令牌不会写入 `.env`、URL 或普通容器日志。管理页面必须通过 `ADMIN_PUBLIC_ORIGIN` 对应的 HTTPS 地址访问。仅在回环地址开发或 SSH 隧道测试时才可临时设置：

```dotenv
ADMIN_PUBLIC_ORIGIN=http://127.0.0.1:28080
ADMIN_ALLOW_INSECURE_ORIGIN=1
```

生产环境禁止启用不安全 Origin。

## 5. 反向代理

默认只有一个上游：

```text
仓库站点与管理入口：127.0.0.1:28080
```

反向代理只需把一个 HTTPS 域名转发到该上游，并保留原始 Host、协议和客户端地址转发头。根路径提供公开使用说明，APT 文件位于 `/ubuntu`、`/debian` 和 `/lmde`，管理页面位于 `/admin/`。公开文件路径只允许 `GET` 和 `HEAD`，`/admin/` 的写请求由管理端执行认证、Origin 与 CSRF 校验。

健康检查：

```text
http://127.0.0.1:28080/healthz
http://127.0.0.1:28080/admin/healthz
```

## 6. 生命周期命令

```bash
./scripts/prvaptmirror up
./scripts/prvaptmirror stop
./scripts/prvaptmirror status
./scripts/prvaptmirror logs
```

`stop` 只停止常驻容器，不删除数据。项目没有提供自动清空数据的命令。

管理会话目前保存在单个 Gunicorn 进程内存中，管理服务重启会使现有会话失效。

## 7. 高级仓库操作

浏览器上传和发布任务接入前，可以通过 SSH 把 DEB 放到默认数据根目录：

```bash
scp example_1.0.0_arm64.deb user@vps:/path/to/PrvAptMirror/data/incoming/
```

创建仓库并发布：

```bash
./scripts/prvaptmirror repoctl repo create ubuntu noble
./scripts/prvaptmirror repoctl package add \
  ubuntu noble /incoming/example_1.0.0_arm64.deb
```

查看软件包和快照：

```bash
./scripts/prvaptmirror repoctl package list ubuntu noble
./scripts/prvaptmirror repoctl snapshot list ubuntu noble
```

回滚到前一个快照：

```bash
./scripts/prvaptmirror repoctl rollback ubuntu noble
```

或者指定快照：

```bash
./scripts/prvaptmirror repoctl rollback \
  ubuntu noble ubuntu-noble-20260801T120000123456789Z
```

系统无法从 DEB 元数据可靠判断目标发行版，管理员必须明确选择正确的 family 和 suite。写操作使用共享文件锁，不允许并发发布。

## 8. 配置 APT 客户端

以下示例假设公开域名为 `apt.example.com`，目标仓库为 Ubuntu Noble：

同样的说明已经内置到仓库首页，部署后访问 `https://apt.example.com/` 即可查看并复制。

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.example.com/repository-key.gpg \
  | sudo tee /etc/apt/keyrings/prvaptmirror.gpg >/dev/null

echo 'deb [signed-by=/etc/apt/keyrings/prvaptmirror.gpg] https://apt.example.com/ubuntu noble main' \
  | sudo tee /etc/apt/sources.list.d/prvaptmirror.list

sudo apt update
```

Debian 和 LMDE 客户端分别使用 `/debian` 和 `/lmde` 路径。

## 9. 数据与备份

建议整体备份 `PRVAPTMIRROR_DATA_DIR`。至少必须保护：

```text
data/aptly
data/gnupg
data/admin/auth
```

`data/gnupg` 包含无人值守签名私钥，备份必须额外加密并存放在服务器之外。`data/public` 可以根据 Aptly 状态重新发布，但备份它能缩短恢复时间。

不要单独移动数据根目录中的某个子目录。恢复时应保持原有目录结构和权限，再运行 `./scripts/prvaptmirror up` 让 bootstrap 校验状态。

## 10. 测试

静态检查：

```bash
./tests/smoke/static.sh
```

管理端认证和首次设置测试：

```bash
./scripts/prvaptmirror test
```

完整仓库端到端测试：

```bash
./tests/smoke/run.sh
```

完整首次设置端到端测试：

```bash
./tests/smoke/admin-setup.sh
```

端到端测试使用临时数据根目录，验证 bootstrap、GPG 签名、多架构索引、更新、回滚和公开下载，不会改写正式数据目录。
