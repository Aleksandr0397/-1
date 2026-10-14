"""Account sessions and immutable orders in an explicitly configured store.

PostgreSQL objects live exclusively in the fixed okunev_orders schema. SQLite
history is opt-in for local development, never an automatic hosting fallback.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from db_initialization import run_once

SCHEMA = "okunev_orders"
SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000
TABLES = "catalog_meta|catalog_archive|products|users|account_profiles|account_carts|sessions|orders"


class DuplicateAccount(ValueError):
    pass


class InvalidSession(ValueError):
    pass


class IncorrectPassword(ValueError):
    pass


class CartConflict(ValueError):
    def __init__(self, state):
        super().__init__("Cart changed on another device")
        self.state = state


def postgres_configured():
    return bool(os.environ.get("ORDERS_DATABASE_URL", "").strip())


def available():
    return postgres_configured() or bool(os.environ.get("ORDERS_HISTORY_DIR", "").strip())


class PostgresConnection:
    """Small adapter for the application's existing parameterized SQLite SQL."""

    def __init__(self, connection):
        self.connection = connection

    def sql(self, statement):
        statement = re.sub(rf"\b({TABLES})\b", lambda match: f"{SCHEMA}.{match[0]}", statement)
        return statement.replace("?", "%s")

    def execute(self, statement, parameters=()):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            self.connection.execute("BEGIN")
            # Serialize catalog bootstrap/replacement across workers without
            # touching or locking unrelated application tables.
            return self.connection.execute("SELECT pg_advisory_xact_lock(4946450719142991)")
        if statement.strip().upper() == "BEGIN":
            return self.connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        return self.connection.execute(self.sql(statement), parameters)

    def executemany(self, statement, parameters):
        cursor = self.connection.cursor()
        cursor.executemany(self.sql(statement), parameters)
        return cursor

    def executescript(self, statements):
        for statement in statements.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        self.rollback() if error_type else self.commit()


@contextmanager
def postgres_database():
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ["ORDERS_DATABASE_URL"].strip()
    raw = psycopg.connect(dsn, autocommit=True, connect_timeout=10, row_factory=dict_row,
                          **postgres_tls_options(dsn))
    connection = PostgresConnection(raw)
    try:
        def initialize_schema():
            # All later table references are schema-qualified by the adapter.
            connection.execute("BEGIN IMMEDIATE")
            raw.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            connection.commit()
        run_once(dsn, "schema", initialize_schema)
        yield connection
    finally:
        connection.close()


def postgres_tls_options(dsn):
    from psycopg.conninfo import conninfo_to_dict

    parameters = conninfo_to_dict(dsn)
    hosts = parameters.get("host", "").split(",")
    if "sslmode" not in parameters and any(host.lower().endswith(".render.com") for host in hosts):
        return {"sslmode": "require"}
    return {}


@contextmanager
def database():
    if not available():
        raise RuntimeError("Persistent order history is not configured")
    if postgres_configured():
        with postgres_database() as connection:
            run_once(os.environ["ORDERS_DATABASE_URL"].strip(), "accounts", lambda: initialize(connection, "BYTEA"))
            yield connection
    else:
        directory = Path(os.environ["ORDERS_HISTORY_DIR"].strip())
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "accounts.sqlite3", timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            initialize(connection, "BLOB")
            yield connection
        finally:
            connection.close()


def initialize(connection, binary_type):
    if isinstance(connection, PostgresConnection):
        connection.execute("BEGIN IMMEDIATE")
    connection.executescript(f"""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_profiles (
            user_id TEXT PRIMARY KEY REFERENCES users(id),
            name TEXT NOT NULL, phone TEXT NOT NULL,
            company TEXT NOT NULL, delivery_address TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_carts (
            user_id TEXT PRIMARY KEY REFERENCES users(id), revision BIGINT NOT NULL,
            document TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            expires_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL, filename TEXT NOT NULL, status TEXT NOT NULL,
            total TEXT NOT NULL, positions TEXT NOT NULL, customer TEXT NOT NULL,
            document {binary_type} NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_owner_created ON orders (user_id, created_at);
    """)
    connection.commit()


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password, stored):
    try:
        algorithm, iterations, salt, digest = stored.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return secrets.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(connection, user_id):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    connection.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (token_hash(token), user_id, now + SESSION_SECONDS))
    return token


