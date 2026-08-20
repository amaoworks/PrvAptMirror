"""Client setup snippets. Never include trusted=yes / apt-key / trusted.gpg.d."""

from __future__ import annotations

from prvaptmirror.config import Config


def deb822_snippet(cfg: Config) -> str:
    base = cfg.apt_base()
    return f"""sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL {base}/pubkey.asc \\
  -o /etc/apt/keyrings/prvaptmirror.asc
sudo tee /etc/apt/sources.list.d/prvaptmirror.sources >/dev/null <<'EOF'
Types: deb
URIs: {base}
Suites: {cfg.suite}
Components: {cfg.component}
Signed-By: /etc/apt/keyrings/prvaptmirror.asc
EOF
sudo apt update
"""


def oneline_snippet(cfg: Config) -> str:
    base = cfg.apt_base()
    return f"""sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL {base}/pubkey.asc \\
  -o /etc/apt/keyrings/prvaptmirror.asc
sudo gpg --dearmor -o /etc/apt/keyrings/prvaptmirror.gpg \\
  /etc/apt/keyrings/prvaptmirror.asc
sudo tee /etc/apt/sources.list.d/prvaptmirror.list >/dev/null <<'EOF'
deb [signed-by=/etc/apt/keyrings/prvaptmirror.gpg] {base} {cfg.suite} {cfg.component}
EOF
sudo apt update
"""


def forbidden_in_snippet(text: str) -> bool:
    lowered = text.lower()
    return (
        "trusted=yes" in lowered
        or "apt-key add" in lowered
        or "/etc/apt/trusted.gpg.d/" in lowered
    )
