"""NEXORA WORKPULSE - Team Work & Attendance Portal (Flask).

Run locally:   python app.py            (demo mode when no .env exists)
Production:    gunicorn "app:create_app()"
CLI:           flask --app app init-db | create-admin | wipe-data
"""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (Blueprint, Flask, Response, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from models import (Acknowledgement, Agreement, AgreementSignature, Assignment, AssignmentReview,
                    AttendanceSession, AuditLog, BreakRecord, ChangeRequest, Milestone, Project, ProjectUpdate,
                    Setting, Task, TaskUpdate, User, db, utcnow)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


STATUS = {"not_started": "Not started", "in_progress": "In progress", "submitted": "Completed - awaiting review",
          "completed": "Done & approved", "needs_changes": "Needs changes"}
PRIORITIES = {"low": "Low", "medium": "Medium", "high": "High", "urgent": "Urgent"}
SKILLS = {"moderate": ("Moderate", 1), "good": ("Good enough", 2), "high": ("High", 3)}
SKILL_AREAS = [("backend", "Backend operations", "skill_backend"),
               ("database", "Database management", "skill_database"),
               ("frontend", "Frontend development", "skill_frontend")]
WORK_TYPES = {"backend": "Backend", "database": "Database", "frontend": "Frontend",
              "fullstack": "Full-stack", "other": "General / other"}
STAGES = ["Assigned", "Working", "Pull request", "Completed", "Approved"]
ROLES = {"member": "Team member", "founder": "Founder", "admin": "Admin"}
PHASES = {"discovery": "Discovery", "design": "Design", "development": "Development", "testing": "Testing",
          "launch": "Launch", "live": "Live & support"}
PROJECT_STATUS = {"on_track": "On track", "at_risk": "Needs attention", "delayed": "Delayed",
                  "paused": "Paused", "completed": "Completed"}
MILESTONE_STATUS = {"pending": "Upcoming", "in_progress": "In progress", "done": "Done"}
REQUEST_STATUS = {"open": "Received", "in_progress": "Working on it", "done": "Done", "declined": "Not planned"}
REQUEST_PRIORITY = {"low": "Low", "normal": "Normal", "high": "High", "urgent": "Urgent"}
MIN_PASSWORD = 12
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PR_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:[/?#].*)?$")
REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
WIPE_PHRASE = "DELETE ALL DATA"

csrf = CSRFProtect()
limiter = Limiter(get_remote_address, default_limits=[], storage_uri="memory://")
bp = Blueprint("web", __name__)
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))  # equalises timing for unknown emails


# --------------------------------------------------------------------------- configuration
def normalize_database_url(raw):
    """Accept Supabase strings as pasted; fix [brackets] and special characters in the password."""
    url = raw.strip().strip('"').strip("'")
    if any(x in url for x in ("YOUR_DB_PASSWORD", "YOUR_PROJECT_REF", "YOUR-PASSWORD", "YOUR_REGION")):
        raise ConfigError("DATABASE_URL still contains placeholder text. Paste your real Supabase Session pooler string "
                          "with your real database password.")
    m = re.match(r"^postgres(?:ql)?(?:\+psycopg2)?://([^:/@]+):(.*)@([^@/]+)/([^?]*)(\?.*)?$", url)
    if not m:
        raise ConfigError("DATABASE_URL must look like postgresql://USER:PASSWORD@HOST:5432/postgres")
    user, pw, host, dbname, query = m.groups()
    if pw.startswith("[") and pw.endswith("]"):
        pw = pw[1:-1]
    pw = quote(unquote(pw), safe="")  # makes @ # / ? etc. in the password safe
    return f"postgresql+psycopg2://{user}:{pw}@{host}/{dbname or 'postgres'}{query or ''}"


def github_config():
    return {"GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", "").strip(),
            "GITHUB_REPO_URL": os.environ.get("GITHUB_REPO_URL", "").strip(),
            "GITHUB_MERGE_METHOD": os.environ.get("GITHUB_MERGE_METHOD", "merge").strip() or "merge"}


def load_config():
    missing = [k for k in ("DATABASE_URL", "FLASK_SECRET_KEY") if not os.environ.get(k, "").strip()]
    if missing:
        raise ConfigError("Missing required environment variable(s): " + ", ".join(missing)
                          + ". Set them in Render > Environment (or in a local .env). See README.md.")
    secret = os.environ["FLASK_SECRET_KEY"].strip()
    if len(secret) < 32:
        raise ConfigError("FLASK_SECRET_KEY must be at least 32 characters long.")
    if secret.lower().startswith("replace-with"):
        raise ConfigError("FLASK_SECRET_KEY is still the placeholder. Run 'python make_env.py' or paste a real random key.")
    url = normalize_database_url(os.environ["DATABASE_URL"])
    tzname = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")
    try:
        ZoneInfo(tzname)
        hours = float(os.environ.get("DAILY_TARGET_HOURS", "4"))
        max_hours = float(os.environ.get("SESSION_MAX_HOURS", "16"))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Invalid APP_TIMEZONE / DAILY_TARGET_HOURS / SESSION_MAX_HOURS: {exc}") from exc
    dev = os.environ.get("APP_ENV", "production").lower() == "development"
    return {
        "SECRET_KEY": secret,
        "SQLALCHEMY_DATABASE_URI": url,
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "pool_size": 5, "max_overflow": 5, "pool_recycle": 280, "pool_pre_ping": True,
            "connect_args": {"sslmode": os.environ.get("DB_SSLMODE", "require")},
        },
        "APP_TIMEZONE": tzname,
        "DAILY_TARGET_MINUTES": int(hours * 60),
        "SESSION_MAX_HOURS": max_hours,
        "DEV_MODE": dev,
        "SESSION_COOKIE_SECURE": not dev,
        **github_config(),
    }


def create_app(test_config=None):
    app = Flask(__name__)
    if test_config is None:
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).parent / ".env", override=True)  # local only; Render has no .env file
        except ImportError:  # pragma: no cover
            pass
        app.config.update(load_config())
    else:
        app.config.update({**github_config(), **test_config})
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=14), MAX_CONTENT_LENGTH=256 * 1024,
        WTF_CSRF_TIME_LIMIT=None, SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render terminates TLS
    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    app.register_blueprint(bp)
    register_filters(app)
    register_errors(app)
    register_cli(app)

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
            "script-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
        if not app.config.get("DEV_MODE") and not app.config.get("TESTING"):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


# --------------------------------------------------------------------------- time & attendance helpers
def tz():
    return ZoneInfo(current_app.config["APP_TIMEZONE"])


def to_local(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(tz())


def local_today():
    return to_local(utcnow()).date()


def day_bounds(d):
    start = datetime.combine(d, time.min, tzinfo=tz())
    end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz())
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def target_minutes():
    return current_app.config["DAILY_TARGET_MINUTES"]


def max_session():
    return timedelta(hours=current_app.config["SESSION_MAX_HOURS"])


def session_minutes(s, now):
    """Recorded minutes = (clock_out or now, capped) - clock_in - pauses. Server-side only."""
    end = s.clock_out or min(now, s.clock_in + max_session())
    total = (end - s.clock_in).total_seconds()
    for b in s.breaks:
        be = min(b.ended_at or end, end)
        if be > b.started_at:
            total -= (be - b.started_at).total_seconds()
    return max(total, 0.0) / 60.0


def is_stale(s, now):
    return s.clock_out is None and now - s.clock_in > max_session()


def empty_day():
    return dict(sessions=[], minutes=0.0, first_in=None, last_out=None, active=False,
                on_break=False, incomplete=False)


def build_daily(user_ids, start, end, now=None):
    """Aggregate attendance per (user_id, local date). A session belongs to its start day."""
    now = now or utcnow()
    lo, hi = day_bounds(start)[0], day_bounds(end)[1]
    sessions = (AttendanceSession.query
                .filter(AttendanceSession.user_id.in_(list(user_ids)),
                        AttendanceSession.clock_in >= lo, AttendanceSession.clock_in < hi)
                .order_by(AttendanceSession.clock_in).all())
    out = {}
    for s in sessions:
        d = out.setdefault((s.user_id, to_local(s.clock_in).date()), empty_day())
        d["sessions"].append(s)
        d["minutes"] += session_minutes(s, now)
        d["first_in"] = d["first_in"] or s.clock_in
        if s.clock_out:
            d["last_out"] = max(d["last_out"] or s.clock_out, s.clock_out)
        else:
            d["active"] = True
            d["on_break"] = any(b.ended_at is None for b in s.breaks)
        if s.incomplete or is_stale(s, now):
            d["incomplete"] = True
    return out


def add_carryover(user_ids, daily, today, now):
    """Open sessions that started before today still count as active and must be visible/closable."""
    lo = day_bounds(today)[0]
    for s in AttendanceSession.query.filter(AttendanceSession.user_id.in_(list(user_ids)),
                                            AttendanceSession.clock_out.is_(None),
                                            AttendanceSession.clock_in < lo).all():
        d = daily.setdefault((s.user_id, today), empty_day())
        d["active"], d["first_in"] = True, d["first_in"] or s.clock_in
        d["on_break"] = d["on_break"] or any(b.ended_at is None for b in s.breaks)
        d["incomplete"] = d["incomplete"] or is_stale(s, now)
        d["sessions"].append(s)
    return daily


def day_state(day, met):
    """One plain label per member-day: what the admin actually wants to know."""
    if day["active"]:
        return ("paused", "Paused") if day["on_break"] else ("working", "Working now")
    if not day["sessions"]:
        return "absent", "Absent"
    return ("reached", "Target reached") if met else ("missed", "Target missed")


