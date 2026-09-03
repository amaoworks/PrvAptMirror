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
mkdir -p data && sudo chown 1000:1000 data
cp .env.example .env
docker compose pull
docker compose up -d
```

`.env.example` 默认固定到 `0.0.1` 镜像，升级时请明确修改 `PRVAPT_IMAGE`。如需从本地源码构建：

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up --build
```

- 后台：http://127.0.0.1:8000/admin/
- apt 源：http://127.0.0.1:8000/apt/
- 未显式指定首次密码时，生成的初始密码会写入 `data/admin-bootstrap.txt`（权限 0600），首次登录后必须修改。

如需 HTTPS，可在宿主机用 Caddy / Traefik / nginx 反代到该端口。

## 网站设置

登录后可在 **设置** 页面管理公开 URL、Suite、Codename、Component、架构、Origin、Label、上传限制和新会话期限。首次启动时环境变量中的业务参数只用于初始化数据库，此后以网站保存的设置为准。修改仓库元数据会自动重建并重新签名索引；失败时设置会回滚，中途崩溃则在下次启动时恢复一致状态。

镜像版本、宿主机数据目录、绑定地址、端口、可信代理和密钥仍由部署环境管理。`.env.example` 因此只保留四个部署参数。

## 自动软件来源

后台的 **软件来源** 页面可配置自动下载，不需要增加容器或外部来源配置文件。调度器运行在同一个 Uvicorn 进程内，来源定义、调度时间、运行历史和发现的文件全部保存在 `data.sqlite`。

目前支持两种来源：

- **GitHub Releases**：填写 `owner/repository`、Asset 文件名正则、检查最近几个 Release、是否包含 prerelease，以及检查间隔。只下载同时匹配规则且以 `.deb` 结尾的 Asset。
- **固定下载地址**：定期访问一个 HTTP(S) 地址。远端支持时使用 `ETag` 和 `Last-Modified` 条件请求，并使用内容摘要避免重复导入。

每个来源都可在网页中新增、编辑、启用、停用、删除或“立即同步”。下载内容会流式写入 `data/incoming`，通过 Debian 软件包校验后整批导入，最后只重建和签名一次索引。完全相同的软件包会跳过；如果 Package、Version、Architecture 相同但内容摘要不同，会显示冲突，不会静默覆盖。检查失败会进行指数退避重试，最长不超过来源设置的正常检查间隔。

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

推送到 `main` 会发布 `edge` 和 `sha-*` 镜像；推送 `v0.0.1` 这样的语义化 Git Tag，会发布正式版本标签（`0.0.1`、`0.0`、`0`）并更新 `latest`。已经发布的完整版本标签不得覆盖；生产环境应固定完整版本号，不建议直接使用 `latest`。

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

## 备份

```bash
docker compose exec app /app/scripts/backup.sh /tmp/prvapt-backup
```

`scripts/backup.sh` 依赖镜像内的 `sqlite3`，会备份 `data.sqlite`、`repo/pool`、`gnupg/` 与 `secret-key`。备份中包含签名私钥和来源凭据的加密密钥，应按敏感文件保存。恢复前请先停掉应用（`scripts/restore.sh`）。
