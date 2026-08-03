from __future__ import annotations

import re
import os
import tempfile
import unittest
from pathlib import Path

from argon2 import PasswordHasher

from app import create_app


ORIGIN = "https://admin.example.test"


class AdminWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        release_root = root / "lmde" / "dists" / "lmde7"
        release_root.mkdir(parents=True)
        (release_root / "InRelease").write_text("signed\n", encoding="utf-8")
        (root / "repository-key.gpg").write_bytes(b"public-key")

        password_hasher = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
        self.password = "correct horse battery staple"
        app = create_app(
            {
                "TESTING": True,
                "ADMIN_PUBLIC_ORIGIN": ORIGIN,
                "ADMIN_PASSWORD_HASH_FOR_TESTS": password_hasher.hash(self.password),
                "ADMIN_USERNAME": "admin",
                "ADMIN_SESSION_TTL": 300,
                "ADMIN_LOGIN_MAX_FAILURES": 5,
                "ADMIN_LOGIN_WINDOW": 900,
                "REPOSITORY_ROOT": str(root),
            }
        )
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def csrf_from(response) -> str:
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if match is None:
            raise AssertionError("response did not contain a CSRF token")
        return match.group(1).decode("ascii")

    def login(self, password: str | None = None, origin: str = ORIGIN):
        page = self.client.get("/login", base_url=ORIGIN)
        token = self.csrf_from(page)
        return self.client.post(
            "/login",
            base_url=ORIGIN,
            headers={"Origin": origin},
            data={
                "username": "admin",
                "password": password if password is not None else self.password,
                "csrf_token": token,
            },
        )

    def test_health_is_public_and_hardened(self) -> None:
        response = self.client.get("/healthz", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"ok\n")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_dashboard_requires_login(self) -> None:
        response = self.client.get("/", base_url=ORIGIN)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/login")
        self.assertEqual(self.client.get("/api/status", base_url=ORIGIN).status_code, 303)

    def test_forwarded_admin_prefix_is_used_for_urls_and_cookies(self) -> None:
        headers = {"X-Forwarded-Prefix": "/admin"}
        login = self.client.get("/login", base_url=ORIGIN, headers=headers)
        self.assertEqual(login.status_code, 200)
        self.assertIn(b'action="/admin/login"', login.data)
        self.assertIn(b'href="/admin/"', login.data)
        self.assertIn("Path=/admin/login", login.headers["Set-Cookie"])

        dashboard = self.client.get("/", base_url=ORIGIN, headers=headers)
        self.assertEqual(dashboard.status_code, 303)
        self.assertEqual(dashboard.headers["Location"], "/admin/login")

    def test_login_rejects_missing_csrf(self) -> None:
        response = self.client.post(
            "/login",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data={"username": "admin", "password": self.password},
        )
        self.assertEqual(response.status_code, 400)

    def test_login_rejects_wrong_origin(self) -> None:
        response = self.login(origin="https://attacker.example")
        self.assertEqual(response.status_code, 403)

    def test_login_rejects_wrong_password(self) -> None:
        response = self.login(password="incorrect password")
        self.assertEqual(response.status_code, 401)
        self.assertIn("账号或密码不正确".encode(), response.data)

    def test_login_rate_limit(self) -> None:
        for _ in range(5):
            self.assertEqual(self.login(password="incorrect password").status_code, 401)
        response = self.login(password="incorrect password")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_login_dashboard_api_and_logout(self) -> None:
        response = self.login()
        self.assertEqual(response.status_code, 303)
        self.assertIn("__Host-prvaptmirror_admin_session=", response.headers["Set-Cookie"])
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])

        dashboard = self.client.get("/", base_url=ORIGIN)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"lmde/lmde7", dashboard.data)
        self.assertIn(b"Content-Security-Policy", str(dashboard.headers).encode())

        status = self.client.get("/api/status", base_url=ORIGIN)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["repository_count"], 1)
        self.assertTrue(status.json["public_key_ready"])

        csrf_token = self.csrf_from(dashboard)
        logout = self.client.post(
            "/logout",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data={"csrf_token": csrf_token},
        )
        self.assertEqual(logout.status_code, 303)
        self.assertEqual(self.client.get("/", base_url=ORIGIN).status_code, 303)

    def test_login_rejects_oversized_request(self) -> None:
        response = self.client.post(
            "/login",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
            data="password=" + ("x" * 70000),
        )
        self.assertEqual(response.status_code, 413)

    def test_logout_rejects_missing_csrf(self) -> None:
        self.assertEqual(self.login().status_code, 303)
        response = self.client.post(
            "/logout",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/", base_url=ORIGIN).status_code, 200)

    def test_password_hash_file_rejects_broad_permissions(self) -> None:
        auth_root = Path(self.temp_dir.name) / "auth"
        auth_root.mkdir(mode=0o700)
        secret_path = auth_root / "password-hash"
        secret_path.write_text(
            PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(self.password),
            encoding="utf-8",
        )
        os.chmod(secret_path, 0o644)
        with self.assertRaisesRegex(RuntimeError, "权限"):
            create_app(
                {
                    "TESTING": True,
                    "ADMIN_PUBLIC_ORIGIN": ORIGIN,
                    "ADMIN_AUTH_ROOT": str(auth_root),
                    "REPOSITORY_ROOT": self.temp_dir.name,
                }
            )


class AdminSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.auth_root = root / "auth"
        self.auth_root.mkdir(mode=0o700)
        self.audit_root = root / "audit"
        self.audit_root.mkdir(mode=0o700)
        self.setup_token = "a" * 64
        token_path = self.auth_root / "setup-token"
        token_path.write_text(self.setup_token + "\n", encoding="utf-8")
        os.chmod(token_path, 0o600)
        self.password = "correct horse battery staple"
        app = create_app(
            {
                "TESTING": True,
                "ADMIN_PUBLIC_ORIGIN": ORIGIN,
                "ADMIN_AUTH_ROOT": str(self.auth_root),
                "ADMIN_AUDIT_ROOT": str(self.audit_root),
                "ADMIN_USERNAME": "admin",
                "ADMIN_SESSION_TTL": 300,
                "ADMIN_LOGIN_MAX_FAILURES": 5,
                "ADMIN_LOGIN_WINDOW": 900,
                "ADMIN_SETUP_MAX_FAILURES": 3,
                "REPOSITORY_ROOT": str(root),
            }
        )
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def csrf_from(response) -> str:
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if match is None:
            raise AssertionError("response did not contain a CSRF token")
        return match.group(1).decode("ascii")

    def submit_setup(
        self,
        *,
        token: str | None = None,
        password: str | None = None,
        confirmation: str | None = None,
        origin: str = ORIGIN,
    ):
        page = self.client.get("/setup", base_url=ORIGIN)
        csrf_token = self.csrf_from(page)
        selected_password = password if password is not None else self.password
        return self.client.post(
            "/setup",
            base_url=ORIGIN,
            headers={"Origin": origin},
            data={
                "setup_token": token if token is not None else self.setup_token,
                "password": selected_password,
                "password_confirmation": (
                    confirmation if confirmation is not None else selected_password
                ),
                "csrf_token": csrf_token,
            },
        )

    def login(self):
        page = self.client.get("/login", base_url=ORIGIN)
        csrf_token = self.csrf_from(page)
        return self.client.post(
            "/login",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data={
                "username": "admin",
                "password": self.password,
                "csrf_token": csrf_token,
            },
        )

    def test_uninitialized_application_only_allows_setup(self) -> None:
        dashboard = self.client.get("/", base_url=ORIGIN)
        self.assertEqual(dashboard.status_code, 303)
        self.assertEqual(dashboard.headers["Location"], "/setup")
        login = self.client.get("/login", base_url=ORIGIN)
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["Location"], "/setup")
        self.assertEqual(self.client.get("/setup", base_url=ORIGIN).status_code, 200)

    def test_setup_rejects_wrong_origin_and_missing_csrf(self) -> None:
        self.assertEqual(
            self.submit_setup(origin="https://attacker.example").status_code, 403
        )
        response = self.client.post(
            "/setup",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data={
                "setup_token": self.setup_token,
                "password": self.password,
                "password_confirmation": self.password,
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_setup_rejects_wrong_token_and_rate_limits(self) -> None:
        for _ in range(3):
            self.assertEqual(self.submit_setup(token="b" * 64).status_code, 401)
        response = self.submit_setup(token="b" * 64)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        self.assertFalse((self.auth_root / "password-hash").exists())

    def test_setup_validates_password(self) -> None:
        mismatch = self.submit_setup(confirmation="different password value")
        self.assertEqual(mismatch.status_code, 400)
        short = self.submit_setup(password="too short", confirmation="too short")
        self.assertEqual(short.status_code, 400)
        self.assertTrue((self.auth_root / "setup-token").exists())

    def test_setup_is_one_time_and_new_password_can_login(self) -> None:
        response = self.submit_setup()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/login?configured=1")
        password_hash = self.auth_root / "password-hash"
        self.assertTrue(password_hash.read_text(encoding="utf-8").startswith("$argon2id$"))
        self.assertEqual(os.stat(password_hash).st_mode & 0o777, 0o600)
        self.assertFalse((self.auth_root / "setup-token").exists())
        self.assertIn(
            '"action":"admin.setup"',
            (self.audit_root / "events.jsonl").read_text(encoding="utf-8"),
        )
        self.assertEqual(self.client.get("/setup", base_url=ORIGIN).status_code, 404)
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.client.get("/", base_url=ORIGIN).status_code, 200)


if __name__ == "__main__":
    unittest.main()
