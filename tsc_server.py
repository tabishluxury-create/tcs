import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import timedelta, timezone, datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
AUTH_DATABASE = Path(os.environ.get("TSC_AUTH_DATABASE", BASE_DIR / "tsc-auth.sqlite3"))
LOGIN_TEMPLATE_DIR = BASE_DIR / "templates"
MIN_ADMIN_PASSWORD_LENGTH = 5


def _required_environment(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set.")
    return value


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _validate_csrf():
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        abort(400, description="The security token is missing or expired. Reload the page and try again.")


def _connect_database():
    AUTH_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(AUTH_DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS member_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            year TEXT NOT NULL,
            batch TEXT NOT NULL,
            department TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    return connection


@contextmanager
def _database_connection():
    connection = _connect_database()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _bootstrap_admin():
    password = _required_environment("TSC_ADMIN_PASSWORD")
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise RuntimeError(f"TSC_ADMIN_PASSWORD must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters.")

    with _database_connection() as connection:
        admin = connection.execute("SELECT id FROM admin_users WHERE id = 1").fetchone()
        if admin is None:
            connection.execute(
                "INSERT INTO admin_users (id, username, password_hash, updated_at) VALUES (1, ?, ?, ?)",
                ("admin", generate_password_hash(password), datetime.now(timezone.utc).isoformat()),
            )
    os.environ.pop("TSC_ADMIN_PASSWORD", None)


def create_app():
    app = Flask(__name__, template_folder=str(LOGIN_TEMPLATE_DIR), static_folder=None)
    app.config.update(
        SECRET_KEY=_required_environment("TSC_SESSION_SECRET"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("TSC_COOKIE_SECURE", "true").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("TSC_SESSION_SECRET must be at least 32 characters.")

    environment = os.environ.get("TSC_ENV", "development").lower()
    if environment == "production":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    limiter_storage = os.environ.get("TSC_RATE_LIMIT_STORAGE_URI")
    if environment == "production" and not limiter_storage:
        raise RuntimeError("Set TSC_RATE_LIMIT_STORAGE_URI to a shared limiter store (for example Redis) in production.")
    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri=limiter_storage or "memory://",
        default_limits=[],
    )
    master_password = os.environ.get("TSC_MASTER_PASSWORD", "")
    _bootstrap_admin()

    def admin_is_authenticated():
        user_id = session.get("admin_user_id")
        if user_id != 1:
            return False
        with _database_connection() as connection:
            return connection.execute("SELECT 1 FROM admin_users WHERE id = ?", (user_id,)).fetchone() is not None

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not admin_is_authenticated():
                if request.path.startswith("/api/admin/"):
                    return jsonify(error="Your admin session has expired. Sign in again to continue."), 401
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def safe_next_path(value):
        parsed = urlparse(value or "")
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
            return url_for("admin_dashboard")
        return value

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if environment == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.get("/healthz")
    def health_check():
        with _database_connection() as connection:
            connection.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    @app.get("/")
    def home():
        return send_file(BASE_DIR / "tcs.html")

    @app.get("/tcs.html")
    def tsc_page():
        return send_file(BASE_DIR / "tcs.html")

    @app.get("/member-form.html")
    def member_form():
        return send_file(BASE_DIR / "member-form.html")

    @app.get("/api/csrf-token")
    def public_csrf_token():
        return jsonify(csrf_token=_csrf_token())

    @app.post("/api/member-registrations")
    @limiter.limit("5 per hour")
    def create_member_registration():
        _validate_csrf()
        fields = {
            "name": request.form.get("name", "").strip(),
            "email": request.form.get("email", "").strip().lower(),
            "phone": request.form.get("phone", "").strip(),
            "year": request.form.get("year", "").strip(),
            "batch": request.form.get("batch", "").strip(),
            "department": request.form.get("department", "").strip(),
        }
        limits = {"name": 120, "email": 254, "phone": 30, "batch": 64, "department": 120}
        if any(not fields[name] or len(fields[name]) > limit for name, limit in limits.items()):
            return jsonify(error="Complete every field and stay within the field length limits."), 400
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", fields["email"]):
            return jsonify(error="Enter a valid email address."), 400
        phone_digits = re.sub(r"\D", "", fields["phone"])
        if not re.fullmatch(r"[0-9+().\-\s]+", fields["phone"]) or not 7 <= len(phone_digits) <= 15:
            return jsonify(error="Enter a valid phone number with 7 to 15 digits."), 400
        if fields["year"] not in {"1", "2", "3", "4", "other"}:
            return jsonify(error="Select a valid year of study."), 400

        fields["year"] = "Other" if fields["year"] == "other" else f"Year {fields['year']}"
        with _database_connection() as connection:
            connection.execute(
                """
                INSERT INTO member_registrations (name, email, phone, year, batch, department, created_at)
                VALUES (:name, :email, :phone, :year, :batch, :department, :created_at)
                """,
                {**fields, "created_at": datetime.now(timezone.utc).isoformat()},
            )
        return jsonify(message="Your registration was submitted successfully."), 201

    @app.get("/gallery-store.js")
    def gallery_store():
        return send_file(BASE_DIR / "gallery-store.js", mimetype="text/javascript")

    @app.get("/tsc-cassette.jpg")
    def cassette_art():
        return send_file(BASE_DIR / "tsc-cassette.jpg", mimetype="image/jpeg")

    @app.route("/admin.html/login", methods=["GET", "POST"])
    @app.route("/admin/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute", methods=["POST"])
    def admin_login():
        if admin_is_authenticated():
            return redirect(url_for("admin_dashboard"))
        next_path = safe_next_path(request.values.get("next"))
        error = ""
        if request.method == "POST":
            _validate_csrf()
            password = request.form.get("password", "")
            with _database_connection() as connection:
                admin = connection.execute(
                    "SELECT id, password_hash FROM admin_users WHERE id = 1"
                ).fetchone()
            valid_admin_password = admin and check_password_hash(admin["password_hash"], password)
            valid_master_password = master_password and hmac.compare_digest(master_password, password)
            if valid_admin_password or valid_master_password:
                session.clear()
                session["admin_user_id"] = admin["id"]
                session.permanent = True
                return redirect(next_path)
            error = "The password is incorrect."
        return render_template(
            "admin-login.html",
            csrf_token=_csrf_token(),
            error=error,
            next_path=next_path,
        )

    @app.get("/admin.html")
    @admin_required
    def admin_dashboard():
        return send_file(BASE_DIR / "admin.html")

    @app.get("/api/admin/csrf-token")
    @admin_required
    def admin_csrf_token():
        return jsonify(csrf_token=_csrf_token())

    @app.get("/api/admin/members")
    @admin_required
    def admin_members():
        with _database_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, name, email, phone, year, batch, department, created_at
                FROM member_registrations
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        response = jsonify(members=[
            {
                "id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "phone": row["phone"],
                "year": row["year"],
                "batch": row["batch"],
                "department": row["department"],
                "registered": row["created_at"][:10],
            }
            for row in rows
        ])
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.post("/admin/logout")
    @admin_required
    def admin_logout():
        _validate_csrf()
        session.clear()
        return redirect(url_for("admin_login"))

    @app.post("/api/admin/change-password")
    @admin_required
    @limiter.limit("5 per hour")
    def change_password():
        _validate_csrf()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(new_password) < MIN_ADMIN_PASSWORD_LENGTH:
            return jsonify(error=f"Choose a new password with at least {MIN_ADMIN_PASSWORD_LENGTH} characters."), 400
        if not hmac.compare_digest(new_password, confirm_password):
            return jsonify(error="The new passwords do not match."), 400

        with _database_connection() as connection:
            admin = connection.execute(
                "SELECT password_hash FROM admin_users WHERE id = 1"
            ).fetchone()
            if not admin or not check_password_hash(admin["password_hash"], current_password):
                return jsonify(error="The current password is incorrect."), 400
            connection.execute(
                "UPDATE admin_users SET password_hash = ?, updated_at = ? WHERE id = 1",
                (generate_password_hash(new_password), datetime.now(timezone.utc).isoformat()),
            )
        session.clear()
        return jsonify(message="Password changed. Sign in again with your new password.", redirect=url_for("admin_login"))

    @app.errorhandler(429)
    def rate_limit_exceeded(_error):
        if request.endpoint == "create_member_registration":
            return jsonify(error="Too many registration attempts. Wait before submitting again."), 429
        return render_template(
            "admin-login.html",
            csrf_token=_csrf_token(),
            error="Too many sign-in attempts. Wait a minute and try again.",
            next_path=url_for("admin_dashboard"),
        ), 429

    @app.errorhandler(400)
    def bad_request(error):
        if request.endpoint == "create_member_registration":
            return jsonify(error=error.description), 400
        return error

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
