# PrvAptMirror

**English** | [中文](README_zh.md)

Personal signed apt repository with a password-protected admin UI. Upload `.deb` files that have no public source, then add the repo to Debian or Ubuntu with `Signed-By` (never `trusted=yes`).

The process **only exposes one HTTP port**. TLS, domain names, and access control belong on the host reverse proxy.

## One-click start

With Docker, `--docker` pulls the published GHCR image by default; `--dev` uses local uvicorn. Origin checks are off by default.

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
| `--build` | Build the Docker image from the current source instead of pulling GHCR |
| `-P` / `--password` | First-start admin password; never overrides later UI changes |
| `--origin-check` | Enable address verification (off by default) |

Manual Compose:

```bash
mkdir -p data && sudo chown -R 1000:1000 data
cp .env.example .env
docker compose pull
docker compose up -d
```

`.env.example` uses `ghcr.io/amaoworks/prvaptmirror:latest`. To upgrade, run `docker compose pull` followed by `docker compose up -d`; no image version edit is needed. To build locally instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

- Admin: http://127.0.0.1:8000/admin/
- Apt: http://127.0.0.1:8000/apt/
- When no initial password is specified, the generated password is written to `data/admin-bootstrap.txt` (mode 0600) and must be changed on first login.

Stop an existing instance before migrating data and recursively fix ownership of the entire data directory. Startup checks that index directories are writable. The container runs as `1000:1000`.

Compose uses `restart: unless-stopped` to recover after a process exit or Docker restart, except for manually stopped containers. Its health check uses `/readyz`, which returns 503 when indexes are missing, changes are unpublished, or the latest publish did not succeed. `/healthz` only checks process/database access. Docker does not restart a container merely because it is unhealthy; monitor persistent failures.

Put Caddy / Traefik / host nginx in front of that port if you need HTTPS.

## Web settings

After login, use **Settings** to manage the public URL, Suite, Codename, Component, architectures, Origin, Label, upload limits, and new-session lifetime. On the first start, environment-backed application values only seed the database; saved web settings are authoritative afterward. Repository metadata changes rebuild and re-sign the indexes with rollback protection; an interrupted change is reconciled on the next start.

The image version, host data directory, bind address, port, trusted proxies, and secrets remain deployment settings. `.env.example` therefore contains only four deployment values.

## Automatic package sources

The **Sources** page configures automatic downloads without adding another container or configuration file. The scheduler runs inside the single Uvicorn process and stores source definitions, schedules, run history, and discovered artifacts in `data.sqlite`.

Two source types are currently supported:

- **GitHub Releases** — enter `owner/repository`, an asset filename regular expression, the number of recent releases to inspect, whether prereleases are included, and the polling interval. Only matching `.deb` assets are downloaded.
- **Direct URL or HTTP directory** — periodically fetch one `.deb` URL, or enumerate a same-origin HTML directory whose URL ends in `/`. Directory sources apply the asset regex and use Debian version ordering to select the newest `package_version_arch.deb` for each package and architecture. Fixed files use `ETag` and `Last-Modified` when available, and content hashes prevent duplicate imports.

Each source can be enabled or disabled, edited, deleted, or queued for an immediate synchronization from the web UI. Downloads are streamed into `data/incoming`, validated as Debian packages, imported in one batch, and then published and signed once. A matching package is skipped; the same Package/Version/Architecture with different content is reported as a conflict. A manually queued synchronization retries selected rejected/conflicting assets, while scheduled checks skip those terminal records. Failed checks retry with exponential backoff up to the configured interval.

When adding or editing a GitHub or HTTP-directory source, **Test fetch and regex** reads remote metadata without saving the source and shows why each asset would be selected or ignored. For a directory, it also probes the selected URL and verifies the Debian ar magic before reporting that the package can be imported.

An optional GitHub token can be entered on the source page for private repositories or higher API limits. It is encrypted before being stored and is never displayed again. The encryption key is derived from `data/secret-key`, so that file must remain with `data.sqlite` when moving or restoring an installation.

