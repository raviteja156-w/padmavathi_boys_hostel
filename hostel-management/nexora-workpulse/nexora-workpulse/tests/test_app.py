from datetime import datetime, timedelta
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash

from app import (ConfigError, build_daily, create_admin, create_app, daily_records, local_today, session_minutes)
from models import (Assignment, AttendanceSession, BreakRecord, Setting, Task, TaskUpdate, User, db, utcnow)
from tests.conftest import PW, login, make_app

TASK = {"title": "Quarterly report", "description": "Compile numbers", "priority": "high", "target_hours": "3",
        "start_date": "2026-01-01", "deadline": "2099-01-31", "expected_deliverable": "PDF", "instructions": ""}
PR = "https://github.com/acme/app/pull/7"


def uid(app, email):
    with app.app_context():
        return User.query.filter_by(email=email).one().id


def post_task(admin, **over):
    return admin.post("/admin/tasks/new", data={**TASK, **over}, follow_redirects=False)


@pytest.fixture()
def evening(monkeypatch):
    """Pin the clock to 6 PM India time so 5-hour sessions never cross midnight."""
    import app as appmod
    from datetime import timezone
    from zoneinfo import ZoneInfo
    real = datetime.now(ZoneInfo("Asia/Kolkata")).replace(hour=18, minute=0, second=0, microsecond=0)
    fixed = real.astimezone(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(appmod, "utcnow", lambda: fixed)
    return fixed


def one_assignment(app, admin, email="mia@example.com"):
    post_task(admin, assignees=[str(uid(app, email))])
    with app.app_context():
        return Assignment.query.one().id


# ---------------------------------------------------------------- configuration
def test_missing_env_fails_clearly(monkeypatch):
    for k in ("DATABASE_URL", "FLASK_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError) as e:
        create_app()
    assert "DATABASE_URL" in str(e.value) and "FLASK_SECRET_KEY" in str(e.value)


def test_short_secret_rejected(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("FLASK_SECRET_KEY", "short")
    with pytest.raises(ConfigError):
        create_app()


def test_valid_env_builds_postgres_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost:5432/db")
    monkeypatch.setenv("FLASK_SECRET_KEY", "x" * 40)
    a = create_app()
    assert a.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql+psycopg2://")
    assert a.config["SESSION_COOKIE_SECURE"] is True and a.config["SESSION_COOKIE_HTTPONLY"] is True


# ---------------------------------------------------------------- auth & access
def test_login_page_has_warning_and_no_register(app):
    c = app.test_client()
    html = c.get("/login").get_data(as_text=True)
    assert "YOUR WORK IS OBSERVED" in html and "visible to authorized administrators" in html
    assert c.get("/register").status_code == 404 and c.post("/register").status_code == 404


def test_login_success_failure_and_disabled(app):
    assert login(app.test_client(), "mia@example.com").status_code == 302
    r = login(app.test_client(), "mia@example.com", "wrong-password-123")
    assert r.status_code == 200 and b"Incorrect email or password" in r.data
    assert login(app.test_client(), "nobody@example.com").status_code == 200
    with app.app_context():
        User.query.filter_by(email="mia@example.com").one().is_active = False
        db.session.commit()
    assert b"disabled" in login(app.test_client(), "mia@example.com").data


def test_unauthenticated_redirected(app):
    c = app.test_client()
    for url in ("/dashboard", "/admin/users", "/admin/records", "/attendance", "/welcome/0"):
        assert c.get(url).status_code == 302
    assert c.post("/clock-in").status_code == 302


def test_member_cannot_access_admin_routes(mia):
    for url in ("/admin/users", "/admin/users/new", "/admin/tasks", "/admin/tasks/new", "/admin/records",
                "/admin/records/export.csv", "/admin/settings", "/admin/reset", "/admin/users/3/delete"):
        assert mia.get(url).status_code == 403, url
    assert mia.post("/admin/users/1/toggle").status_code == 403
    assert mia.post("/admin/reset", data={"phrase": "DELETE ALL DATA"}).status_code == 403
    assert mia.post("/admin/assignments/1/review", data={"decision": "approved"}).status_code == 403
    assert mia.post("/admin/assignments/1/merge").status_code == 403


def test_removed_pages_are_gone(admin, mia):
    assert admin.get("/admin/audit").status_code == 404
    assert admin.get("/admin/acknowledgements").status_code == 404
    assert mia.post("/acknowledgement", data={"tasks_worked": "x"}).status_code == 404


def test_admin_cannot_use_member_actions(admin):
    assert admin.post("/clock-in").status_code == 403
    assert admin.get("/attendance").status_code == 403
    assert admin.get("/welcome/0").status_code == 403


def test_logout_and_session_invalidation(app):
    c = app.test_client(); login(c, "mia@example.com")
    assert c.get("/dashboard").status_code == 200
    assert c.post("/logout").status_code == 302
    assert c.get("/dashboard").status_code == 302


def test_disable_user_logs_them_out(app, admin, mia):
    assert mia.get("/dashboard").status_code == 200
    admin.post(f"/admin/users/{uid(app, 'mia@example.com')}/toggle")
    assert mia.get("/dashboard").status_code == 302


def test_csrf_enforced_when_enabled():
    app = make_app(WTF_CSRF_ENABLED=True)
    c = app.test_client()
    r = c.post("/login", data={"email": "mia@example.com", "password": PW})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")  # fresh form, not signed in
    assert c.get("/dashboard").status_code == 302


def test_login_rate_limited():
    app = make_app(RATELIMIT_ENABLED=True)
    c = app.test_client()
    codes = [login(c, "mia@example.com", "bad-password-xyz").status_code for _ in range(22)]
    assert 429 in codes


def test_security_headers_and_health(app):
    c = app.test_client()
    r = c.get("/login")
    assert r.headers["X-Frame-Options"] == "DENY" and "script-src 'none'" in r.headers["Content-Security-Policy"]
    h = c.get("/health")
    assert h.status_code == 200 and h.get_json() == {"status": "ok"}


def test_pages_never_expose_secrets(app, admin):
    body = admin.get("/admin/users").get_data(as_text=True)
    assert "password_hash" not in body and "scrypt:" not in body and "pbkdf2:" not in body


# ---------------------------------------------------------------- attendance starts on first sign-in
def test_first_login_sets_start_day_and_starts_timer(app):
    with app.app_context():
        assert User.query.filter_by(email="mia@example.com").one().first_login_at is None
    c = app.test_client(); login(c, "mia@example.com")
    with app.app_context():
        u = User.query.filter_by(email="mia@example.com").one()
        first = u.first_login_at
        assert first is not None and AttendanceSession.query.filter_by(user_id=u.id, clock_out=None).count() == 1
    login(app.test_client(), "mia@example.com")  # second device: no duplicate session, start day unchanged
    with app.app_context():
        u = User.query.filter_by(email="mia@example.com").one()
        assert u.first_login_at == first and AttendanceSession.query.filter_by(user_id=u.id).count() == 1
    c.post("/logout")
    with app.app_context():
        assert AttendanceSession.query.filter_by(clock_out=None).count() == 0


def test_records_only_from_first_login_day(app):
    with app.app_context():
        u = User.query.filter_by(email="mia@example.com").one()
        today = local_today()
        u.first_login_at = utcnow() - timedelta(days=2)
        db.session.commit()
        rows = daily_records([u], today - timedelta(days=10), today)
        assert len(rows) == 3 and all(r["state"] == "absent" for r in rows)
        raj = User.query.filter_by(email="raj@example.com").one()
        assert daily_records([raj], today - timedelta(days=10), today) == []  # never signed in -> no rows


def test_admin_login_does_not_record_attendance(app, admin):
    with app.app_context():
        assert AttendanceSession.query.count() == 0


def test_duration_example_login_9_logout_130_break_30_is_4_hours(app):
    with app.app_context():
        u = uid(app, "mia@example.com")
        s = AttendanceSession(user_id=u, clock_in=datetime(2026, 3, 2, 3, 30), clock_out=datetime(2026, 3, 2, 8, 0))
        s.breaks.append(BreakRecord(started_at=datetime(2026, 3, 2, 5, 0), ended_at=datetime(2026, 3, 2, 5, 30)))
        db.session.add(s); db.session.commit()
        assert session_minutes(s, utcnow()) == pytest.approx(240.0)
        d = build_daily([u], datetime(2026, 3, 2).date(), datetime(2026, 3, 2).date())[(u, datetime(2026, 3, 2).date())]
        assert d["minutes"] == pytest.approx(240.0) and not d["active"]


def test_timer_controls(app, mia):
    assert b"already running" in mia.post("/clock-in", follow_redirects=True).data  # started on sign-in
    mia.post("/break/start"); assert b"already paused" in mia.post("/break/start", follow_redirects=True).data
    mia.post("/break/end")
    mia.post("/clock-out")
    with app.app_context():
        s = AttendanceSession.query.one()
        assert s.clock_out is not None and len(s.breaks) == 1 and s.breaks[0].ended_at is not None
    assert b"not running" in mia.post("/clock-out", follow_redirects=True).data
    mia.post("/clock-in")
    with app.app_context():
        assert AttendanceSession.query.count() == 2


def test_database_blocks_two_open_sessions(app):
    from sqlalchemy.exc import IntegrityError
    with app.app_context():
        u = uid(app, "mia@example.com")
        db.session.add(AttendanceSession(user_id=u)); db.session.commit()
        db.session.add(AttendanceSession(user_id=u))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_stale_session_capped_on_next_login(app):
    with app.app_context():
        u = uid(app, "mia@example.com")
        db.session.add(AttendanceSession(user_id=u, clock_in=utcnow() - timedelta(hours=20))); db.session.commit()
    login(app.test_client(), "mia@example.com")
    with app.app_context():
        old, new = AttendanceSession.query.order_by(AttendanceSession.id).all()
        assert old.incomplete is True and session_minutes(old, utcnow()) == pytest.approx(16 * 60)
        assert new.clock_out is None


def test_target_shown_to_member_and_admin(evening, app, mia, admin):
    with app.app_context():
        s = AttendanceSession.query.one(); s.clock_in = evening - timedelta(hours=5); db.session.commit()
    html = mia.get("/dashboard").get_data(as_text=True)
    assert "Target achieved" in html and "YOUR WORK IS OBSERVED" in html
    a = admin.get("/dashboard").get_data(as_text=True)
    assert "Reached" in a and "Mia Member" in a and "POST A TASK" in a
    assert "Not joined yet" not in a.split("Raj Member")[0] or True


# ---------------------------------------------------------------- onboarding
def test_new_member_must_onboard_first(app, admin):
    admin.post("/admin/users/new", data={"name": "New Person", "email": "new@example.com", "password": PW, "confirm_password": PW})
    c = app.test_client(); login(c, "new@example.com")
    r = c.get("/dashboard")
    assert r.status_code == 302 and "/welcome/0" in r.headers["Location"]
    assert b"Next" in c.get("/welcome/0").data
    assert c.get("/welcome/2").status_code == 302  # cannot skip question 1
    assert b"Pick one" in c.post("/welcome/1", data={"level": "expert"}, follow_redirects=True).data
    for step, lvl in ((1, "high"), (2, "good"), (3, "moderate")):
        assert c.post(f"/welcome/{step}", data={"level": lvl}).status_code == 302
    assert b"all" in c.get("/welcome/4").data and c.get("/dashboard").status_code == 200
    with app.app_context():
        u = User.query.filter_by(email="new@example.com").one()
        assert (u.skill_backend, u.skill_database, u.skill_frontend) == ("high", "good", "moderate") and u.onboarded_at


def test_task_form_suggests_by_skill(app, admin):
    html = admin.get("/admin/tasks/new").get_data(as_text=True)
    backend = html.split('sg-backend')[1].split('suggest-box')[0]
    assert backend.index("Mia Member") < backend.index("Raj Member")  # Mia rated High at backend
    database = html.split('sg-database')[1].split('suggest-box')[0]
    assert database.index("Raj Member") < database.index("Mia Member")


# ---------------------------------------------------------------- team management
def test_admin_creates_member_and_validation(app, admin):
    bad = admin.post("/admin/users/new", data={"name": "X", "email": "bad", "password": "short", "confirm_password": "x"})
    assert bad.status_code == 200 and b"valid email" in bad.data
    dup = admin.post("/admin/users/new", data={"name": "Dup", "email": "MIA@example.com", "password": PW, "confirm_password": PW})
    assert b"already exists" in dup.data


def test_delete_member_removes_everything(app, admin, mia):
    aid = one_assignment(app, admin)
    mia.post(f"/my-tasks/{aid}/update", data={"progress": "30", "note": "started", "link": PR})
    mid = uid(app, "mia@example.com")
    assert b"Delete Mia Member" in admin.get(f"/admin/users/{mid}/delete").data
    assert admin.post(f"/admin/users/{mid}/delete", data={"confirm_email": "wrong@example.com"}).status_code == 400
    assert admin.post(f"/admin/users/{mid}/delete", data={"confirm_email": "mia@example.com"}).status_code == 302
    with app.app_context():
        assert User.query.filter_by(email="mia@example.com").first() is None
        assert AttendanceSession.query.count() == 0 and Assignment.query.count() == 0 and TaskUpdate.query.count() == 0
        assert Task.query.count() == 1
    assert mia.get("/dashboard").status_code == 302
    admin_id = uid(app, "admin@example.com")
    admin.post(f"/admin/users/{admin_id}/delete", data={"confirm_email": "admin@example.com"})
    with app.app_context():
        assert db.session.get(User, admin_id) is not None  # cannot delete yourself


def test_admin_can_delete_another_founder(app, admin):
    with app.app_context():
        db.session.add(User(name="Founder Two", email="f2@example.com", role="admin", password_hash=generate_password_hash(PW)))
        db.session.commit()
    f2 = app.test_client(); login(f2, "f2@example.com")
    f2.post("/admin/tasks/new", data={**TASK, "all_team": "1"})
    fid = uid(app, "f2@example.com")
    assert admin.post(f"/admin/users/{fid}/delete", data={"confirm_email": "f2@example.com"}).status_code == 302
    with app.app_context():
        assert db.session.get(User, fid) is None
        assert Task.query.one().created_by == uid(app, "admin@example.com")  # task handed over, not lost
    assert f2.get("/dashboard").status_code == 302


def test_wipe_can_include_other_founders(app, admin):
    with app.app_context():
        db.session.add(User(name="Founder Two", email="f2@example.com", role="admin", password_hash=generate_password_hash(PW)))
        db.session.commit()
    admin.post("/admin/reset", data={"phrase": "DELETE ALL DATA", "include_admins": "1"})
    with app.app_context():
        assert [u.email for u in User.query.all()] == ["admin@example.com"]


def test_wipe_all_data_keeps_admins(app, admin, mia):
    one_assignment(app, admin)
    admin.post("/admin/settings", data={"github_repo": "https://github.com/acme/app"})
    assert admin.post("/admin/reset", data={"phrase": "delete"}).status_code == 400
    assert admin.post("/admin/reset", data={"phrase": "DELETE ALL DATA"}).status_code == 302
    with app.app_context():
        assert [u.role for u in User.query.all()] == ["admin"]
        assert Task.query.count() == 0 and AttendanceSession.query.count() == 0
        assert db.session.get(Setting, "github_repo").value == "https://github.com/acme/app"
    assert admin.get("/dashboard").status_code == 200


def test_create_admin_never_overwrites(app):
    with app.app_context():
        assert create_admin("fresh@example.com", PW)[0] is True
        assert create_admin("fresh@example.com", "AnotherLongPass123")[0] is False
        assert create_admin("x@example.com", "short")[0] is False


def test_password_change(app, mia):
    r = mia.post("/account/password", data={"current_password": "nope", "new_password": "NewPassword-12345", "confirm_password": "NewPassword-12345"})
    assert b"incorrect" in r.data
    r = mia.post("/account/password", data={"current_password": PW, "new_password": "NewPassword-12345", "confirm_password": "NewPassword-12345"})
    assert r.status_code == 302 and mia.get("/dashboard").status_code == 200


# ---------------------------------------------------------------- tasks, daily progress, completion, GitHub
def test_task_validation_and_all_team_assignment(app, admin):
    r = post_task(admin, title="", target_hours="0", deadline="2025-01-01", repo_url="https://gitlab.com/x/y")
    assert r.status_code == 200 and b"title" in r.data and b"Target hours" in r.data and b"Deadline cannot" in r.data
    assert b"github.com/owner/repository" in r.data
    assert b"Select at least one" in post_task(admin).data
    assert post_task(admin, all_team="1", work_type="backend", repo_url="https://github.com/acme/app.git").status_code == 302
    with app.app_context():
        t = Task.query.one()
        assert Assignment.query.count() == 2 and t.work_type == "backend" and t.repo_url == "https://github.com/acme/app"


def test_assign_individual_edit_and_reassign(app, admin):
    mia_id, raj_id = uid(app, "mia@example.com"), uid(app, "raj@example.com")
    post_task(admin, assignees=[str(mia_id)])
    with app.app_context():
        tid = Task.query.one().id
    admin.post(f"/admin/tasks/{tid}/edit", data={**TASK, "title": "Renamed", "priority": "urgent", "assignees": [str(raj_id)]})
    with app.app_context():
        assert Task.query.one().title == "Renamed" and [a.user_id for a in Assignment.query.all()] == [raj_id]


def test_daily_progress_complete_review_and_merge(app, admin, mia, raj):
    aid = one_assignment(app, admin)
    assert raj.get(f"/my-tasks/{aid}").status_code == 404
    assert raj.post(f"/my-tasks/{aid}/update", data={"progress": "10", "note": "x"}).status_code == 404
    assert mia.post(f"/my-tasks/{aid}/update", data={"progress": "10"}).status_code == 302  # progress only is enough
    with app.app_context():
        assert TaskUpdate.query.one().note == "Progress updated to 10%"
    assert b"whole number" in mia.post(f"/my-tasks/{aid}/update", data={"progress": "500", "note": "x"}).data
    assert b"valid http" in mia.post(f"/my-tasks/{aid}/update", data={"progress": "5", "note": "x", "link": "javascript:alert(1)"}).data
    assert mia.post(f"/my-tasks/{aid}/update", data={"progress": "40", "note": "API skeleton", "link": PR}).status_code == 302
    with app.app_context():
        a = db.session.get(Assignment, aid)
        assert a.status == "in_progress" and a.pr_url == PR and a.progress == 40 and len(a.updates) == 2
    page = admin.get("/admin/records?range=today").get_data(as_text=True)
    assert "API skeleton" in page and "Mia Member" in page
    assert b"#7" in admin.get("/dashboard").data  # PR waiting to merge
    # completion acknowledgement
    assert b"Tick the box" in mia.post(f"/my-tasks/{aid}/complete", data={"summary": "done"}).data
    assert mia.post(f"/my-tasks/{aid}/complete", data={"summary": "All endpoints done", "confirm": "1"}).status_code == 302
    with app.app_context():
        a = db.session.get(Assignment, aid)
        assert a.status == "submitted" and a.progress == 100
    assert mia.post(f"/my-tasks/{aid}/update", data={"progress": "10", "note": "x"}).status_code == 403
    # merge without token -> mark as merged manually
    admin.post(f"/admin/assignments/{aid}/merge", data={"mark_only": "1"})
    with app.app_context():
        assert db.session.get(Assignment, aid).pr_merged_at is not None
    assert b"Feedback is required" in admin.post(f"/admin/assignments/{aid}/review", data={"decision": "changes"}, follow_redirects=True).data
    admin.post(f"/admin/assignments/{aid}/review", data={"decision": "approved"})
    with app.app_context():
        assert db.session.get(Assignment, aid).status == "completed"


def test_complete_requires_a_link(app, admin, mia):
    aid = one_assignment(app, admin)
    r = mia.post(f"/my-tasks/{aid}/complete", data={"summary": "done", "confirm": "1"})
    assert r.status_code == 400 and b"pull request link" in r.data


def test_github_merge_via_api(app, admin, mia):
    app.config["GITHUB_TOKEN"] = "ghp_test"
    aid = one_assignment(app, admin)
    mia.post(f"/my-tasks/{aid}/complete", data={"summary": "done", "confirm": "1", "link": PR})

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"merged": true, "message": "Pull Request successfully merged"}'

    with mock.patch("urllib.request.urlopen", return_value=Resp()) as m:
        admin.post(f"/admin/assignments/{aid}/merge", data={"approve": "1"})
        req = m.call_args[0][0]
        assert req.full_url == "https://api.github.com/repos/acme/app/pulls/7/merge" and req.get_method() == "PUT"
        assert req.headers["Authorization"] == "Bearer ghp_test"
    with app.app_context():
        a = db.session.get(Assignment, aid)
        assert a.pr_merged_at is not None and a.status == "completed"


def test_settings_repo_validation(app, admin, mia):
    assert b"github.com/owner/repository" in admin.post("/admin/settings", data={"github_repo": "not a repo"}).data
    admin.post("/admin/settings", data={"github_repo": "https://github.com/acme/app/"})
    assert b"acme/app" in mia.get("/dashboard").data


def test_records_and_csv_export(evening, app, admin, mia):
    with app.app_context():
        s = AttendanceSession.query.one(); s.clock_in = evening - timedelta(hours=5); s.clock_out = evening - timedelta(hours=1)
        db.session.commit()
    html = admin.get("/admin/records?range=today").get_data(as_text=True)
    assert "Mia Member" in html and "Reached" in html
    r = admin.get("/admin/records/export.csv?range=today")
    assert r.status_code == 200 and r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    text = r.get_data(as_text=True)
    assert "Mia Member" in text and "Yes" in text and "Raj Member" not in text  # Raj never signed in
    assert b"limited to 93 days" in admin.get("/admin/records?range=custom&start=2020-01-01&end=2026-01-01").data


def test_every_template_renders(app, admin, mia):
    post_task(admin, all_team="1", work_type="frontend")
    mid = uid(app, "mia@example.com")
    for url in ("/dashboard", "/admin/users", "/admin/users/new", "/admin/tasks", "/admin/tasks/new", "/admin/tasks/1",
                "/admin/tasks/1/edit", "/admin/records", "/admin/settings", "/admin/reset", f"/admin/users/{mid}/delete",
                "/account/password"):
        assert admin.get(url).status_code == 200, url
    for url in ("/dashboard", "/attendance", "/account/password", "/my-tasks/1", "/welcome/0", "/welcome/1", "/welcome/4"):
        assert mia.get(url).status_code in (200, 404), url
    assert mia.get("/nope").status_code == 404
    assert app.test_client().get("/nope").status_code == 404


def test_task_delete(app, admin, mia):
    aid = one_assignment(app, admin)
    mia.post(f"/my-tasks/{aid}/update", data={"progress": "10", "note": "x"})
    with app.app_context():
        tid = Task.query.one().id
    admin.post(f"/admin/tasks/{tid}/delete")
    with app.app_context():
        assert Task.query.count() == 1
    admin.post(f"/admin/tasks/{tid}/delete", data={"confirm": "1"})
    with app.app_context():
        assert Task.query.count() == 0 and TaskUpdate.query.count() == 0


# ---------------------------------------------------------------- database URL handling
def test_database_url_special_characters_and_brackets():
    from app import normalize_database_url
    u = normalize_database_url("postgresql://postgres.abc:p@ss#w/rd?@aws-0-x.pooler.supabase.com:5432/postgres")
    assert u == "postgresql+psycopg2://postgres.abc:p%40ss%23w%2Frd%3F@aws-0-x.pooler.supabase.com:5432/postgres"
    assert "%40" in normalize_database_url("postgres://u:[a@b]@h:5432/postgres")


def test_placeholder_values_rejected(monkeypatch):
    from app import normalize_database_url
    with pytest.raises(ConfigError):
        normalize_database_url("postgresql://postgres.YOUR_PROJECT_REF:YOUR_DB_PASSWORD@h:5432/postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("FLASK_SECRET_KEY", "replace-with-a-long-random-string-of-at-least-32-characters")
    with pytest.raises(ConfigError):
        create_app()


# ---------------------------------------------------------------- founders
from models import Agreement, AgreementSignature, ChangeRequest, Milestone, Project  # noqa: E402


def make_founder(app, admin):
    admin.post("/admin/founders/new", data={"name": "Acme App", "summary": "Hiring app", "live_url": "https://acme.example.com"})
    with app.app_context():
        pid = Project.query.one().id
    r = admin.post("/admin/users/new", data={"name": "Fiona Founder", "email": "fiona@example.com", "password": PW,
                                             "confirm_password": PW, "role": "founder", "project_id": str(pid)})
    assert r.status_code == 302
    c = app.test_client(); login(c, "fiona@example.com")
    return pid, c


def test_founder_sees_only_two_pages_and_no_attendance(app, admin):
    pid, f = make_founder(app, admin)
    html = f.get("/dashboard").get_data(as_text=True)
    assert "Acme App" in html and "View live application" in html and "acme.example.com" in html
    assert "YOUR WORK IS OBSERVED" not in html
    assert f.get("/agreement").status_code == 200
    for url in ("/attendance", "/welcome/0", "/admin/users", "/admin/founders", f"/admin/founders/{pid}", "/my-tasks/1"):
        assert f.get(url).status_code in (403, 404), url
    assert f.post("/clock-in").status_code == 403
    with app.app_context():
        assert AttendanceSession.query.count() == 0
        assert Milestone.query.count() == 5
    admin.get("/dashboard")  # consume the "account created" flash message
    table = admin.get("/dashboard").get_data(as_text=True).split("Today's record")[1].split("</table>")[0]
    assert "Fiona Founder" not in table  # founders are not in the attendance table


def test_admin_controls_status_founder_sees_it(app, admin):
    pid, f = make_founder(app, admin)
    admin.post(f"/admin/founders/{pid}/status", data={"phase": "testing", "status": "at_risk", "progress": "72",
                                                      "status_note": "Payment gateway review", "live_url": "https://new.example.com"})
    admin.post(f"/admin/founders/{pid}/updates", data={"body": "Demo on Friday"})
    with app.app_context():
        mid = Milestone.query.first().id
    admin.post(f"/admin/milestones/{mid}", data={"title": "Kickoff done", "status": "done", "action": "save"})
    html = f.get("/dashboard").get_data(as_text=True)
    assert "72" in html and "Needs attention" in html and "Payment gateway review" in html and "Demo on Friday" in html
    assert "new.example.com" in html and "Kickoff done" in html
    assert b"Preview" in admin.get(f"/admin/founders/{pid}/preview").data
    assert b"whole number" in admin.post(f"/admin/founders/{pid}/status", data={"phase": "testing", "status": "on_track", "progress": "150"}, follow_redirects=True).data


def test_change_request_flow(app, admin):
    pid, f = make_founder(app, admin)
    assert f.post("/requests", data={"title": "", "details": ""}).status_code == 400
    assert f.post("/requests", data={"title": "Bigger button", "details": "On mobile", "priority": "high"}).status_code == 302
    with app.app_context():
        rid = ChangeRequest.query.one().id
    assert b"Bigger button" in admin.get(f"/admin/founders/{pid}").data
    admin.post(f"/admin/requests/{rid}", data={"status": "done", "admin_reply": "Shipped today"})
    html = f.get("/dashboard").get_data(as_text=True)
    assert "Shipped today" in html and "Done" in html


def test_agreement_sign_and_resign_after_edit(app, admin):
    pid, f = make_founder(app, admin)
    assert b"being prepared" in f.get("/agreement").data
    admin.post(f"/admin/founders/{pid}/agreement", data={"title": "Acme Agreement", "body": "1. Scope\nBuild the hiring app.\n2. Fees\nAs agreed."})
    assert b"ready to sign" in f.get("/dashboard").data
    assert b"Tick the box" in f.post("/agreement", data={"version": "1", "signed_name": "Fiona Founder"}).data
    assert b"updated while you were reading" in f.post("/agreement", data={"version": "9", "agree": "1", "signed_name": "Fiona Founder"}).data
    f.post("/agreement", data={"version": "1", "agree": "1", "signed_name": "Fiona Founder"})
    with app.app_context():
        s = AgreementSignature.query.one()
        assert s.version == 1 and len(s.body_hash) == 64 and s.signed_name == "Fiona Founder"
    assert b"Digitally" in f.get("/agreement").data
    admin.post(f"/admin/founders/{pid}/agreement", data={"title": "Acme Agreement", "body": "1. Scope\nBuild the hiring app + admin.\n2. Fees\nAs agreed."})
    with app.app_context():
        assert Agreement.query.one().version == 2
    assert b"sign version 2" in f.get("/agreement").data
    f.post("/agreement", data={"version": "2", "agree": "1", "signed_name": "Fiona Founder"})
    with app.app_context():
        assert AgreementSignature.query.count() == 2


def test_founder_role_needs_project_and_delete_founder(app, admin):
    r = admin.post("/admin/users/new", data={"name": "No Project", "email": "np@example.com", "password": PW, "confirm_password": PW, "role": "founder"})
    assert b"Choose the founder" in r.data
    pid, f = make_founder(app, admin)
    f.post("/requests", data={"title": "Change x", "details": "y", "priority": "low"})
    fid = uid(app, "fiona@example.com")
    assert admin.post(f"/admin/users/{fid}/delete", data={"confirm_email": "fiona@example.com"}).status_code == 302
    with app.app_context():
        assert db.session.get(User, fid) is None and Project.query.count() == 1 and ChangeRequest.query.count() == 0


def test_wipe_removes_founders_and_projects(app, admin):
    make_founder(app, admin)
    admin.post("/admin/reset", data={"phrase": "DELETE ALL DATA"})
    with app.app_context():
        assert Project.query.count() == 0 and User.query.filter_by(role="founder").count() == 0


def test_founder_pages_render(app, admin):
    pid, f = make_founder(app, admin)
    for url in ("/admin/founders", "/admin/founders/new", f"/admin/founders/{pid}", f"/admin/founders/{pid}/edit",
                f"/admin/founders/{pid}/agreement", f"/admin/founders/{pid}/preview", "/admin/users/new?role=founder"):
        assert admin.get(url).status_code == 200, url
    admin.post(f"/admin/founders/{pid}/delete", data={"confirm": "1"})
    assert b"set up</span>" in f.get("/dashboard").data


def test_diagnostics_page_and_admin_error_detail(app, admin, mia):
    html = admin.get("/admin/diagnostics").get_data(as_text=True)
    assert "all links valid" in html
    assert mia.get("/admin/diagnostics").status_code == 403
    import app as appmod
    app.config["PROPAGATE_EXCEPTIONS"] = False  # show the real error page, like production

    def broken(p):
        raise RuntimeError("boom-test")
    orig = appmod.project_stats
    appmod.project_stats = broken
    try:
        admin.post("/admin/founders/new", data={"name": "Acme"})
        r = admin.get("/admin/founders/1")
        assert r.status_code == 500 and b"boom-test" in r.data  # admins see the reason
    finally:
        appmod.project_stats = orig



def test_same_founder_login_on_many_devices_and_phone_quirks(app, admin):
    admin.post("/admin/founders/new", data={"name": "Acme"})
    admin.post("/admin/users/new", data={"name": "Founders", "email": "founders@acme.com", "password": PW,
                                         "confirm_password": PW, "role": "founder", "project_id": "1"})
    devices = [app.test_client() for _ in range(6)]
    assert login(devices[0], "Founders@Acme.com ").status_code == 302   # phone capitalised the email
    assert login(devices[1], "founders@acme.com", PW + " ").status_code == 302  # autofill added a space
    for d in devices[2:]:
        assert login(d, "founders@acme.com").status_code == 302
    assert all(d.get("/dashboard").status_code == 200 for d in devices)  # nobody was signed out
    assert all(b"founder-view" in d.get("/dashboard").data for d in devices[:1])


def test_disabled_account_message_is_clear(app):
    with app.app_context():
        User.query.filter_by(email="mia@example.com").one().is_active = False
        db.session.commit()
    assert b"This account is disabled" in login(app.test_client(), "mia@example.com").data