def first_day(user):
    return to_local(user.first_login_at).date() if user.first_login_at else None


def daily_records(members, start, end, now=None):
    """One row per member per day, from the member's first sign-in day only (earlier days are never 'absent')."""
    now, today = now or utcnow(), local_today()
    ids = [m.id for m in members]
    daily = build_daily(ids, start, end, now)
    if start <= today <= end:
        add_carryover(ids, daily, today, now)
    updates = {}
    for u in (TaskUpdate.query.filter(TaskUpdate.user_id.in_(ids), TaskUpdate.update_date >= start,
                                      TaskUpdate.update_date <= end).order_by(TaskUpdate.created_at).all()):
        updates.setdefault((u.user_id, u.update_date), []).append(u)
    tgt, rows = target_minutes(), []
    for m in members:
        fd = first_day(m)
        if not fd:
            continue
        d, last = max(start, fd), min(end, today)
        while d <= last:
            day = daily.get((m.id, d), empty_day())
            met = day["minutes"] >= tgt
            key, label = day_state(day, met)
            rows.append(dict(date=d, user=m, day=day, minutes=day["minutes"], met=met, state=key, label=label,
                             pct=min(100, int(day["minutes"] * 100 / tgt)) if tgt else 100,
                             updates=updates.get((m.id, d), [])))
            d += timedelta(days=1)
    rows.sort(key=lambda r: (r["date"], r["user"].name.lower()), reverse=True)
    return rows


# --------------------------------------------------------------------------- generic helpers
def clean(value, maxlen):
    return (value or "").strip()[:maxlen]


def parse_date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def valid_url(value):
    p = urlparse(value or "")
    return p.scheme in ("http", "https") and bool(p.netloc) and len(value) <= 500 and " " not in value


def csv_safe(value):
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def is_overdue(a):
    return a.status not in ("completed", "submitted") and a.task.deadline < local_today()


def check_new_password(pw, confirm):
    if len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if pw != confirm:
        return "Password confirmation does not match."
    return None


def stage_index(a):
    if a.status == "completed":
        return 4
    if a.status == "submitted":
        return 3
    if a.pr_url:
        return 2
    return 1 if a.status in ("in_progress", "needs_changes") else 0


# --------------------------------------------------------------------------- skills & suggestions
def skill_score(user, area):
    if area == "fullstack":
        return sum(skill_score(user, a) for a, _, _ in SKILL_AREAS)
    if area not in ("backend", "database", "frontend"):
        return 0
    lvl = getattr(user, f"skill_{area}")
    return SKILLS[lvl][1] if lvl in SKILLS else 0


def suggestions(members, open_counts):
    """Per work type: members ranked by self-rated skill, then by fewest open tasks."""
    out = {}
    for wt in ("backend", "database", "frontend", "fullstack"):
        ranked = sorted((m for m in members if skill_score(m, wt) > 0),
                        key=lambda m: (-skill_score(m, wt), open_counts.get(m.id, 0), m.name.lower()))
        out[wt] = ranked[:4]
    return out


def open_task_counts(ids):
    counts = {}
    for a in Assignment.query.filter(Assignment.user_id.in_(list(ids)), Assignment.status != "completed").all():
        counts[a.user_id] = counts.get(a.user_id, 0) + 1
    return counts


# --------------------------------------------------------------------------- settings & GitHub
def get_setting(key, default=""):
    row = db.session.get(Setting, key)
    return row.value if row and row.value else default


def set_setting(key, value):
    row = db.session.get(Setting, key) or Setting(key=key)
    row.value = value
    db.session.add(row)


def team_repo():
    return get_setting("github_repo", current_app.config.get("GITHUB_REPO_URL", ""))


def repo_for(task):
    return (task.repo_url if task and task.repo_url else team_repo()) or ""


def normalize_repo(url):
    m = REPO_RE.match((url or "").strip())
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None


def pr_number(url):
    m = PR_RE.match(url or "")
    return f"#{m.group(3)}" if m else ""


def github_merge(pr_url):
    """Merge a pull request through the GitHub REST API. Returns (ok, message)."""
    token = current_app.config.get("GITHUB_TOKEN")
    m = PR_RE.match(pr_url or "")
    if not token:
        return False, "GITHUB_TOKEN is not configured; open the pull request on GitHub to merge it."
    if not m:
        return False, "This is not a GitHub pull request link."
    owner, repo, num = m.groups()
    body = json.dumps({"merge_method": current_app.config.get("GITHUB_MERGE_METHOD", "merge")}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}/merge", data=body, method="PUT",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nexora-workpulse",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "{}")
            return bool(data.get("merged", True)), data.get("message", "Pull request merged.")
    except urllib.error.HTTPError as e:
        msgs = {401: "GitHub rejected the token. Create a new GITHUB_TOKEN.",
                403: "The token has no permission to merge in this repository.",
                404: "Pull request not found, or the token cannot see this repository.",
                405: "GitHub says this pull request is not mergeable (checks failing, draft, or already merged).",
                409: "Merge conflict: the branch must be updated before merging.",
                422: "GitHub could not merge this pull request."}
        return False, msgs.get(e.code, f"GitHub returned an error ({e.code}).")
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "Could not reach GitHub. Try again, or merge on GitHub directly."


# --------------------------------------------------------------------------- auth
ONBOARD_FREE = {"web.onboarding", "web.logout", "web.health", "static"}


@bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        u = db.session.get(User, uid)
        if u and u.is_active and session.get("sv") == u.session_version:
            g.user = u
        else:
            session.clear()
    if (g.user and g.user.role == "member" and g.user.onboarded_at is None
            and request.endpoint not in ONBOARD_FREE and request.method == "GET"):
        return redirect(url_for("web.onboarding", step=0))
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("web.login"))
        return view(*a, **kw)
    return wrapped


def role_required(role):
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if g.user.role != role:
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


admin_required = role_required("admin")
member_required = role_required("member")
founder_required = role_required("founder")


@bp.route("/")
def index():
    return redirect(url_for("web.dashboard" if g.user else "web.login"))


