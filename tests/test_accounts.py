import hashlib
import io
import json
import os
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import app
from account_store import postgres_tls_options


PASSWORD = "test-long-password-0123"


def price_file(name="Товар первого прайса", price="12,50"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Прайс"])
    sheet.append(["Наименование", "Заказ↓", "Ед. изм.", "Цена", "Сумма"])
    sheet.append([name, None, "шт", price, None])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.history_dir = str(Path(self.directory.name) / "history")
        self.environment = patch.dict(os.environ, {
            "DATA_DIR": str(Path(self.directory.name) / "catalog"),
            "INITIAL_CATALOG_FILE": "", "ADMIN_TOKEN": "test-owner-password",
            "ORDERS_DATABASE_URL": "", "ORDERS_HISTORY_DIR": self.history_dir,
            "SESSION_COOKIE_SECURE": "0", "SMTP_HOST": "", "SMTP_FROM": "",
            "SMTP_USER": "", "SMTP_PASSWORD": "", "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls", "ORDERS_TO": "",
        })
        self.environment.start()
        self.client = TestClient(app)
        self.other = TestClient(app)
        self.catalog = self.upload()

    def tearDown(self):
        self.client.close()
        self.other.close()
        self.environment.stop()
        self.directory.cleanup()

    def upload(self, name="Товар первого прайса", price="12,50"):
        response = self.client.post("/api/catalog/import", headers={"X-Admin-Token": "test-owner-password"},
                                    files={"file": ("price.xlsx", price_file(name, price))})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def register(self, client=None, email="buyer-a@example.com"):
        client = client or self.client
        response = client.post("/api/accounts/register", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def order(self, quantity=2):
        return {"catalog_version": self.catalog["catalog_version"], "customer": {},
                "items": [{"product_id": self.catalog["products"][0]["id"], "quantity": quantity}]}

    def exported(self, client=None, quantity=2):
        response = (client or self.client).post("/api/orders/export", json=self.order(quantity))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-order-saved"], "true")
        return response

    def test_disabled_history_is_honest_and_legacy_checkout_remains_anonymous(self):
        with patch.dict(os.environ, {"ORDERS_HISTORY_DIR": ""}):
            self.assertEqual(self.client.get("/api/accounts/me").json(), {"available": False, "user": None})
            self.assertFalse(self.client.get("/api/config").json()["history_ready"])
            credentials = {"email": "buyer@example.com", "password": PASSWORD}
            for route in ("register", "login"):
                self.assertEqual(self.client.post(f"/api/accounts/{route}", json=credentials).status_code, 503)
            self.assertEqual(self.client.get("/api/orders/history").status_code, 503)
            response = self.client.post("/api/orders/export", json=self.order())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-order-saved"], "false")

    def test_configured_history_requires_login_before_checkout(self):
        self.assertTrue(self.client.get("/api/config").json()["history_ready"])
        self.assertEqual(self.client.get("/api/accounts/me").json(), {"available": True, "user": None})
        for endpoint in ("/api/orders/export", "/api/orders/send"):
            self.assertEqual(self.client.post(endpoint, json=self.order()).status_code, 401)
        self.assertEqual(self.client.get("/api/orders/history").status_code, 401)

    def test_password_and_session_are_hashed_cookie_is_private_and_email_normalized(self):
        response = self.client.post("/api/accounts/register", json={"email": " Buyer-A@Example.COM ", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        user = response.json()["user"]
        self.assertEqual(user["email"], "buyer-a@example.com")
        self.assertEqual(set(user), {"id", "email"})
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        self.assertIn("Max-Age=2592000", response.headers["set-cookie"])
        with sqlite3.connect(Path(self.history_dir) / "accounts.sqlite3") as connection:
            stored_password = connection.execute("SELECT password_hash FROM users").fetchone()[0]
            stored_token = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertNotIn(PASSWORD, stored_password)
        self.assertTrue(stored_password.startswith("pbkdf2_sha256$600000$"))
        token = self.client.cookies.get("okunev_session")
        self.assertNotEqual(stored_token, token)
        self.assertEqual(stored_token, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.client.get("/api/accounts/me").json()["user"], user)

    def test_secure_cookie_setting_for_https(self):
        with patch.dict(os.environ, {"SESSION_COOKIE_SECURE": "1"}), TestClient(app, base_url="https://testserver") as client:
            response = client.post("/api/accounts/register", json={"email": "secure@example.com", "password": PASSWORD})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Secure", response.headers["set-cookie"])
            self.assertTrue(client.get("/api/accounts/me").json()["user"])

    def test_wrong_password_unknown_account_and_duplicate_registration(self):
        self.register()
        duplicate = self.other.post("/api/accounts/register", json={"email": "BUYER-A@EXAMPLE.COM", "password": PASSWORD})
        self.assertEqual(duplicate.status_code, 409)
        responses = []
        for email in ("buyer-a@example.com", "unknown@example.com"):
            response = self.other.post("/api/accounts/login", json={"email": email, "password": "wrong-password"})
            self.assertEqual(response.status_code, 401)
            responses.append(response.json())
        self.assertEqual(responses[0], responses[1])
        self.assertIsNone(self.other.get("/api/accounts/me").json()["user"])
        for credentials in ({"email": "invalid", "password": PASSWORD},
                            {"email": "valid@example.com", "password": "short"}):
            self.assertEqual(self.other.post("/api/accounts/register", json=credentials).status_code, 422)

    def test_accounts_cannot_list_or_download_each_others_orders(self):
        user_a = self.register()
        exported_a = self.exported()
        id_a = exported_a.headers["x-order-id"]
        self.register(self.other, "buyer-b@example.com")
        self.assertEqual(self.other.get("/api/orders/history").json()["orders"], [])
        self.assertEqual(self.other.get(f"/api/orders/history/{id_a}/file").status_code, 404)
        exported_b = self.exported(self.other, 3)
        id_b = exported_b.headers["x-order-id"]
        history_a = self.client.get("/api/orders/history").json()
        history_b = self.other.get(f"/api/orders/history?user_id={user_a['id']}").json()
        self.assertEqual([order["id"] for order in history_a["orders"]], [id_a])
        self.assertEqual([order["id"] for order in history_b["orders"]], [id_b])
        self.assertEqual(history_a["orders"][0]["positions"], [{"name": "Товар первого прайса", "unit": "шт",
                          "price": "12.50", "quantity": 2, "line_total": "25.00"}])
        self.assertEqual(history_a["orders"][0]["status"], "exported")
        self.assertEqual(history_a["orders"][0]["customer"], {"name": "", "contact": "", "comment": ""})
        self.assertEqual(self.client.get(f"/api/orders/history/{id_b}/file").status_code, 404)
        self.assertEqual(self.client.get(f"/api/orders/history/{id_a}/file").content, exported_a.content)
        injected = self.order()
        injected["user_id"] = user_a["id"]
        self.assertEqual(self.other.post("/api/orders/export", json=injected).status_code, 422)

    def test_history_and_original_excel_survive_new_process_and_login(self):
        user = self.register()
        exported = self.exported()
        order_id = exported.headers["x-order-id"]
        code = """
import hashlib,json,sys
from fastapi.testclient import TestClient
from app import app
data=json.load(sys.stdin)
with TestClient(app) as client:
    login=client.post('/api/accounts/login',json={'email':data['email'],'password':data['password']})
    assert login.status_code==200
    history=client.get('/api/orders/history').json()
    document=client.get('/api/orders/history/'+data['order_id']+'/file')
    assert document.status_code==200
    print(json.dumps({'user':login.json()['user'],'ids':[order['id'] for order in history['orders']],
                      'file_hash':hashlib.sha256(document.content).hexdigest()}))
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                input=json.dumps({"email": user["email"], "password": PASSWORD, "order_id": order_id}),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        restarted = json.loads(result.stdout)
        self.assertEqual(restarted["user"], user)
        self.assertEqual(restarted["ids"], [order_id])
        self.assertEqual(restarted["file_hash"], hashlib.sha256(exported.content).hexdigest())

    def test_replacing_catalog_never_changes_old_history_or_file(self):
        self.register()
        exported = self.exported()
        order_id = exported.headers["x-order-id"]
        old_order = self.order()
        self.upload("Совсем новый товар", "99,99")
        self.assertEqual(self.client.post("/api/orders/export", json=old_order).status_code, 409)
        restored = self.client.get(f"/api/orders/history/{order_id}/file")
        self.assertEqual(restored.content, exported.content)
        workbook = load_workbook(io.BytesIO(restored.content), data_only=True)
        self.assertEqual(workbook["Заказ"]["A3"].value, "Товар первого прайса")
        self.assertEqual(workbook["Заказ"]["D3"].value, 12.5)
        self.assertEqual(self.client.get("/api/orders/history").json()["orders"][0]["total"], "25.00")

    def test_logout_revokes_copied_token_and_expired_sessions_fail(self):
        self.register()
        token = self.client.cookies.get("okunev_session")
        self.assertEqual(self.client.post("/api/accounts/logout").status_code, 200)
        self.other.cookies.set("okunev_session", token)
        self.assertIsNone(self.other.get("/api/accounts/me").json()["user"])
        self.assertEqual(self.other.get("/api/orders/history").status_code, 401)
        response = self.client.post("/api/accounts/login", json={"email": "buyer-a@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(Path(self.history_dir) / "accounts.sqlite3") as connection:
            connection.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
        self.assertIsNone(self.client.get("/api/accounts/me").json()["user"])
        self.assertEqual(self.client.get("/api/orders/history").status_code, 401)

    def test_history_pagination_is_bounded_and_complete(self):
        self.register()
        ids = {self.exported(quantity=quantity).headers["x-order-id"] for quantity in (1, 2, 3)}
        first = self.client.get("/api/orders/history?limit=2").json()
        second = self.client.get("/api/orders/history?limit=2&offset=2").json()
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["next_offset"], 2)
        self.assertIsNone(second["next_offset"])
        self.assertEqual({order["id"] for order in first["orders"] + second["orders"]}, ids)
        self.assertEqual(self.client.get("/api/orders/history?limit=101").status_code, 422)
        self.assertEqual(self.client.get("/api/orders/history?offset=-1").status_code, 422)

    def test_successful_smtp_history_contains_exact_sent_attachment(self):
        self.register()
        settings = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "sender@example.com", "ORDERS_TO": "owner@example.com"}
        with patch.dict(os.environ, settings), patch("app.smtplib.SMTP", autospec=True) as smtp:
            connection = smtp.return_value.__enter__.return_value
            connection.send_message.return_value = {}
            response = self.client.post("/api/orders/send", json=self.order())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(set(response.json()), {"ok", "order_id"})
            attached = list(connection.send_message.call_args.args[0].iter_attachments())[0].get_payload(decode=True)
        order_id = response.json()["order_id"]
        history = self.client.get("/api/orders/history").json()["orders"]
        self.assertEqual(history[0]["status"], "sent")
        self.assertEqual(self.client.get(f"/api/orders/history/{order_id}/file").content, attached)

    def test_failed_smtp_history_keeps_downloadable_order_and_failed_status(self):
        self.register()
        settings = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "sender@example.com", "ORDERS_TO": "owner@example.com"}
        with patch.dict(os.environ, settings), patch("app.smtplib.SMTP", side_effect=smtplib.SMTPException("failure")):
            response = self.client.post("/api/orders/send", json=self.order())
        self.assertEqual(response.status_code, 502)
        history = self.client.get("/api/orders/history").json()["orders"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "failed")
        document = self.client.get(f"/api/orders/history/{history[0]['id']}/file")
        self.assertEqual(document.status_code, 200)
        self.assertEqual(load_workbook(io.BytesIO(document.content))["Заказ"]["B3"].value, 2)

    def test_unconfigured_smtp_is_failed_history_not_fake_sent_success(self):
        self.register()
        self.assertEqual(self.client.post("/api/orders/send", json=self.order()).status_code, 503)
        self.assertEqual(self.client.get("/api/orders/history").json()["orders"][0]["status"], "failed")

    def test_unavailable_storage_never_returns_fake_saved_excel(self):
        self.register()
        with patch("account_store.save_order", side_effect=OSError("storage unavailable")):
            response = self.client.post("/api/orders/export", json=self.order())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get("/api/orders/history").json()["orders"], [])


class PostgresTransportTests(unittest.TestCase):
    def test_external_render_requires_tls_and_preserves_explicit_verification(self):
        external = "postgresql://test-user@dpg-test.oregon-postgres.render.com/catalog"
        self.assertEqual(postgres_tls_options(external), {"sslmode": "require"})
        self.assertEqual(postgres_tls_options(external + "?sslmode=verify-full"), {})
        self.assertEqual(postgres_tls_options("postgresql://test-user@127.0.0.1/catalog"), {})


if __name__ == "__main__":
    unittest.main()
