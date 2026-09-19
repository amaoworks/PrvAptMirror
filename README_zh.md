# PrvAptMirror

[English](README.md) | **中文**

个人用的签名 apt 仓库，带密码登录后台。把没有官方安装源的 `.deb` 托管到自己的服务器上，Debian / Ubuntu 客户端用 `Signed-By` 接入（不要使用 `trusted=yes`）。

进程**只对外暴露一个 HTTP 端口**。TLS、域名和访问控制交给宿主机上的反向代理处理。

## 一键启动

有 Docker 时，`--docker` 默认拉取已发布的 GHCR 镜像；`--dev` 使用本机 uvicorn。Origin 地址校验默认关闭。

```bash
chmod +x scripts/start.sh
./scripts/start.sh --docker --local -p 8000 --password 'change-me'
./scripts/start.sh --dev --public -p 8000
./scripts/start.sh --origin-check          # 打开 Origin/Referer 白名单
./scripts/start.sh stop
```

| 选项 | 作用 |
| --- | --- |
| `-p` / `--port` | 主机端口（默认 8000） |
| `--local` | 仅监听 `127.0.0.1`，打印本机地址 |
| `--public` | 监听 `0.0.0.0`，打印本机和各网卡地址 |
| `--dev` / `--docker` | 本机 uvicorn 或 Docker app 容器 |
| `--build` | 从当前源码构建 Docker 镜像，而不是从 GHCR 拉取 |
| `-P` / `--password` | 首次启动的管理员密码，不会在重启时覆盖网站密码 |
| `--origin-check` | 打开地址校验（默认关闭） |

手动使用 Compose：

```bash
mkdir -p data && sudo chown -R 1000:1000 data
cp .env.example .env
docker compose pull
docker compose up -d
```

`.env.example` 默认固定到 `0.1.1` 镜像，升级时请明确修改 `PRVAPT_IMAGE`。如需从本地源码构建：

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

- 后台：http://127.0.0.1:8000/admin/
- apt 源：http://127.0.0.1:8000/apt/
- 未显式指定首次密码时，生成的初始密码会写入 `data/admin-bootstrap.txt`（权限 0600），首次登录后必须修改。

已有数据迁移前先停服务，并递归修正整个数据目录的属主；应用启动会检查索引目录是否可写。容器运行用户固定为 `1000:1000`。

如需 HTTPS，可在宿主机用 Caddy / Traefik / nginx 反代到该端口。

## 网站设置

登录后可在 **设置** 页面管理公开 URL、Suite、Codename、Component、架构、Origin、Label、上传限制和新会话期限。首次启动时环境变量中的业务参数只用于初始化数据库，此后以网站保存的设置为准。修改仓库元数据会自动重建并重新签名索引；失败时设置会回滚，中途崩溃则在下次启动时恢复一致状态。

镜像版本、宿主机数据目录、绑定地址、端口、可信代理和密钥仍由部署环境管理。`.env.example` 因此只保留四个部署参数。

## 自动软件来源

后台的 **软件来源** 页面可配置自动下载，不需要增加容器或外部来源配置文件。调度器运行在同一个 Uvicorn 进程内，来源定义、调度时间、运行历史和发现的文件全部保存在 `data.sqlite`。

目前支持两种来源：

- **GitHub Releases**：填写 `owner/repository`、Asset 文件名正则、检查最近几个 Release、是否包含 prerelease，以及检查间隔。只下载同时匹配规则且以 `.deb` 结尾的 Asset。
- **固定下载地址或 HTTP 目录**：定期访问一个 `.deb` 地址；也可填写以 `/` 结尾的同源 HTML 目录，按 Asset 正则筛选，并按 Debian 版本规则选择每个包和架构的最新 `package_version_arch.deb`。固定文件在远端支持时使用 `ETag` 和 `Last-Modified` 条件请求，内容摘要可避免重复导入。

每个来源都可在网页中新增、编辑、启用、停用、删除或“立即同步”。下载内容会流式写入 `data/incoming`，通过 Debian 软件包校验后整批导入，最后只重建和签名一次索引。完全相同的软件包会跳过；如果 Package、Version、Architecture 相同但内容摘要不同，会显示冲突，不会静默覆盖。手动“立即同步”会重试本次仍被选中的已拒绝/冲突文件，定时检查则跳过这些终态记录。检查失败会进行指数退避重试，最长不超过来源设置的正常检查间隔。