If packages are saved but publishing fails, the UI reports it explicitly. The scheduler retries before subsequent synchronizations or during idle polling (every 30 seconds by default), even without configured sources. Re-uploading or re-downloading packages is unnecessary. Initialization failures, including token decryption errors, finish the run as failed so it can be queued again after correcting the configuration.

The existing bind mount persists everything across image upgrades and container recreation:

```text
./data:/var/lib/prvaptmirror
```

No worker image or worker service is built: the web server, scheduler, downloader, package importer, and APT publisher all run in the one `app` container.

To recover a forgotten administrator password from an interactive terminal:

```bash
docker compose exec app prvaptmirror-admin reset-password --username admin
```

## Container releases

Pushing to `main` publishes `edge` and `sha-*` images. Pushing a semantic Git tag publishes versioned images and updates `latest`. After tests and image publishing succeed, the workflow creates a GitHub Release using `.github/release-notes/<tag>.md` when available, or generated notes otherwise. Deployments use `latest` by default; versioned tags remain available for selecting a specific release or rolling back. Released full-version tags must not be overwritten.

## Without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export PRVAPT_DATA_DIR=$PWD/data
.venv/bin/uvicorn prvaptmirror.main:app --host 127.0.0.1 --port 8000 --workers 1
```

The app serves both `/admin/` and `/apt/`.

## Client setup

Indexes support SHA256 By-Hash. Publishing retains hash-addressed indexes for seven days and at least two previous versions of each current index, so clients can finish requests across a publish. Upgrading an older installation rebuilds indexes at startup. This retention covers indexes only: explicitly deleting a package can still make downloads referenced by an old index return 404.

Copy the snippet from the admin **Client setup** page. It installs the ASCII-armored key under `/etc/apt/keyrings` and a deb822 source with `Signed-By`. Do not use `apt-key` or `trusted=yes`.

## Tests

```bash
.venv/bin/pytest -q
```

Optional official-apt client: `tests/integration/test_apt_client.sh` (needs Docker and a ready instance). It forces By-Hash and fails on index download errors. Set `APT_TEST_SUITE` / `APT_TEST_COMPONENT` if you changed those repository settings.

## Backup and restore

Store backups under the bind-mounted `data/backups` so they survive container recreation. Use a new destination each time. Uploads, deletions, and publishing wait for the backup lock; HTTP reads remain available.

```bash
backup_name="prvapt-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose exec -T app /app/scripts/backup.sh "/var/lib/prvaptmirror/backups/$backup_name"
# Export another copy for separate or off-host storage.
mkdir -p backups
docker compose cp "app:/var/lib/prvaptmirror/backups/$backup_name" ./backups/
```

The script uses the Python standard library to snapshot `data.sqlite`, `repo/pool`, `gnupg/`, and `secret-key` under the publish lock and verify package hashes. Only a completed backup appears at the destination. SHA-256 checksums cover the database and archive. Backups contain signing keys and source-credential encryption keys; directories use mode 0700 and files 0600.

Run restoration from the project directory on a host with Python 3.11+ and Docker Compose:

```bash
sudo ./scripts/restore.sh "./backups/$backup_name"
docker compose up -d
```

Restore resolves the actual data bind mount from Compose and stops `app`; a failed stop aborts restoration. It validates the backup, prepares a clean sibling directory without stale indexes or WAL files, marks the repository for rebuilding, restores UID/GID 1000 ownership, and atomically exchanges the directories. The previous data is retained at the printed `.data.before-restore-*` path. Archive or remove it after confirming recovery. Allow disk space for both datasets. Startup rebuilds and signs the indexes; do not start another instance during restoration.

If you used `scripts/start.sh` with `.env.runtime`, add `--compose-env-file .env.runtime` when restoring. For native deployments, stop the process first and explicitly pass `--offline --data-dir /actual/data --uid USER_UID --gid USER_GID`. Running instances of the updated application hold a service lock that prevents restore.

Failed database imports roll back newly created package files. Startup moves unregistered crash leftovers to `data/quarantine` for inspection; the original package can be uploaded again. Upload authentication runs before receiving files, which are copied in chunks. Compressed and decompressed control archives are each limited to 16 MiB, control text to 1 MiB, and decoder windows/memory to 64 MiB.
