# PrvAptMirror

**English** | [中文](README_zh.md)

Personal signed apt repository with a password-protected admin UI. Upload `.deb` files that have no public source, then add the repo to Debian or Ubuntu with `Signed-By` (never `trusted=yes`).

The process **only exposes one HTTP port**. TLS, domain names, and access control belong on the host reverse proxy.

## One-click start

With Docker, `--docker` runs the app container by default; `--dev` uses local uvicorn. Origin checks are off by default.

```bash
chmod +x scripts/start.sh
./scripts/start.sh --docker --local -p 8000 --password 'change-me'
./scripts/start.sh --dev --public -p 8000
./scripts/start.sh --origin-check          # enable Origin/Referer allow-list
./scripts/start.sh stop
```

| Option | Meaning |
| --- | --- |
| `-p` / `--port` | Host port (default 8000) |
| `--local` | Bind `127.0.0.1` only; print local URLs |
| `--public` | Bind `0.0.0.0`; print local and NIC URLs |
| `--dev` / `--docker` | Local uvicorn or Docker app container |
| `-P` / `--password` | Admin password |
| `--origin-check` | Enable address verification (off by default) |

Manual Compose:

```bash
mkdir -p data && sudo chown 1000:1000 data
cp .env.example .env   # optional: set PRVAPT_ADMIN_PASSWORD
docker compose up --build
```

- Admin: http://127.0.0.1:8000/admin/
- Apt: http://127.0.0.1:8000/apt/
- If `PRVAPT_ADMIN_PASSWORD` is empty, the generated password is written to `data/admin-bootstrap.txt` (mode 0600). Change it on first login.

Put Caddy / Traefik / host nginx in front of that port if you need HTTPS.

## Without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export PRVAPT_DATA_DIR=$PWD/data
export PRVAPT_ADMIN_PASSWORD=change-me-now
export PRVAPT_PUBLIC_URL=http://127.0.0.1:8000
.venv/bin/uvicorn prvaptmirror.main:app --host 127.0.0.1 --port 8000 --workers 1
```

The app serves both `/admin/` and `/apt/`.

## Client setup

Copy the snippet from the admin **Client setup** page. It installs the ASCII-armored key under `/etc/apt/keyrings` and a deb822 source with `Signed-By`. Do not use `apt-key` or `trusted=yes`.

## Tests

```bash
.venv/bin/pytest -q
```

Optional official-apt client: `tests/integration/test_apt_client.sh` (needs Docker and a running instance).

## Backup

```bash
docker compose exec app /app/scripts/backup.sh /tmp/prvapt-backup
```

`scripts/backup.sh` needs `sqlite3` (installed in the image) and dumps `data.sqlite` plus `repo/pool` plus `gnupg/`. Restore stops the app first (`scripts/restore.sh`).