@bp.route("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        return {"status": "ok"}, 200
    except Exception:  # noqa: BLE001 - never leak details
        return {"status": "database unavailable"}, 503


def open_session(user_id):
    return AttendanceSession.query.filter_by(user_id=user_id, clock_out=None).first()


def close_session(s, now):
    """Close a session. Forgotten (over-long) sessions are capped and flagged. Returns True if capped."""
    capped = is_stale(s, now)
    end = s.clock_in + max_session() if capped else now
    s.incomplete = s.incomplete or capped
    for b in s.breaks:
        if b.ended_at is None:
            b.ended_at = min(end, now)
    s.clock_out = end
    return capped


def start_attendance(user, now):
    """Start a work session if none is open. Returns 'started', 'running' or 'restarted'."""
    s = open_session(user.id)
    if s and not is_stale(s, now):
        return "running"
    state = "started"
    if s:
        close_session(s, now)
        state = "restarted"
    db.session.add(AttendanceSession(user_id=user.id, clock_in=now))
    return state


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute;120 per hour", methods=["POST"])
def login():
    if g.user:
        return redirect(url_for("web.dashboard"))
    error, email = None, ""
    if request.method == "POST":
        email = clean(request.form.get("email"), 254).lower()
        password = request.form.get("password", "")[:200]
        if not EMAIL_RE.match(email) or not password:
            error = "Enter a valid email address and your password."
        else:
            user = User.query.filter_by(email=email).first()
            ok = check_password_hash(user.password_hash if user else DUMMY_HASH, password)
            if user and not ok and password != password.strip():
                # phone keyboards / autofill often add a space before or after the password
                ok = check_password_hash(user.password_hash, password.strip())
            if user and ok and not user.is_active:
                error = "This account is disabled. Ask your administrator to enable it on the Team page."
            elif user and ok:
                session.clear()
                session.permanent = True
                session["uid"], session["sv"] = user.id, user.session_version
                now = utcnow()
                user.last_login_at = now
                if user.role == "member":
                    user.first_login_at = user.first_login_at or now  # attendance starts from this day
                    try:
                        if start_attendance(user, now) != "running":
                            flash("Signed in. Your work timer has started.", "success")
                        db.session.commit()
                    except IntegrityError:
                        db.session.rollback()
                else:
                    db.session.commit()
                return redirect(url_for("web.dashboard"))
            else:
                error = "Incorrect email or password. Check for capital letters in the password."
    return render_template("login.html", error=error, email=email)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    if g.user.role == "member":
        s = open_session(g.user.id)
        if s:
            close_session(s, utcnow())
            db.session.commit()
    was_member = g.user.role == "member"
    session.clear()
    flash("You have been signed out." + (" Your work timer is stopped." if was_member else ""), "info")
    return redirect(url_for("web.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        if not check_password_hash(g.user.password_hash, current):
            error = "Current password is incorrect."
        else:
            error = check_new_password(new, confirm)
        if not error:
            g.user.password_hash = generate_password_hash(new)
            g.user.session_version += 1
            session["sv"] = g.user.session_version
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("web.dashboard"))
    return render_template("password.html", error=error, min_length=MIN_PASSWORD)


# --------------------------------------------------------------------------- onboarding (members, first sign-in)
@bp.route("/welcome/<int:step>", methods=["GET", "POST"])
@member_required
def onboarding(step):
    if not 0 <= step <= 4:
        abort(404)
    u = g.user
    if step in (1, 2, 3):
        area, label, field = SKILL_AREAS[step - 1]
        if request.method == "POST":
            level = request.form.get("level", "")
            if level not in SKILLS:
                flash("Pick one of the three options to continue.", "error")
            else:
                setattr(u, field, level)
                if step == 3:
                    u.onboarded_at = u.onboarded_at or utcnow()
                db.session.commit()
                return redirect(url_for("web.onboarding", step=step + 1))
        # do not skip questions: earlier answers must exist
        for i, (_, _, f) in enumerate(SKILL_AREAS[:step - 1], start=1):
            if getattr(u, f) is None:
                return redirect(url_for("web.onboarding", step=i))
        return render_template("onboarding.html", step=step, area=area, label=label,
                               current=getattr(u, field), total=3)
    if step == 4 and u.onboarded_at is None:
        return redirect(url_for("web.onboarding", step=1))
    return render_template("onboarding.html", step=step, total=3)


# --------------------------------------------------------------------------- dashboards
@bp.route("/dashboard")
@login_required
def dashboard():
    if g.user.role == "founder":
        return founder_dashboard()
    return admin_dashboard() if g.user.role == "admin" else member_dashboard()


def member_dashboard():
    now, today = utcnow(), local_today()
    daily = add_carryover([g.user.id], build_daily([g.user.id], today, today, now), today, now)
    day = daily.get((g.user.id, today), empty_day())
    assignments = Assignment.query.join(Task).filter(Assignment.user_id == g.user.id).all()
    assignments.sort(key=lambda a: (a.status == "completed", a.status == "submitted", a.task.deadline, a.id))
    history = daily_records([g.user], today - timedelta(days=6), today, now)
    minutes, tgt = day["minutes"], target_minutes()
    updated_today = {u.assignment_id for u in TaskUpdate.query.filter_by(user_id=g.user.id, update_date=today)}
    return render_template(
        "member_dashboard.html", day=day, state=day_state(day, minutes >= tgt), minutes=minutes,
        target=tgt, remaining=max(tgt - minutes, 0), pct=min(100, int(minutes * 100 / tgt)) if tgt else 100,
        assignments=assignments, overdue=[a for a in assignments if is_overdue(a)],
        updated_today=updated_today, history=history, repo=team_repo(), today=today)


def admin_dashboard():
    now, today = utcnow(), local_today()
    members = User.query.filter_by(role="member", is_active=True).order_by(User.name).all()
    rows = daily_records(members, today, today, now)
    seen = {r["user"].id for r in rows}
    waiting = [m for m in members if m.id not in seen]  # never signed in yet
    prs = (Assignment.query.filter(Assignment.pr_url.isnot(None), Assignment.pr_merged_at.is_(None))
           .order_by(Assignment.updated_at.desc()).all())
    review = Assignment.query.filter_by(status="submitted").order_by(Assignment.submitted_at.desc()).all()
    stats = dict(members=len(members), working=sum(1 for r in rows if r["state"] in ("working", "paused")),
                 reached=sum(1 for r in rows if r["met"]), absent=sum(1 for r in rows if r["state"] == "absent"),
                 prs=len(prs), review=len(review))
    return render_template("admin_dashboard.html", rows=rows, waiting=waiting, stats=stats, prs=prs,
                           review=review, target=target_minutes(), today=today, repo=team_repo())


# --------------------------------------------------------------------------- attendance (members)
@bp.route("/clock-in", methods=["POST"])
@member_required
def clock_in():
    s = open_session(g.user.id)
    if s and not is_stale(s, utcnow()):
        flash("Your work timer is already running.", "warning")
    else:
        try:
            g.user.first_login_at = g.user.first_login_at or utcnow()
            start_attendance(g.user, utcnow())
            db.session.commit()
            flash("Work timer started.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Your work timer is already running.", "warning")
    return redirect(url_for("web.dashboard"))


@bp.route("/clock-out", methods=["POST"])
@member_required
def clock_out():
    s, now = open_session(g.user.id), utcnow()
    if not s:
        flash("Your work timer is not running.", "warning")
        return redirect(url_for("web.dashboard"))
    if close_session(s, now):
        flash("Your previous session ran past the maximum length and was capped and flagged as incomplete.", "warning")
    db.session.commit()
    flash("Work timer stopped. Today's hours are saved.", "success")
    return redirect(url_for("web.dashboard"))


@bp.route("/break/start", methods=["POST"])
@member_required
def break_start():
    s = open_session(g.user.id)
    if not s:
        flash("Start your work timer before pausing.", "warning")
    elif any(b.ended_at is None for b in s.breaks):
        flash("Your timer is already paused.", "warning")
    else:
        db.session.add(BreakRecord(session_id=s.id, started_at=utcnow()))
        db.session.commit()
        flash("Timer paused. Paused time does not count toward your target.", "info")
    return redirect(url_for("web.dashboard"))


@bp.route("/break/end", methods=["POST"])
@member_required
def break_end():
    s = open_session(g.user.id)
    b = next((b for b in (s.breaks if s else []) if b.ended_at is None), None)
    if not b:
        flash("Your timer is not paused.", "warning")
    else:
        b.ended_at = utcnow()
        db.session.commit()
        flash("Welcome back. Timer resumed.", "success")
    return redirect(url_for("web.dashboard"))


@bp.route("/attendance")
@member_required
def attendance_history():
    today = local_today()
    rows = daily_records([g.user], today - timedelta(days=29), today)
    return render_template("attendance.html", rows=rows, target=target_minutes(),
                           reached=sum(1 for r in rows if r["met"]),
                           total=sum(r["minutes"] for r in rows))


# --------------------------------------------------------------------------- member tasks
def my_assignment(aid):
    a = db.session.get(Assignment, aid)
    if a is None or a.user_id != g.user.id:
        abort(404)
    return a


def render_my_task(a, errors=None, form=None, status=200):
    return render_template("my_task.html", a=a, errors=errors or [], form=form or {},
                           locked=a.status in ("submitted", "completed"), repo=repo_for(a.task),
                           stage=stage_index(a)), status


@bp.route("/my-tasks/<int:aid>")
@member_required
def my_task(aid):
    return render_my_task(my_assignment(aid))


def store_link(a, link):
    if PR_RE.match(link):
        if a.pr_url != link:
            a.pr_url, a.pr_merged_at = link, None
    else:
        a.deliverable_url = link


@bp.route("/my-tasks/<int:aid>/update", methods=["POST"])
@member_required
def task_update(aid):
    """Daily progress update: what was done today, progress %, optional pull request / work link."""
    a = my_assignment(aid)
    if a.status in ("submitted", "completed"):
        abort(403)
    f, errors = request.form, []
    note, link = clean(f.get("note"), 5000), clean(f.get("link"), 500)
    try:
        progress = int(f.get("progress", ""))
        if not 0 <= progress <= 100:
            raise ValueError
    except ValueError:
        progress = None
        errors.append("Progress must be a whole number from 0 to 100.")
    if link and not valid_url(link):
        errors.append("The link must be a valid http:// or https:// URL (for code, paste your GitHub pull request).")
    if errors:
        return render_my_task(a, errors, f, 400)
    if link:
        store_link(a, link)
    a.progress = progress
    if a.status in ("not_started", "needs_changes"):
        a.status = "in_progress"
    db.session.add(TaskUpdate(assignment_id=a.id, user_id=g.user.id, update_date=local_today(),
                              progress=progress, note=note or f"Progress updated to {progress}%", pr_url=link or None))
    db.session.commit()
    flash("Today's progress is saved.", "success")
    return redirect(url_for("web.my_task", aid=a.id))


@bp.route("/my-tasks/<int:aid>/complete", methods=["POST"])
@member_required
def task_complete(aid):
    """Task completion acknowledgement: the member confirms the task is finished and sends it for review."""
    a = my_assignment(aid)
    if a.status in ("submitted", "completed"):
        abort(403)
    f, errors = request.form, []
    summary, link = clean(f.get("summary"), 5000), clean(f.get("link"), 500)
    if f.get("confirm") != "1":
        errors.append("Tick the box to confirm the task is complete.")
    if not summary:
        errors.append("Add a short completion summary.")
    if link and not valid_url(link):
        errors.append("The link must be a valid http:// or https:// URL.")
    if not (link or a.pr_url or a.deliverable_url):
        errors.append("Add your pull request link (or a deliverable link) before marking the task complete.")
    if errors:
        return render_my_task(a, errors, f, 400)
    if link:
        store_link(a, link)
    a.work_notes, a.progress, a.status, a.submitted_at = summary, 100, "submitted", utcnow()
    db.session.add(TaskUpdate(assignment_id=a.id, user_id=g.user.id, update_date=local_today(), progress=100,
                              note="Marked complete: " + summary, pr_url=link or None))
    db.session.commit()
    flash("Task marked complete. Your admin will review and merge it.", "success")
    return redirect(url_for("web.my_task", aid=a.id))


# --------------------------------------------------------------------------- admin: users
@bp.route("/admin/users")
@admin_required
def users():
    people = User.query.order_by(User.role, User.name).all()
    counts = open_task_counts([u.id for u in people])
    return render_template("users.html", users=people, counts=counts)


@bp.route("/admin/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    errors = []
    v = {"name": "", "email": "", "role": request.args.get("role", "member"), "project_id": request.args.get("project", "")}
    projects = Project.query.order_by(Project.name).all()
    if request.method == "POST":
        v = {"name": clean(request.form.get("name"), 80), "email": clean(request.form.get("email"), 254).lower(),
             "role": request.form.get("role", "member"), "project_id": request.form.get("project_id", "")}
        if v["role"] not in ("member", "founder"):
            errors.append("Choose Team member or Founder.")
        project = (db.session.get(Project, int(v["project_id"])) if v["project_id"].isdigit() else None)
        if v["role"] == "founder" and not project:
            errors.append("Choose the founder's project (create the project first under Founders).")
        pw, confirm = request.form.get("password", ""), request.form.get("confirm_password", "")
        if len(v["name"]) < 2:
            errors.append("Name must be at least 2 characters.")
        if not EMAIL_RE.match(v["email"]):
            errors.append("Enter a valid email address.")
        elif User.query.filter_by(email=v["email"]).first():
            errors.append("An account with this email already exists.")
        err = check_new_password(pw, confirm)
        if err:
            errors.append(err)
        if not errors:
            try:
                u = User(name=v["name"], email=v["email"], role=v["role"], password_hash=generate_password_hash(pw),
                         project_id=project.id if v["role"] == "founder" else None)
                db.session.add(u)
                db.session.commit()
                flash(f"Account created for {u.name}. " + ("They will see their project status and agreement."
                      if u.role == "founder" else "Attendance starts the day they first sign in."), "success")
                return redirect(url_for("web.users"))
            except IntegrityError:
                db.session.rollback()
                errors.append("An account with this email already exists.")
    return render_template("user_form.html", errors=errors, v=v, min_length=MIN_PASSWORD, projects=projects)


@bp.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def user_toggle(uid):
    u = db.session.get(User, uid) or abort(404)
    if u.id == g.user.id:
        flash("You cannot disable your own account.", "warning")
    else:
        u.is_active = not u.is_active
        u.session_version += 1  # signs the user out immediately
        db.session.commit()
        flash(f"{u.name} is now {'active' if u.is_active else 'disabled'}.", "success")
    return redirect(url_for("web.users"))


@bp.route("/admin/users/<int:uid>/reset-password", methods=["POST"])
@admin_required
def user_reset(uid):
    u = db.session.get(User, uid) or abort(404)
    pw = request.form.get("password", "")
    err = check_new_password(pw, pw)
    if err:
        flash(err, "error")
    else:
        u.password_hash = generate_password_hash(pw)
        u.session_version += 1
        db.session.commit()
        flash(f"Password reset for {u.name}.", "success")
    return redirect(url_for("web.users"))


def member_footprint(u):
    return dict(sessions=AttendanceSession.query.filter_by(user_id=u.id).count(),
                tasks=Assignment.query.filter_by(user_id=u.id).count(),
                updates=TaskUpdate.query.filter_by(user_id=u.id).count(),
                requests=ChangeRequest.query.filter_by(user_id=u.id).count(),
                signatures=AgreementSignature.query.filter_by(user_id=u.id).count())


def delete_member(u, heir_id=None):
    """Permanently remove a user (member, founder or admin) and everything recorded for them.
    Tasks they created and reviews they wrote are handed over to `heir_id` (the admin doing the delete)."""
    if heir_id:
        Task.query.filter_by(created_by=u.id).update({Task.created_by: heir_id}, synchronize_session=False)
        AssignmentReview.query.filter_by(reviewer_id=u.id).update({AssignmentReview.reviewer_id: heir_id},
                                                                  synchronize_session=False)
    sids = [s.id for s in AttendanceSession.query.with_entities(AttendanceSession.id).filter_by(user_id=u.id)]
    aids = [a.id for a in Assignment.query.with_entities(Assignment.id).filter_by(user_id=u.id)]
    if sids:
        BreakRecord.query.filter(BreakRecord.session_id.in_(sids)).delete(synchronize_session=False)
    AttendanceSession.query.filter_by(user_id=u.id).delete(synchronize_session=False)
    TaskUpdate.query.filter(TaskUpdate.user_id == u.id).delete(synchronize_session=False)
    if aids:
        TaskUpdate.query.filter(TaskUpdate.assignment_id.in_(aids)).delete(synchronize_session=False)
        AssignmentReview.query.filter(AssignmentReview.assignment_id.in_(aids)).delete(synchronize_session=False)
    Assignment.query.filter_by(user_id=u.id).delete(synchronize_session=False)
    Acknowledgement.query.filter_by(user_id=u.id).delete(synchronize_session=False)
    AuditLog.query.filter_by(user_id=u.id).delete(synchronize_session=False)
    AgreementSignature.query.filter_by(user_id=u.id).delete(synchronize_session=False)
    ChangeRequest.query.filter_by(user_id=u.id).delete(synchronize_session=False)
    ProjectUpdate.query.filter_by(author_id=u.id).update({ProjectUpdate.author_id: heir_id}, synchronize_session=False)
    Agreement.query.filter_by(updated_by=u.id).update({Agreement.updated_by: heir_id}, synchronize_session=False)
    db.session.delete(u)


@bp.route("/admin/users/<int:uid>/delete", methods=["GET", "POST"])
@admin_required
def user_delete(uid):
    u = db.session.get(User, uid) or abort(404)
    if u.id == g.user.id:
        flash("You cannot delete the account you are signed in with. Ask another admin to delete it.", "warning")
        return redirect(url_for("web.users"))
    if request.method == "POST":
        if request.form.get("confirm_email", "").strip().lower() != u.email:
            flash("Type the email exactly to confirm.", "error")
            return render_template("user_delete.html", u=u, fp=member_footprint(u)), 400
        name = u.name
        delete_member(u, heir_id=g.user.id)
        db.session.commit()
        flash(f"{name} and all of their records were deleted.", "success")
        return redirect(url_for("web.users"))
    return render_template("user_delete.html", u=u, fp=member_footprint(u))


# --------------------------------------------------------------------------- admin: settings & data wipe
@bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings():
    error = None
    if request.method == "POST":
        raw = clean(request.form.get("github_repo"), 300)
        repo = normalize_repo(raw) if raw else ""
        if raw and not repo:
            error = "Enter the repository as https://github.com/owner/repository"
        else:
            set_setting("github_repo", repo)
            db.session.commit()
            flash("GitHub repository saved. Members now see it on their dashboard.", "success")
            return redirect(url_for("web.settings"))
    return render_template("settings.html", repo=team_repo(), error=error,
                           token=bool(current_app.config.get("GITHUB_TOKEN")))


def wipe_all_data(keep_user_id=None, include_admins=False):
    """Delete all WorkPulse records and settings stay. Members are always deleted.
    include_admins=True also deletes every other admin account, keeping only `keep_user_id`."""
    for model in (TaskUpdate, AssignmentReview, Assignment, Task, BreakRecord, AttendanceSession,
                  Acknowledgement, AuditLog, AgreementSignature, Agreement, ChangeRequest, ProjectUpdate, Milestone):
        model.query.delete(synchronize_session=False)
    gone = User.role != "admin"
    if include_admins and keep_user_id:
        gone = User.id != keep_user_id
    User.query.filter(gone).delete(synchronize_session=False)
    User.query.update({User.first_login_at: None, User.project_id: None}, synchronize_session=False)
    Project.query.delete(synchronize_session=False)


@bp.route("/admin/reset", methods=["GET", "POST"])
@admin_required
def reset_data():
    counts = dict(members=User.query.filter_by(role="member").count(), tasks=Task.query.count(),
                  founders=User.query.filter_by(role="founder").count(), projects=Project.query.count(),
                  sessions=AttendanceSession.query.count(), updates=TaskUpdate.query.count(),
                  admins=User.query.filter(User.role == "admin", User.id != g.user.id).count())
    if request.method == "POST":
        if request.form.get("phrase", "").strip() != WIPE_PHRASE:
            flash(f"Type {WIPE_PHRASE} exactly to confirm.", "error")
            return render_template("reset.html", counts=counts, phrase=WIPE_PHRASE), 400
        everyone = request.form.get("include_admins") == "1"
        wipe_all_data(keep_user_id=g.user.id, include_admins=everyone)
        db.session.commit()
        flash("All WorkPulse data was deleted. " + ("Only your own account was kept." if everyone
              else "Admin accounts and settings were kept."), "success")
        return redirect(url_for("web.dashboard"))
    return render_template("reset.html", counts=counts, phrase=WIPE_PHRASE)


# --------------------------------------------------------------------------- admin: tasks
def parse_task_form(form, members):
    v = {k: clean(form.get(k), n) for k, n in (("title", 150), ("description", 5000),
                                               ("expected_deliverable", 1000), ("instructions", 3000),
                                               ("priority", 10), ("target_hours", 10), ("work_type", 12),
                                               ("repo_url", 300), ("start_date", 10), ("deadline", 10))}
    errors = []
    if len(v["title"]) < 3:
        errors.append("Task title must be at least 3 characters.")
    if not v["description"]:
        errors.append("A detailed description is required.")
    if v["priority"] not in PRIORITIES:
        errors.append("Choose a valid priority.")
    if v["work_type"] and v["work_type"] not in WORK_TYPES:
        errors.append("Choose a valid work type.")
    if v["repo_url"]:
        repo = normalize_repo(v["repo_url"])
        if not repo:
            errors.append("Repository must look like https://github.com/owner/repository")
        else:
            v["repo_url"] = repo
    try:
        hours = float(v["target_hours"])
        if not 0 < hours <= 200:
            raise ValueError
    except ValueError:
        hours = None
        errors.append("Target hours must be a number greater than 0 and at most 200.")
    sd, dl = parse_date(v["start_date"]), parse_date(v["deadline"])
    if not sd or not dl:
        errors.append("Start date and deadline are required.")
    elif dl < sd:
        errors.append("Deadline cannot be before the start date.")
    valid_ids = {m.id for m in members}
    all_team = form.get("all_team") == "1"
    ids = valid_ids if all_team else {int(x) for x in form.getlist("assignees") if x.isdigit()} & valid_ids
    if not ids:
        errors.append("Select at least one team member, or choose the entire team.")
    return v, hours, sd, dl, ids, all_team, errors


def apply_task_fields(task, v, hours, sd, dl):
    task.title, task.description, task.priority = v["title"], v["description"], v["priority"]
    task.target_hours, task.start_date, task.deadline = hours, sd, dl
    task.expected_deliverable, task.instructions = v["expected_deliverable"] or None, v["instructions"] or None
    task.work_type, task.repo_url = v["work_type"] or None, v["repo_url"] or None


def task_form_page(task, members, v, selected, all_team, errors, status=200):
    counts = open_task_counts([m.id for m in members])
    return render_template("task_form.html", task=task, members=members, v=v, selected=selected,
                           all_team=all_team, errors=errors, counts=counts,
                           suggest=suggestions(members, counts), repo=team_repo()), status


@bp.route("/admin/tasks")
@admin_required
def tasks():
    items = Task.query.order_by(Task.deadline.desc(), Task.id.desc()).all()
    return render_template("tasks.html", tasks=items)


@bp.route("/admin/tasks/new", methods=["GET", "POST"])
@admin_required
def task_new():
    members = User.query.filter_by(role="member", is_active=True).order_by(User.name).all()
    errors, v, selected, all_team = [], {"priority": "medium", "start_date": local_today().isoformat()}, set(), False
    if request.method == "POST":
        v, hours, sd, dl, selected, all_team, errors = parse_task_form(request.form, members)
        if not errors:
            t = Task(created_by=g.user.id)
            apply_task_fields(t, v, hours, sd, dl)
            db.session.add(t)
            db.session.flush()
            for uid in selected:
                db.session.add(Assignment(task_id=t.id, user_id=uid))
            db.session.commit()
            flash(f"Task posted to {len(selected)} team member(s).", "success")
            return redirect(url_for("web.task_detail", tid=t.id))
    return task_form_page(None, members, v, selected, all_team, errors)


@bp.route("/admin/tasks/<int:tid>/edit", methods=["GET", "POST"])
@admin_required
def task_edit(tid):
    t = db.session.get(Task, tid) or abort(404)
    members = User.query.filter_by(role="member", is_active=True).order_by(User.name).all()
    existing = {a.user_id: a for a in t.assignments}
    errors, selected, all_team = [], set(existing), False
    v = dict(title=t.title, description=t.description, priority=t.priority, target_hours=t.target_hours,
             start_date=t.start_date.isoformat(), deadline=t.deadline.isoformat(), work_type=t.work_type or "",
             repo_url=t.repo_url or "", expected_deliverable=t.expected_deliverable or "",
             instructions=t.instructions or "")
    if request.method == "POST":
        v, hours, sd, dl, selected, all_team, errors = parse_task_form(request.form, members)
        if not errors:
            apply_task_fields(t, v, hours, sd, dl)
            kept = 0
            for uid in selected - set(existing):
                db.session.add(Assignment(task_id=t.id, user_id=uid))
            for uid, a in existing.items():
                if uid not in selected:
                    if a.status == "not_started":
                        db.session.delete(a)
                    else:
                        kept += 1
            db.session.commit()
            flash("Task updated." + (f" {kept} member(s) with work in progress kept on the task." if kept else ""),
                  "success")
            return redirect(url_for("web.task_detail", tid=t.id))
    return task_form_page(t, members, v, selected, all_team, errors)


@bp.route("/admin/tasks/<int:tid>")
@admin_required
def task_detail(tid):
    t = db.session.get(Task, tid) or abort(404)
    assignments = sorted(t.assignments, key=lambda a: a.user.name.lower())
    return render_template("task_detail.html", task=t, assignments=assignments, repo=repo_for(t),
                           token=bool(current_app.config.get("GITHUB_TOKEN")))


@bp.route("/admin/tasks/<int:tid>/delete", methods=["POST"])
@admin_required
def task_delete(tid):
    t = db.session.get(Task, tid) or abort(404)
    if request.form.get("confirm") != "1":
        flash("Tick the confirmation box to delete this task.", "error")
        return redirect(url_for("web.task_detail", tid=t.id))
    aids = [a.id for a in t.assignments]
    if aids:
        TaskUpdate.query.filter(TaskUpdate.assignment_id.in_(aids)).delete(synchronize_session=False)
    db.session.delete(t)
    db.session.commit()
    flash("Task deleted.", "success")
    return redirect(url_for("web.tasks"))


@bp.route("/admin/assignments/<int:aid>/review", methods=["POST"])
@admin_required
def review(aid):
    a = db.session.get(Assignment, aid) or abort(404)
    decision, feedback = request.form.get("decision"), clean(request.form.get("feedback"), 3000)
    if a.status != "submitted":
        flash("Only completed work can be reviewed.", "warning")
    elif decision not in ("approved", "changes"):
        flash("Choose approve or request changes.", "error")
    elif decision == "changes" and not feedback:
        flash("Feedback is required when requesting changes.", "error")
    else:
        if decision == "approved":
            a.status, a.completed_at, a.progress = "completed", utcnow(), 100
        else:
            a.status = "needs_changes"
        db.session.add(AssignmentReview(assignment_id=a.id, reviewer_id=g.user.id, decision=decision,
                                        feedback=feedback or None))
        db.session.commit()
        flash("Work approved." if decision == "approved" else "Changes requested.", "success")
    return redirect(request.form.get("next") if request.form.get("next") == url_for("web.dashboard")
                    else url_for("web.task_detail", tid=a.task_id))


@bp.route("/admin/assignments/<int:aid>/merge", methods=["POST"])
@admin_required
def merge_pr(aid):
    a = db.session.get(Assignment, aid) or abort(404)
    back = (url_for("web.dashboard") if request.form.get("next") == url_for("web.dashboard")
            else url_for("web.task_detail", tid=a.task_id))
    if not a.pr_url:
        flash("No pull request is linked to this task.", "warning")
        return redirect(back)
    if request.form.get("mark_only") == "1":
        a.pr_merged_at = utcnow()
        db.session.commit()
        flash("Pull request marked as merged.", "success")
        return redirect(back)
    ok, msg = github_merge(a.pr_url)
    if ok:
        a.pr_merged_at = utcnow()
        if request.form.get("approve") == "1" and a.status == "submitted":
            a.status, a.completed_at, a.progress = "completed", utcnow(), 100
            db.session.add(AssignmentReview(assignment_id=a.id, reviewer_id=g.user.id, decision="approved",
                                            feedback="Merged on GitHub."))
        db.session.commit()
        flash(f"Merged {pr_number(a.pr_url)} on GitHub. {msg}", "success")
    else:
        flash(msg, "error")
    return redirect(back)


# --------------------------------------------------------------------------- founders: project status + agreement
def agreement_hash(a):
    return hashlib.sha256(f"v{a.version}\n{a.title}\n{a.body}".encode("utf-8")).hexdigest()


def signature_state(project, user=None):
    """Returns (agreement, current_signature_for_user_or_any, needs_signing)."""
    a = project.agreement if project else None
    if not a:
        return None, None, False
    sigs = [s for s in a.signatures if s.version == a.version and (user is None or s.user_id == user.id)]
    return a, (sigs[0] if sigs else None), not sigs


def project_stats(p):
    ms = p.milestones
    return dict(done=sum(1 for m in ms if m.status == "done"), total=len(ms),
                open=sum(1 for r in p.requests if r.status in ("open", "in_progress")),
                fixed=sum(1 for r in p.requests if r.status == "done"))


def render_founder_page(p, preview=False, errors=None, form=None, status=200):
    agreement, sig, needs = signature_state(p, None if preview else g.user)
    return render_template("founder_dashboard.html", p=p, preview=preview, errors=errors or [], form=form or {},
                           agreement=agreement, sig=sig, needs_sign=needs, stats=project_stats(p) if p else {},
                           phase_keys=list(PHASES)), status


def founder_dashboard():
    return render_founder_page(g.user.project)


@bp.route("/requests", methods=["POST"])
@founder_required
def change_request_new():
    p = g.user.project or abort(404)
    f, errors = request.form, []
    title, details = clean(f.get("title"), 150), clean(f.get("details"), 5000)
    link, priority = clean(f.get("page_url"), 500), f.get("priority", "normal")
    if len(title) < 3:
        errors.append("Give the change a short title (at least 3 characters).")
    if not details:
        errors.append("Describe what should change.")
    if priority not in REQUEST_PRIORITY:
        errors.append("Choose a priority.")
    if link and not valid_url(link):
        errors.append("The page link must start with http:// or https://")
    if errors:
        return render_founder_page(p, errors=errors, form=f, status=400)
    db.session.add(ChangeRequest(project_id=p.id, user_id=g.user.id, title=title, details=details,
                                 priority=priority, page_url=link or None))
    db.session.commit()
    flash("Your change request was sent to the team.", "success")
    return redirect(url_for("web.dashboard") + "#changes")


@bp.route("/agreement", methods=["GET", "POST"])
@founder_required
def agreement():
    p = g.user.project
    a, sig, needs = signature_state(p, g.user)
    error = None
    if request.method == "POST":
        if not a:
            abort(404)
        name = clean(request.form.get("signed_name"), 120)
        if not needs:
            flash("You have already signed this version.", "info")
            return redirect(url_for("web.agreement"))
        if request.form.get("version") != str(a.version):
            error = "The agreement was updated while you were reading it. Please review the new version."
        elif request.form.get("agree") != "1":
            error = "Tick the box to confirm you have read and agree to the agreement."
        elif len(name) < 3:
            error = "Type your full name as your signature."
        else:
            db.session.add(AgreementSignature(
                agreement_id=a.id, user_id=g.user.id, version=a.version, signed_name=name,
                body_hash=agreement_hash(a), ip_address=(request.remote_addr or "")[:45],
                user_agent=(request.headers.get("User-Agent") or "")[:300]))
            db.session.commit()
            flash("Agreement signed. Thank you!", "success")
            return redirect(url_for("web.agreement"))
    return render_template("agreement.html", p=p, a=a, sig=sig, needs_sign=needs, error=error,
                           history=[s for s in (a.signatures if a else []) if s.user_id == g.user.id])


# --------------------------------------------------------------------------- admin: founder projects
def parse_project_form(f):
    v = {k: clean(f.get(k), n) for k, n in (("name", 120), ("summary", 3000), ("live_url", 500),
                                           ("start_date", 10), ("target_date", 10))}
    errors = []
    if len(v["name"]) < 2:
        errors.append("Project name must be at least 2 characters.")
    if v["live_url"] and not valid_url(v["live_url"]):
        errors.append("Live application link must start with http:// or https://")
    sd, td = parse_date(v["start_date"]), parse_date(v["target_date"])
    if v["start_date"] and not sd or v["target_date"] and not td:
        errors.append("Dates must be valid.")
    elif sd and td and td < sd:
        errors.append("Target date cannot be before the start date.")
    return v, sd, td, errors


@bp.route("/admin/founders")
@admin_required
def projects():
    items = Project.query.order_by(Project.updated_at.desc()).all()
    unassigned = User.query.filter_by(role="founder", project_id=None).order_by(User.name).all()
    return render_template("projects.html", projects=items, stats={p.id: project_stats(p) for p in items},
                           signed={p.id: signature_state(p)[1] for p in items}, unassigned=unassigned)


@bp.route("/admin/founders/new", methods=["GET", "POST"])
@admin_required
def project_new():
    v, errors = {}, []
    if request.method == "POST":
        v, sd, td, errors = parse_project_form(request.form)
        if not errors:
            p = Project(name=v["name"], summary=v["summary"] or None, live_url=v["live_url"] or None,
                        start_date=sd, target_date=td)
            db.session.add(p)
            db.session.flush()
            for i, title in enumerate(("Discovery & requirements", "Design approval", "Development",
                                       "Testing & your feedback", "Launch")):
                db.session.add(Milestone(project_id=p.id, title=title, sort_order=i))
            db.session.commit()
            flash("Project created with 5 starter milestones. Now add the founder's login.", "success")
            return redirect(url_for("web.project_admin", pid=p.id))
    return render_template("project_form.html", p=None, v=v, errors=errors)


@bp.route("/admin/founders/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def project_edit(pid):
    p = db.session.get(Project, pid) or abort(404)
    v = dict(name=p.name, summary=p.summary or "", live_url=p.live_url or "",
             start_date=p.start_date.isoformat() if p.start_date else "",
             target_date=p.target_date.isoformat() if p.target_date else "")
    errors = []
    if request.method == "POST":
        v, sd, td, errors = parse_project_form(request.form)
        if not errors:
            p.name, p.summary, p.live_url = v["name"], v["summary"] or None, v["live_url"] or None
            p.start_date, p.target_date = sd, td
            db.session.commit()
            flash("Project details saved.", "success")
            return redirect(url_for("web.project_admin", pid=p.id))
    return render_template("project_form.html", p=p, v=v, errors=errors)


@bp.route("/admin/founders/<int:pid>")
@admin_required
def project_admin(pid):
    p = db.session.get(Project, pid) or abort(404)
    a, sig, _ = signature_state(p)
    return render_template("project_admin.html", p=p, stats=project_stats(p), agreement=a, sig=sig,
                           all_founders=User.query.filter_by(role="founder").order_by(User.name).all())


@bp.route("/admin/founders/<int:pid>/preview")
@admin_required
def project_preview(pid):
    return render_founder_page(db.session.get(Project, pid) or abort(404), preview=True)


@bp.route("/admin/founders/<int:pid>/status", methods=["POST"])
@admin_required
def project_status(pid):
    p = db.session.get(Project, pid) or abort(404)
    f = request.form
    phase, status = f.get("phase"), f.get("status")
    try:
        progress = int(f.get("progress", ""))
        if not 0 <= progress <= 100:
            raise ValueError
    except ValueError:
        flash("Progress must be a whole number from 0 to 100.", "error")
        return redirect(url_for("web.project_admin", pid=p.id))
    if phase not in PHASES or status not in PROJECT_STATUS:
        flash("Choose a valid phase and status.", "error")
        return redirect(url_for("web.project_admin", pid=p.id))
    p.phase, p.status, p.progress = phase, status, progress
    p.status_note = clean(f.get("status_note"), 2000) or None
    live = clean(f.get("live_url"), 500)
    if live and not valid_url(live):
        flash("Live application link must start with http:// or https://", "error")
        return redirect(url_for("web.project_admin", pid=p.id))
    p.live_url = live or None
    p.updated_at = utcnow()
    db.session.commit()
    flash("Work status updated. Founders see it right away.", "success")
    return redirect(url_for("web.project_admin", pid=p.id))


@bp.route("/admin/founders/<int:pid>/milestones", methods=["POST"])
@admin_required
def milestone_add(pid):
    p = db.session.get(Project, pid) or abort(404)
    title = clean(request.form.get("title"), 150)
    if len(title) < 2:
        flash("Milestone title is required.", "error")
    else:
        db.session.add(Milestone(project_id=p.id, title=title, due_date=parse_date(request.form.get("due_date")),
                                 sort_order=max([m.sort_order for m in p.milestones] or [0]) + 1))
        p.updated_at = utcnow()
        db.session.commit()
        flash("Milestone added.", "success")
    return redirect(url_for("web.project_admin", pid=p.id) + "#milestones")


@bp.route("/admin/milestones/<int:mid>", methods=["POST"])
@admin_required
def milestone_update(mid):
    m = db.session.get(Milestone, mid) or abort(404)
    pid = m.project_id
    if request.form.get("action") == "delete":
        db.session.delete(m)
        flash("Milestone removed.", "success")
    else:
        title, status = clean(request.form.get("title"), 150), request.form.get("status")
        if len(title) < 2 or status not in MILESTONE_STATUS:
            flash("Milestone needs a title and a valid status.", "error")
            return redirect(url_for("web.project_admin", pid=pid) + "#milestones")
        m.title, m.status, m.due_date = title, status, parse_date(request.form.get("due_date"))
        flash("Milestone saved.", "success")
    m.project.updated_at = utcnow()
    db.session.commit()
    return redirect(url_for("web.project_admin", pid=pid) + "#milestones")


@bp.route("/admin/founders/<int:pid>/updates", methods=["POST"])
@admin_required
def project_update_add(pid):
    p = db.session.get(Project, pid) or abort(404)
    body = clean(request.form.get("body"), 5000)
    if not body:
        flash("Write the update first.", "error")
    else:
        db.session.add(ProjectUpdate(project_id=p.id, author_id=g.user.id, body=body))
        p.updated_at = utcnow()
        db.session.commit()
        flash("Update posted to the founder's page.", "success")
    return redirect(url_for("web.project_admin", pid=p.id) + "#updates")


@bp.route("/admin/project-updates/<int:uid>/delete", methods=["POST"])
@admin_required
def project_update_delete(uid):
    u = db.session.get(ProjectUpdate, uid) or abort(404)
    pid = u.project_id
    db.session.delete(u)
    db.session.commit()
    flash("Update removed.", "success")
    return redirect(url_for("web.project_admin", pid=pid) + "#updates")


@bp.route("/admin/requests/<int:rid>", methods=["POST"])
@admin_required
def change_request_update(rid):
    r = db.session.get(ChangeRequest, rid) or abort(404)
    status = request.form.get("status")
    if status not in REQUEST_STATUS:
        flash("Choose a valid status.", "error")
    else:
        r.status, r.admin_reply = status, clean(request.form.get("admin_reply"), 3000) or None
        db.session.commit()
        flash("Change request updated. The founder can see your reply.", "success")
    return redirect(url_for("web.project_admin", pid=r.project_id) + "#changes")


@bp.route("/admin/founders/<int:pid>/founders", methods=["POST"])
@admin_required
def project_founders(pid):
    p = db.session.get(Project, pid) or abort(404)
    chosen = {int(x) for x in request.form.getlist("founders") if x.isdigit()}
    for u in User.query.filter_by(role="founder").all():
        if u.id in chosen:
            u.project_id = p.id
        elif u.project_id == p.id:
            u.project_id = None
    db.session.commit()
    flash("Founder access updated.", "success")
    return redirect(url_for("web.project_admin", pid=p.id))


@bp.route("/admin/founders/<int:pid>/agreement", methods=["GET", "POST"])
@admin_required
def agreement_edit(pid):
    p = db.session.get(Project, pid) or abort(404)
    a = p.agreement
    v = {"title": a.title if a else f"{p.name} — Project Agreement", "body": a.body if a else DEFAULT_AGREEMENT}
    error = None
    if request.method == "POST":
        v = {"title": clean(request.form.get("title"), 200), "body": (request.form.get("body") or "").strip()[:60000]}
        if len(v["title"]) < 3 or len(v["body"]) < 20:
            error = "The agreement needs a title and at least a few lines of text."
        else:
            if a is None:
                a = Agreement(project_id=p.id, title=v["title"], body=v["body"], version=1, updated_by=g.user.id)
                db.session.add(a)
                msg = "Agreement published. The founder can now sign it."
            elif (a.title, a.body) != (v["title"], v["body"]):
                had_sigs = any(s.version == a.version for s in a.signatures)
                a.title, a.body, a.version = v["title"], v["body"], a.version + 1
                a.updated_by, a.updated_at = g.user.id, utcnow()
                msg = f"Agreement updated to version {a.version}." + (" The founder must sign again." if had_sigs else "")
            else:
                msg = "No changes to save."
            db.session.commit()
            flash(msg, "success")
            return redirect(url_for("web.agreement_edit", pid=p.id))
    sig = signature_state(p)[1]
    return render_template("agreement_edit.html", p=p, a=a, v=v, error=error, sig=sig)


@bp.route("/admin/founders/<int:pid>/delete", methods=["POST"])
@admin_required
def project_delete(pid):
    p = db.session.get(Project, pid) or abort(404)
    if request.form.get("confirm") != "1":
        flash("Tick the confirmation box to delete this project.", "error")
        return redirect(url_for("web.project_admin", pid=p.id))
    User.query.filter_by(project_id=p.id).update({User.project_id: None}, synchronize_session=False)
    db.session.delete(p)
    db.session.commit()
    flash("Project deleted. Founder logins were kept; delete them from Team if needed.", "success")
    return redirect(url_for("web.projects"))


APP_VERSION = "v3.2 (2026-10-27)"


@bp.route("/admin/diagnostics")
@admin_required
def diagnostics():
    """Read-only health check: shows the database layout and tries every founder page part."""
    checks, columns, migrations = [], [], []

    def run(name, fn):
        try:
            result = fn()
            checks.append((name, True, result if isinstance(result, str) else "OK"))
        except Exception as exc:  # noqa: BLE001 - this page exists to show errors to the admin
            db.session.rollback()
            checks.append((name, False, f"{type(exc).__name__}: {' '.join(str(exc).split())[:500]}"))

    try:
        migrations = [r[0] for r in db.session.execute(text("SELECT version FROM nwp_schema_migrations ORDER BY version"))]
    except Exception:  # noqa: BLE001
        db.session.rollback()
    try:
        columns = [tuple(r) for r in db.session.execute(text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_name LIKE 'nwp_%' ORDER BY table_name, ordinal_position"))]
    except Exception:  # noqa: BLE001
        db.session.rollback()
    run("Count users by role", lambda: ", ".join(f"{r}: {n}" for r, n in db.session.execute(
        text("SELECT role, COUNT(*) FROM nwp_users GROUP BY role"))))
    run("Load founder accounts", lambda: f"{User.query.filter_by(role='founder').count()} founder(s)")
    run("Load founder projects", lambda: f"{Project.query.count()} project(s)")
    for p in Project.query.order_by(Project.id).all() if checks[-1][1] else []:
        run(f"Project {p.id}: founders", lambda p=p: f"{len(p.founders)}")
        run(f"Project {p.id}: milestones", lambda p=p: f"{len(p.milestones)}")
        run(f"Project {p.id}: updates", lambda p=p: f"{len(p.updates)}")
        run(f"Project {p.id}: change requests", lambda p=p: f"{len(p.requests)}")
        run(f"Project {p.id}: agreement", lambda p=p: "yes" if p.agreement else "none yet")
        run(f"Project {p.id}: admin page", lambda p=p: (render_template(
            "project_admin.html", p=p, stats=project_stats(p), agreement=signature_state(p)[0],
            sig=signature_state(p)[1], all_founders=User.query.filter_by(role="founder").all()), "renders")[1])
        run(f"Project {p.id}: founder page", lambda p=p: (render_founder_page(p, preview=True), "renders")[1])
    run("Founders list page", lambda: (projects(), "renders")[1])

    def template_links():
        bad = []
        folder = Path(current_app.root_path) / "templates"
        for f in sorted(folder.glob("*.html")):
            for ep in re.findall(r"url_for\(\s*'([^']+)'", f.read_text(encoding="utf-8")):
                if ep != "static" and ep not in current_app.view_functions:
                    bad.append(f"{f.name} links to missing page '{ep}'")
        if bad:
            raise RuntimeError("Templates from a different version: " + "; ".join(bad))
        return f"{len(list(folder.glob('*.html')))} templates, all links valid"
    run("Template files match this app version", template_links)
    return render_template("diagnostics.html", checks=checks, columns=columns, migrations=migrations,
                           version=APP_VERSION)


DEFAULT_AGREEMENT = """1. Scope of work
Describe what will be built and delivered.

2. Timeline
Key milestones and the expected launch date.

3. Progress and reporting
Work status, milestones and team updates are shared on the WorkPulse project page.

4. Changes
Change requests are submitted through the WorkPulse founder page. Changes outside the agreed scope may need a separate quote.

5. Ownership and confidentiality
Who owns the code and designs once the project is delivered, and how both sides keep information confidential.

6. Support
What support is included after launch and for how long.

By signing, the founder confirms they have read and agree to these terms."""


# --------------------------------------------------------------------------- admin: daily records
def records_range(args):
    today = local_today()
    rng, errors = args.get("range", "week"), []
    if rng == "today":
        start, end = today, today
    elif rng == "yesterday":
        start = end = today - timedelta(days=1)
    elif rng == "month":
        start, end = today.replace(day=1), today
    elif rng == "custom":
        start, end = parse_date(args.get("start")), parse_date(args.get("end"))
        if not start or not end:
            errors.append("Enter both a start and an end date.")
            rng, start, end = "week", today - timedelta(days=today.weekday()), today
        elif end < start or (end - start).days > 92:
            errors.append("End date must not precede the start date, and the range is limited to 93 days.")
            rng, start, end = "week", today - timedelta(days=today.weekday()), today
    else:
        rng, start, end = "week", today - timedelta(days=today.weekday()), today
    return rng, start, end, errors


def report_data(args):
    rng, start, end, errors = records_range(args)
    members = User.query.filter_by(role="member").order_by(User.name).all()
    uid = args.get("user_id", "")
    chosen = [m for m in members if uid.isdigit() and m.id == int(uid)] or members
    rows = daily_records(chosen, start, end)
    tgt = target_minutes()
    summary = []
    for m in chosen:
        mine = [r for r in rows if r["user"].id == m.id]
        if not mine:
            continue
        present = [r for r in mine if r["state"] != "absent"]
        summary.append(dict(user=m, days=len(mine), present=len(present), minutes=sum(r["minutes"] for r in mine),
                            met=sum(1 for r in mine if r["met"]), absent=len(mine) - len(present),
                            rate=int(sum(1 for r in mine if r["met"]) * 100 / len(mine))))
    return dict(rng=rng, start=start, end=end, members=members, uid=uid, rows=rows, summary=summary,
                errors=errors, target=tgt)


@bp.route("/admin/records")
@admin_required
def reports():
    return render_template("reports.html", **report_data(request.args))


@bp.route("/admin/records/export.csv")
@admin_required
def reports_export():
    data = report_data(request.args)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Member", "Email", "Logged in", "Logged out", "Hours worked", "Minutes",
                "Target reached", "Status", "Work updates"])
    for r in data["rows"]:
        d, mins = r["day"], int(round(r["minutes"]))
        work = " | ".join(f"{u.assignment.task.title} ({u.progress}%): {u.note}" for u in r["updates"])
        w.writerow([csv_safe(x) for x in [
            r["date"].isoformat(), r["user"].name, r["user"].email,
            to_local(d["first_in"]).strftime("%H:%M") if d["first_in"] else "",
            to_local(d["last_out"]).strftime("%H:%M") if d["last_out"] else ("Active" if d["active"] else ""),
            f"{mins // 60}h {mins % 60:02d}m", mins, "Yes" if r["met"] else "No", r["label"], work]])
    name = f"workpulse_daily_records_{data['start']}_{data['end']}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------- template filters, errors
def register_filters(app):
    @app.template_filter("hm")
    def hm(minutes):
        m = int(round(minutes or 0))
        return f"{m // 60}h {m % 60:02d}m"

    @app.template_filter("ltime")
    def ltime(dt):
        return to_local(dt).strftime("%I:%M %p").lstrip("0") if dt else "—"

    @app.template_filter("ldt")
    def ldt(dt):
        return to_local(dt).strftime("%d %b %Y, %I:%M %p") if dt else "—"

    @app.template_filter("ldate")
    def ldate(d):
        return d.strftime("%a, %d %b %Y") if d else "—"

    @app.template_filter("initials")
    def initials(name):
        parts = [p for p in (name or "?").split() if p]
        return ((parts[0][0] + (parts[1][0] if len(parts) > 1 else "")) if parts else "?").upper()

    @app.template_filter("hue")
    def hue(value):
        return (sum(ord(c) for c in str(value)) * 47) % 36 * 10

    app.jinja_env.globals.update(
        STATUS=STATUS, PRIORITIES=PRIORITIES, SKILLS=SKILLS, SKILL_AREAS=SKILL_AREAS, WORK_TYPES=WORK_TYPES,
        STAGES=STAGES, ROLES=ROLES, PHASES=PHASES, PROJECT_STATUS=PROJECT_STATUS, MILESTONE_STATUS=MILESTONE_STATUS,
        REQUEST_STATUS=REQUEST_STATUS, REQUEST_PRIORITY=REQUEST_PRIORITY, is_overdue=is_overdue, stage_index=stage_index, pr_number=pr_number,
        skill_score=skill_score, session_mins=lambda s: session_minutes(s, utcnow()))

    @app.context_processor
    def nav_counts():
        counts = {}
        if getattr(g, "user", None) is not None and g.user.role == "admin":
            counts["review"] = Assignment.query.filter_by(status="submitted").count()
            counts["requests"] = ChangeRequest.query.filter_by(status="open").count()
        return {"nav_counts": counts}


def register_errors(app):
    def page(code, title, msg):
        return render_template("error.html", code=code, title=title, message=msg), code

    @app.errorhandler(403)
    def e403(_):
        return page(403, "Access denied", "Your account does not have permission to view this page.")

    @app.errorhandler(404)
    def e404(_):
        return page(404, "Page not found", "The page you requested does not exist or is not available to you.")

    @app.errorhandler(429)
    def e429(_):
        return page(429, "Too many attempts", "Please wait a few minutes before trying again.")

    @app.errorhandler(CSRFError)
    def ecsrf(_):
        if request.endpoint == "web.login":
            flash("Your sign-in page was open too long or cookies were blocked. Please sign in again.", "warning")
            return redirect(url_for("web.login"))
        return page(400, "Session expired", "Your form session expired or was invalid. Go back, refresh, and try again.")

    @app.errorhandler(500)
    def e500(e):
        db.session.rollback()
        orig = getattr(e, "original_exception", None) or e
        current_app.logger.error("500 on %s: %s: %s", request.path, type(orig).__name__, orig)
        detail = None
        user = getattr(g, "user", None)
        if user is not None and getattr(user, "role", None) == "admin":  # only admins see the technical reason
            detail = f"{type(orig).__name__}: {' '.join(str(orig).split())[:600]}"
        return render_template("error.html", code=500, title="Something went wrong", detail=detail,
                               message="An unexpected error occurred. Nothing has been exposed; please try again."), 500


# --------------------------------------------------------------------------- CLI
def run_migrations():
    folder = Path(__file__).parent / "migrations"
    with db.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS nwp_schema_migrations (version VARCHAR(100) PRIMARY KEY, "
                             "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        done = {r[0] for r in conn.exec_driver_sql("SELECT version FROM nwp_schema_migrations")}
        for f in sorted(folder.glob("*.sql")):
            if f.name not in done:
                conn.exec_driver_sql(f.read_text(encoding="utf-8"))
                conn.exec_driver_sql("INSERT INTO nwp_schema_migrations (version) VALUES (%s)", (f.name,))
                print(f"Applied migration {f.name}")
    print("Database schema is up to date.")


def create_admin(email, password, name="Administrator"):
    """Create an admin. Never overwrites an existing account. Returns (ok, message)."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return False, "ADMIN_EMAIL is missing or not a valid email address."
    if len(password or "") < MIN_PASSWORD:
        return False, f"ADMIN_PASSWORD must be at least {MIN_PASSWORD} characters."
    if User.query.filter_by(email=email).first():
        return False, "An account with that email already exists; nothing changed."
    u = User(name=name[:80], email=email, role="admin", password_hash=generate_password_hash(password))
    db.session.add(u)
    db.session.commit()
    return True, f"Administrator {email} created."


def prepare_database():
    """Create/upgrade nwp_ tables (safe, additive) and create the first admin if none exists."""
    run_migrations()
    if User.query.filter_by(role="admin").count():
        print("An administrator already exists; skipping admin bootstrap.")
    elif os.environ.get("ADMIN_EMAIL") and os.environ.get("ADMIN_PASSWORD"):
        print(create_admin(os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"],
                           os.environ.get("ADMIN_NAME", "Administrator"))[1])
    else:
        print("No administrator yet. Set ADMIN_EMAIL and ADMIN_PASSWORD in .env and start again.")


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Apply SQL migrations (safe, additive) and create the first admin if none exists."""
        prepare_database()

    @app.cli.command("wipe-data")
    def wipe_data_cmd():
        """Delete ALL WorkPulse records (keeps administrator accounts and settings). Cannot be undone."""
        if input(f"Type {WIPE_PHRASE} to permanently delete all WorkPulse data: ").strip() != WIPE_PHRASE:
            raise SystemExit("Cancelled. Nothing was deleted.")
        wipe_all_data()
        db.session.commit()
        print("All WorkPulse data deleted. Administrator accounts were kept.")

    @app.cli.command("create-admin")
    def create_admin_cmd():
        """Create an administrator from ADMIN_EMAIL / ADMIN_PASSWORD (never overwrites)."""
        ok, msg = create_admin(os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD"),
                               os.environ.get("ADMIN_NAME", "Administrator"))
        print(msg)
        raise SystemExit(0 if ok else 1)


DEMO_ADMIN = ("admin@example.com", "Admin-Demo-2026!")
DEMO_MEMBER_PW = "Member-Demo-2026!"
DEMO_FOUNDER = "farah@example.com"


def demo_config():
    """Local-only fallback: SQLite file + auto-generated secret, so the app can be explored with zero setup."""
    base = Path(__file__).parent
    keyfile = base / ".local_secret"
    if not keyfile.exists():
        keyfile.write_text(secrets.token_urlsafe(48))
    return {
        "SECRET_KEY": keyfile.read_text().strip(),
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(base / "workpulse_demo.db"),
        "APP_TIMEZONE": os.environ.get("APP_TIMEZONE", "Asia/Kolkata"),
        "DAILY_TARGET_MINUTES": 240, "SESSION_MAX_HOURS": 16,
        "DEV_MODE": True, "SESSION_COOKIE_SECURE": False,
        **github_config(),
    }


def seed_demo():
    if User.query.count():
        return
    today = local_today()
    db.session.add(User(name="Demo Admin", email=DEMO_ADMIN[0], role="admin",
                        password_hash=generate_password_hash(DEMO_ADMIN[1])))
    members = [User(name=n, email=e, role="member", password_hash=generate_password_hash(DEMO_MEMBER_PW))
               for n, e in (("Mia Member", "mia@example.com"), ("Raj Member", "raj@example.com"))]
    db.session.add_all(members)
    db.session.flush()
    t = Task(title="Build the login API endpoint", description="Create a POST /api/login endpoint with input "
             "validation and rate limiting. Open a pull request when ready.", priority="high", target_hours=3,
             start_date=today, deadline=today + timedelta(days=3), work_type="backend",
             expected_deliverable="Merged pull request", instructions="Write tests for the happy path.", created_by=1)
    db.session.add(t)
    db.session.flush()
    for m in members:
        db.session.add(Assignment(task_id=t.id, user_id=m.id))
    pr = Project(name="Client Web App", summary="Website and project dashboard, tracked in WorkPulse.",
                 live_url="https://example.com", phase="development", status="on_track", progress=58,
                 status_note="Design is approved. Dashboard pages are being built this week.",
                 start_date=today - timedelta(days=20), target_date=today + timedelta(days=25))
    db.session.add(pr)
    db.session.flush()
    for i, (title, st) in enumerate((("Discovery & requirements", "done"), ("Design approval", "done"),
                                     ("Development", "in_progress"), ("Testing & your feedback", "pending"),
                                     ("Launch", "pending"))):
        db.session.add(Milestone(project_id=pr.id, title=title, status=st, sort_order=i))
    db.session.add(ProjectUpdate(project_id=pr.id, author_id=1, body="Design approved. Development has started."))
    db.session.add(Agreement(project_id=pr.id, title="Client Web App — Project Agreement", body=DEFAULT_AGREEMENT,
                             updated_by=1))
    db.session.add(User(name="Farah Founder", email=DEMO_FOUNDER, role="founder", project_id=pr.id,
                        password_hash=generate_password_hash(DEMO_MEMBER_PW)))
    db.session.commit()


if __name__ == "__main__":
    # Local use:  python app.py   ->  http://127.0.0.1:5000
    # (Render uses gunicorn "app:create_app()" instead and always needs real settings.)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env", override=True)
    except ImportError:
        pass
    _demo = not os.environ.get("DATABASE_URL", "").strip()
    try:
        _app = create_app(demo_config()) if _demo else create_app()
    except ConfigError as exc:
        raise SystemExit(f"\nCONFIGURATION ERROR: {exc}\n")
    try:
        with _app.app_context():
            if _demo:
                db.create_all()
                seed_demo()
            else:
                print("Connecting to the database and preparing tables ...")
                prepare_database()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("\nDATABASE ERROR: could not connect or set up the tables.\n"
                         "Check DATABASE_URL (use the Supabase *Session pooler* string, correct password).\n"
                         f"Technical detail: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}\n")
    _port = int(os.environ.get("PORT", "5000"))
    print("\n" + "=" * 62)
    print("NEXORA WORKPULSE is running.   Open:  http://127.0.0.1:%d" % _port)
    if _demo:
        print("DEMO MODE (no Supabase needed; data is in workpulse_demo.db)")
        print("  Admin  : %s  /  %s" % DEMO_ADMIN)
        print("  Member : mia@example.com  /  %s" % DEMO_MEMBER_PW)
        print("  Member : raj@example.com  /  %s" % DEMO_MEMBER_PW)
        print("  Founder: %s  /  %s" % (DEMO_FOUNDER, DEMO_MEMBER_PW))
    print("Press CTRL+C to stop.")
    print("=" * 62 + "\n")
    _app.run(host="127.0.0.1", port=_port, debug=False)