def register(email, password):
    user = {"id": str(uuid.uuid4()), "email": email}
    hashed = password_hash(password)
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("INSERT INTO users VALUES (?, ?, ?, ?) ON CONFLICT (email) DO NOTHING",
                                    (user["id"], email, hashed, datetime.now(timezone.utc).isoformat()))
        if cursor.rowcount != 1:
            connection.rollback()
            raise DuplicateAccount("Account already exists")
        token = new_session(connection, user["id"])
        connection.commit()
    return user, token


def login(email, password):
    with database() as connection:
        # Password verification and session creation must serialize with changes
        # so a login using an old hash cannot issue a session after revocation.
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute("SELECT id, email, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if user is None:
            # Match the normal password hashing cost even for an unknown email.
            hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"unknown-account!", PASSWORD_ITERATIONS)
            connection.rollback()
            return None
        if not password_matches(password, user["password_hash"]):
            connection.rollback()
            return None
        token = new_session(connection, user["id"])
        connection.commit()
        return {"id": user["id"], "email": user["email"]}, token


def change_password(user_id, session_token, current_password, new_password):
    if not session_token or len(session_token) > 256:
        raise InvalidSession("Session is no longer valid")
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute("""
            SELECT users.id, users.email, users.password_hash FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.user_id = ? AND sessions.expires_at > ?
        """, (token_hash(session_token), user_id, int(time.time()))).fetchone()
        if user is None:
            connection.rollback()
            raise InvalidSession("Session is no longer valid")
        if not password_matches(current_password, user["password_hash"]):
            connection.rollback()
            raise IncorrectPassword("Current password is incorrect")
        connection.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash(new_password), user_id))
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        token = new_session(connection, user_id)
        connection.commit()
        return {"id": user["id"], "email": user["email"]}, token


def user_for_session(token):
    if not token or len(token) > 256:
        return None
    with database() as connection:
        row = connection.execute("""
            SELECT users.id, users.email FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
        """, (token_hash(token), int(time.time()))).fetchone()
    return dict(row) if row else None


def logout(token):
    if not token or len(token) > 256:
        return
    with database() as connection:
        # Coordinate revocation with login and password changes on both stores.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        connection.commit()


def profile(user):
    with database() as connection:
        row = connection.execute("""
            SELECT name, phone, company, delivery_address FROM account_profiles WHERE user_id = ?
        """, (user["id"],)).fetchone()
    details = dict(row) if row else {"name": "", "phone": "", "company": "", "delivery_address": ""}
    return {"email": user["email"], **details}


def save_profile(user, details):
    with database() as connection:
        connection.execute("""
            INSERT INTO account_profiles (user_id, name, phone, company, delivery_address)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET name = excluded.name, phone = excluded.phone,
                company = excluded.company, delivery_address = excluded.delivery_address
        """, (user["id"], details["name"], details["phone"], details["company"], details["delivery_address"]))
        connection.commit()
    return {"email": user["email"], **details}


def require_live_session(connection, user_id, session_token):
    if not session_token or len(session_token) > 256:
        raise InvalidSession("Session is no longer valid")
    row = connection.execute("""
        SELECT user_id FROM sessions WHERE token_hash = ? AND user_id = ? AND expires_at > ?
    """, (token_hash(session_token), user_id, int(time.time()))).fetchone()
    if row is None:
        raise InvalidSession("Session is no longer valid")


def cart_state(connection, user_id):
    row = connection.execute("SELECT revision, document FROM account_carts WHERE user_id = ?", (user_id,)).fetchone()
    return {"revision": int(row["revision"]), "cart": json.loads(row["document"])} if row else {"revision": 0, "cart": None}


def cart(user_id):
    with database() as connection:
        return cart_state(connection, user_id)