新增或编辑 GitHub 或 HTTP 目录来源时，可先点击“测试获取与正则”。测试读取远端元数据但不保存来源，并逐项标明文件会被抓取、属于较旧版本、不是 `.deb`，还是未命中正则；目录测试还会探测最新版下载响应并验证 Debian ar 文件头。

GitHub Token 为可选项，可用于私有仓库或提高 API 限额。Token 在写入数据库前会加密，之后不会在页面回显。加密密钥由 `data/secret-key` 派生，因此迁移或恢复时必须让它与 `data.sqlite` 一起保留。

现有目录映射会让全部内容在升级镜像或重建容器后继续存在：

```text
./data:/var/lib/prvaptmirror
```

项目不会额外构建 Worker 镜像或启动 Worker 服务：网站、定时调度、下载、软件包导入和 APT 发布都在唯一的 `app` 容器中运行。

如果忘记管理员密码，可在交互式终端中重置：

```bash
docker compose exec app prvaptmirror-admin reset-password --username admin
```

## 容器版本发布

推送到 `main` 会发布 `edge` 和 `sha-*` 镜像；推送 `v0.1.1` 这样的语义化 Git Tag，会发布正式版本标签（`0.1.1`、`0.1`、`0`）并更新 `latest`。已经发布的完整版本标签不得覆盖；生产环境应固定完整版本号，不建议直接使用 `latest`。

## 不使用 Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export PRVAPT_DATA_DIR=$PWD/data
.venv/bin/uvicorn prvaptmirror.main:app --host 127.0.0.1 --port 8000 --workers 1
```

应用同时提供 `/admin/` 与 `/apt/`。

## 客户端接入

在后台的 **客户端接入** 页面复制命令即可。脚本会把 ASCII 公钥安装到 `/etc/apt/keyrings`，并写入带 `Signed-By` 的 deb822 源。不要使用 `apt-key` 或 `trusted=yes`。

## 测试

```bash
.venv/bin/pytest -q
```

可选的官方 apt 客户端联调：`tests/integration/test_apt_client.sh`（需要 Docker，且服务已启动）。

## 备份与恢复

备份保存到映射在宿主机的 `data/backups`，容器重建后仍保留。每次使用新目录；备份期间上传、删除和发布会等待备份完成，网站读取仍可用。

```bash
backup_name="prvapt-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose exec -T app /app/scripts/backup.sh "/var/lib/prvaptmirror/backups/$backup_name"
# 再导出一份到独立目录，可继续复制到其他主机或备份存储。
mkdir -p backups
docker compose cp "app:/var/lib/prvaptmirror/backups/$backup_name" ./backups/
```

脚本使用 Python 标准库，在发布锁内一起备份 `data.sqlite`、`repo/pool`、`gnupg/` 和 `secret-key`，并验证包摘要。备份完整写入后才出现在目标路径，包含数据库和归档的 SHA-256 校验文件。备份包含签名私钥和来源凭据加密密钥，目录权限为 0700，文件为 0600。

在项目目录执行恢复（宿主机需 Python 3.11+ 和 Docker Compose）：

```bash
sudo ./scripts/restore.sh "./backups/$backup_name"
docker compose up -d
```

恢复脚本从 Compose 解析实际数据目录，先停止 `app`；停止失败立即退出。它会校验备份、在旁边构建干净的数据目录，清除旧索引/WAL 的影响，设置重建标记并恢复 UID/GID 1000 的权限，最后原子替换数据目录。旧数据保留在输出的 `.data.before-restore-*` 路径；确认恢复成功后自行归档或删除。需要足够空间同时保存旧数据和恢复数据。首次启动重新生成并签名索引，恢复期间请勿另行启动应用。

如果之前使用 `scripts/start.sh` 的 `.env.runtime` 配置，恢复时增加 `--compose-env-file .env.runtime`。非 Docker 部署需先停止进程，再显式使用 `--offline --data-dir /实际数据目录 --uid 用户UID --gid 用户GID`；运行中的新版应用持有服务锁，会阻止覆盖恢复。

导入时如果数据库写入失败，新建的包文件会回滚；崩溃遗留的未登记包会在启动时移到 `data/quarantine`，保留供检查，并可重新上传原包。上传请求在接收文件前验证登录，文件按块写入；control 归档压缩前后各限制为 16 MiB，control 文本限制为 1 MiB，解码器窗口/内存上限为 64 MiB。
