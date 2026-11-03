import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.security import generate_password_hash

from app import create_app
from models import User, db, utcnow

PW = "CorrectHorse-Battery9"
TEST_CONFIG = {"TESTING": True, "SECRET_KEY": "k" * 40, "SQLALCHEMY_DATABASE_URI": "sqlite://",
               "WTF_CSRF_ENABLED": False, "RATELIMIT_ENABLED": False, "SESSION_COOKIE_SECURE": False,
               "APP_TIMEZONE": "Asia/Kolkata", "DAILY_TARGET_MINUTES": 240, "SESSION_MAX_HOURS": 16}


def make_app(**extra):
    app = create_app({**TEST_CONFIG, **extra})
    with app.app_context():
        db.create_all()
        db.session.add(User(name="Ada Admin", email="admin@example.com", role="admin", password_hash=generate_password_hash(PW)))
        db.session.add(User(name="Mia Member", email="mia@example.com", role="member", password_hash=generate_password_hash(PW),
                            skill_backend="high", skill_database="good", skill_frontend="moderate", onboarded_at=utcnow()))
        db.session.add(User(name="Raj Member", email="raj@example.com", role="member", password_hash=generate_password_hash(PW),
                            skill_backend="moderate", skill_database="high", skill_frontend="high", onboarded_at=utcnow()))
        db.session.commit()
    return app


@pytest.fixture()
def app():
    return make_app()


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


@pytest.fixture()
def admin(app):
    c = app.test_client(); login(c, "admin@example.com"); return c


@pytest.fixture()
def mia(app):
    c = app.test_client(); login(c, "mia@example.com"); return c


@pytest.fixture()
def raj(app):
    c = app.test_client(); login(c, "raj@example.com"); return c