def write_cart(connection, user_id, revision, document):
    connection.execute("""
        INSERT INTO account_carts (user_id, revision, document, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET revision = excluded.revision,
            document = excluded.document, updated_at = excluded.updated_at
    """, (user_id, revision, json.dumps(document, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    return {"revision": revision, "cart": document}


def save_cart(user_id, session_token, expected_revision, document):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        require_live_session(connection, user_id, session_token)
        current = cart_state(connection, user_id)
        if current["revision"] != expected_revision:
            raise CartConflict(current)
        saved = write_cart(connection, user_id, expected_revision + 1, document)
        connection.commit()
        return saved


def checked_cart(connection, user_id, expected_revision, submitted):
    current = cart_state(connection, user_id)
    if current["revision"] != expected_revision or current["cart"] is None:
        raise CartConflict(current)
    saved_items = {item["product_id"]: item for item in current["cart"]["items"]}
    for item in submitted:
        saved = saved_items.get(item["product_id"])
        if (saved is None or saved["quantity"] < item["quantity"]
                or saved["product"].get("available") is False):
            raise CartConflict(current)
    return current


def complete_cart(connection, user_id, current, submitted):
    quantities = {item["product_id"]: item["quantity"] for item in submitted}
    remaining = []
    for item in current["cart"]["items"]:
        quantity = item["quantity"] - quantities.get(item["product_id"], 0)
        if quantity > 0:
            remaining.append({**item, "quantity": quantity})
    document = {**current["cart"], "items": remaining}
    saved = write_cart(connection, user_id, current["revision"] + 1, document)
    return {"cart_completed": True, "cart_revision": saved["revision"]}


def insert_order(connection, user_id, order_id, created_at, filename, status, total, positions, customer, document):
    connection.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (order_id, user_id, created_at, filename, status, str(total),
                        json.dumps(positions, ensure_ascii=False), json.dumps(customer, ensure_ascii=False), document))


def save_order(user_id, order_id, created_at, filename, status, total, positions, customer, document):
    with database() as connection:
        insert_order(connection, user_id, order_id, created_at, filename, status, total, positions, customer, document)
        connection.commit()


def save_order_with_cart(user_id, session_token, expected_revision, submitted, order_id, created_at,
                         filename, status, total, positions, customer, document):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        require_live_session(connection, user_id, session_token)
        current = checked_cart(connection, user_id, expected_revision, submitted)
        insert_order(connection, user_id, order_id, created_at, filename, status, total, positions, customer, document)
        result = (complete_cart(connection, user_id, current, submitted) if status == "exported"
                  else {"cart_completed": False, "cart_revision": current["revision"]})
        connection.commit()
        return result


def finish_sent_order(user_id, session_token, order_id, expected_revision, submitted):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE orders SET status = ? WHERE id = ? AND user_id = ?", ("sent", order_id, user_id))
        current = cart_state(connection, user_id)
        result = {"cart_completed": False, "cart_revision": current["revision"]}
        # Sending has already succeeded: preserve that fact even if another
        # device changed the draft or the initiating session was revoked.
        try:
            require_live_session(connection, user_id, session_token)
            current = checked_cart(connection, user_id, expected_revision, submitted)
        except (CartConflict, InvalidSession):
            pass
        else:
            result = complete_cart(connection, user_id, current, submitted)
        connection.commit()
        return result


def update_order_status(user_id, order_id, status):
    with database() as connection:
        connection.execute("UPDATE orders SET status = ? WHERE id = ? AND user_id = ?", (status, order_id, user_id))
        connection.commit()


def history(user_id, offset, limit):
    with database() as connection:
        connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*) AS count FROM orders WHERE user_id = ?", (user_id,)).fetchone()["count"]
        rows = connection.execute("""
            SELECT id, created_at, filename, status, total, positions, customer FROM orders
            WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
        """, (user_id, limit, offset)).fetchall()
    orders = [{**dict(row), "positions": json.loads(row["positions"]), "customer": json.loads(row["customer"])} for row in rows]
    return {"orders": orders, "total": total,
            "next_offset": offset + len(orders) if offset + len(orders) < total else None}


def order_file(user_id, order_id):
    with database() as connection:
        row = connection.execute("SELECT filename, document FROM orders WHERE id = ? AND user_id = ?",
                                 (order_id, user_id)).fetchone()
    return (row["filename"], bytes(row["document"])) if row else None


def delete_order(user_id, order_id):
    with database() as connection:
        cursor = connection.execute("DELETE FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id))
        removed = cursor.rowcount == 1
        connection.commit()
    return removed
