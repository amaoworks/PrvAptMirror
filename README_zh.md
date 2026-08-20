# PrvAptMirror

[English](README.md) | **中文**

个人用的签名 apt 仓库，带密码登录后台。把没有官方安装源的 `.deb` 托管到自己的服务器上，Debian / Ubuntu 客户端用 `Signed-By` 接入（不要使用 `trusted=yes`）。

进程**只对外暴露一个 HTTP 端口**。TLS、域名和访问控制交给宿主机上的反向代理处理。

## 一键启动

有 Docker 时默认 `--docker` 只跑 app 容器；`--dev` 使用本机 uvicorn。Origin 地址校验默认关闭。

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
| `-P` / `--password` | 管理员密码 |
| `--origin-check` | 打开地址校验（默认关闭） |

手动使用 Compose：

```bash
mkdir -p data && sudo chown 1000:1000 data
cp .env.example .env   # 可选：设置 PRVAPT_ADMIN_PASSWORD
docker compose up --build
```

- 后台：http://127.0.0.1:8000/admin/
- apt 源：http://127.0.0.1:8000/apt/
- 若未设置 `PRVAPT_ADMIN_PASSWORD`，初始密码会写入 `data/admin-bootstrap.txt`（权限 0600）。首次登录后请修改密码。

如需 HTTPS，可在宿主机用 Caddy / Traefik / nginx 反代到该端口。

## 不使用 Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export PRVAPT_DATA_DIR=$PWD/data
export PRVAPT_ADMIN_PASSWORD=change-me-now
export PRVAPT_PUBLIC_URL=http://127.0.0.1:8000
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

`scripts/backup.sh` 依赖镜像内的 `sqlite3`，会备份 `data.sqlite`、`repo/pool` 与 `gnupg/`。恢复前请先停掉应用（`scripts/restore.sh`）。
