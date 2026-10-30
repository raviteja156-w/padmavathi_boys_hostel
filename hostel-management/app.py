"""
Hostel Management System
A complete, production-ready Flask application for managing hostel students.
"""
import os
import re
import io
import base64
import calendar
import json
import secrets
import uuid
from datetime import datetime, timedelta, date
from functools import wraps

import time
import threading
import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, send_from_directory, jsonify, abort, g, send_file
)
from werkzeug.utils import secure_filename
from PIL import Image
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
    PageBreak, Image as RLImage, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8 MB max upload
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'static', 'uploads')

# Supabase / Postgres connection string, e.g.
# postgresql://postgres.xxxxxxxx:[PASSWORD]@aws-0-xx-xxxx-1.pooler.supabase.com:6543/postgres
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL is not set. Add your Supabase connection string to the environment.')

# Supabase Storage — used for student profile photos so they survive Render restarts/redeploys.
# SUPABASE_SERVICE_ROLE_KEY is a server-only secret: it is read here from the environment and is
# never sent to templates/JS, so it's never exposed to the browser.
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').strip().rstrip('/')
SUPABASE_SERVICE_ROLE_KEY = (os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or '').strip()
SUPABASE_STORAGE_BUCKET = os.environ.get('SUPABASE_STORAGE_BUCKET', 'student-photos').strip()
if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set for photo storage.')

SUPABASE_PUBLIC_PREFIX = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/"

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

ALLOWED_EXTENSIONS = {'jpg', 'jpeg'}
ALLOWED_RECEIPT_EXTENSIONS = {'jpg', 'jpeg', 'png'}
PAYMENT_MODES = ['Online', 'Cash', 'Online + Cash']

# All "today" / "this month" logic uses Indian Standard Time (UTC+5:30).
IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist():
    return datetime.utcnow() + IST_OFFSET


def today_ist():
    return now_ist().date()


# Payments dated before this day are ignored for students who have no joined date.
try:
    LAUNCH_DATE = datetime.strptime(os.environ.get('LAUNCH_DATE', '2026-10-01').strip(), '%Y-%m-%d').date()
except ValueError:
    LAUNCH_DATE = date(2026, 10, 1)


HOSTELS = ['Old Hostel', 'New Hostel']
ROOMS = list(range(1, 11))
SHARINGS = list(range(1, 10))  # 1..9 (9-sharing needed for the New Hostel Hall)

# The New Hostel has one extra, non-numbered room: the "Hall" (9-sharing).
# It's stored internally as room_number = 11 so no column type changes are needed.
HALL_ROOM_NUMBER = 11
HALL_HOSTEL = 'New Hostel'


def rooms_for_hostel(hostel):
    """Room numbers to iterate for a given hostel — adds the Hall for New Hostel only."""
    if hostel == HALL_HOSTEL:
        return ROOMS + [HALL_ROOM_NUMBER]
    return ROOMS


def room_label(room_number):
    return 'Hall' if room_number == HALL_ROOM_NUMBER else f'Room {room_number}'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# --------------------------------------------------------------------------
# DATABASE HELPERS
# --------------------------------------------------------------------------

class DBWrapper:
    """Thin wrapper so the rest of the app can keep calling db.execute(...).fetchone()
    the same way it did with sqlite3, but backed by psycopg2 + Supabase Postgres."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params or [])
        return cur

    def commit(self):
        self.conn.commit()


# SPEED: opening a new connection to Supabase takes about a second (network + SSL), and the app used to do that
# on EVERY page. Connections are now kept open and reused (a small pool per server process).
_POOL = None
_POOL_PID = None
_POOL_LOCK = threading.Lock()
_LAST_USED = {}          # id(conn) -> time it was last handed back
POOL_MAX = 8
IDLE_CHECK_SECONDS = 60  # a connection idle longer than this is tested before use (Supabase may drop idle ones)


def _pool():
    global _POOL, _POOL_PID
    if _POOL is None or _POOL_PID != os.getpid():
        with _POOL_LOCK:
            if _POOL is None or _POOL_PID != os.getpid():
                _POOL = psycopg2.pool.ThreadedConnectionPool(
                    0, POOL_MAX, DATABASE_URL, connect_timeout=15,
                    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
                _POOL_PID = os.getpid()
    return _POOL


def _conn_ok(conn):
    if conn.closed:
        return False
    if time.time() - _LAST_USED.get(id(conn), 0) < IDLE_CHECK_SECONDS:
        return True
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.close()
        conn.rollback()
        return True
    except Exception:
        return False


def get_conn():
    """A ready connection from the pool (or a fresh one if the pool is full). Give it back with put_conn()."""
    pool = _pool()
    for _ in range(3):
        try:
            conn = pool.getconn()
        except psycopg2.pool.PoolError:
            conn = psycopg2.connect(DATABASE_URL)
            conn._hms_unpooled = True
            return conn
        if _conn_ok(conn):
            conn.autocommit = False
            return conn
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
    return psycopg2.connect(DATABASE_URL)


def put_conn(conn):
    if conn is None:
        return
    try:
        if not conn.closed:
            conn.rollback()        # never leave an open transaction on a reused connection
    except Exception:
        pass
    if getattr(conn, '_hms_unpooled', False):
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        _LAST_USED[id(conn)] = time.time()
        _pool().putconn(conn, close=bool(conn.closed))
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def get_db():
    if 'db' not in g:
        g.db = DBWrapper(get_conn())
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        put_conn(db.conn)


SCHEMA_VERSION = '2026-10-29b'   # change this whenever init_db() below gets a new table / column


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    # SPEED: the ~50 setup statements below only run when the schema version changed (one quick check otherwise).
    try:
        cur.execute("SELECT value FROM app_settings WHERE key = 'schema_version'")
        row = cur.fetchone()
        if row and row[0] == SCHEMA_VERSION:
            cur.close()
            put_conn(conn)
            return
    except Exception:
        pass
    conn.rollback()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY,
            hostel TEXT NOT NULL CHECK(hostel IN ('Old Hostel', 'New Hostel')),
            room_number INTEGER NOT NULL CHECK(room_number BETWEEN 1 AND 11),
            sharing INTEGER NOT NULL CHECK(sharing BETWEEN 1 AND 9),
            name TEXT NOT NULL,
            contact TEXT NOT NULL,
            total_rent REAL NOT NULL DEFAULT 0,
            amount_paid REAL NOT NULL DEFAULT 0,
            balance REAL NOT NULL DEFAULT 0,
            photo_filename TEXT,
            note TEXT NOT NULL DEFAULT '',
            due_date INTEGER CHECK(due_date IS NULL OR due_date BETWEEN 1 AND 31),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_hostel_room ON students(hostel, room_number)')
    # Older deployments already had a students table before these columns/constraints existed.
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS due_date INTEGER")
    # Relax the old constraints so sharing can go up to 9 and room_number can be 11 (the Hall).
    # Existing rows (1-6 sharing, 1-10 rooms) already satisfy these wider bounds, so nothing is touched.
    cur.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_sharing_check")
    cur.execute("ALTER TABLE students ADD CONSTRAINT students_sharing_check CHECK (sharing BETWEEN 1 AND 9)")
    cur.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_room_number_check")
    cur.execute("ALTER TABLE students ADD CONSTRAINT students_room_number_check CHECK (room_number BETWEEN 1 AND 11)")
    cur.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_due_date_check")
    cur.execute("ALTER TABLE students ADD CONSTRAINT students_due_date_check CHECK (due_date IS NULL OR due_date BETWEEN 1 AND 31)")

    # One note per (hostel, room_number) — used to tag empty beds on the Vacancy page.
    # The UNIQUE constraint guarantees a room can never end up with two separate notes.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS room_notes (
            id SERIAL PRIMARY KEY,
            hostel TEXT NOT NULL CHECK(hostel IN ('Old Hostel', 'New Hostel')),
            room_number INTEGER NOT NULL CHECK(room_number BETWEEN 1 AND 10),
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(hostel, room_number)
        )
    ''')
    cur.execute("ALTER TABLE room_notes DROP CONSTRAINT IF EXISTS room_notes_room_number_check")
    cur.execute("ALTER TABLE room_notes ADD CONSTRAINT room_notes_room_number_check CHECK (room_number BETWEEN 1 AND 11)")

    # Join date + payment details (all optional, so every existing student stays valid).
    # Dates are stored as ISO text 'YYYY-MM-DD' (what <input type="date"> submits).
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS date_of_join TEXT")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS payment_date TEXT")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS payment_mode TEXT")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS cash_amount REAL")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS paid_to TEXT")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS receipt_filename TEXT")

    # Every month's payment details are copied here before the 1st-of-month reset,
    # so nothing is ever lost. No foreign key: history must outlive a deleted student.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS payment_history (
            id SERIAL PRIMARY KEY,
            student_id INTEGER,
            hostel TEXT,
            room_number INTEGER,
            name TEXT,
            contact TEXT,
            billing_month TEXT NOT NULL,
            total_rent REAL,
            amount_paid REAL,
            balance REAL,
            payment_date TEXT,
            payment_mode TEXT,
            cash_amount REAL,
            paid_to TEXT,
            receipt_filename TEXT,
            note TEXT,
            archived_at TEXT NOT NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_history_month ON payment_history(billing_month)')

    # Simple key/value store for site-wide settings (payment QR image URL, amount due, etc.)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    ''')

    # One row per student payment submission (student-reported; admin must confirm).
    cur.execute('''
        CREATE TABLE IF NOT EXISTS payment_submissions (
            id SERIAL PRIMARY KEY,
            student_id INTEGER REFERENCES students(id) ON DELETE SET NULL,
            hostel TEXT NOT NULL,
            room_number INTEGER NOT NULL,
            name TEXT NOT NULL,
            contact TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            for_month TEXT NOT NULL DEFAULT '',
            proof_filename TEXT,
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'CONFIRMED')),
            submitted_at TEXT NOT NULL,
            confirmed_at TEXT,
            confirmed_by TEXT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_payment_status ON payment_submissions(status)')
    # How the student says they paid ('Online' or 'Cash'); lets the admin confirmation record it correctly.
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS payment_mode TEXT")
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS cash_amount REAL")
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS paid_to TEXT")
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS payment_date TEXT")
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS cash_amount REAL")
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS payment_date TEXT")
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS paid_to TEXT")
    # Admin can hide a confirmed payment from the "Today's Payments" list; the record itself is kept
    # (it still feeds the PDF "payments recorded this month" section).
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS dismissed INTEGER NOT NULL DEFAULT 0")
    # Admin can remove an entry from the "Paid - Not Confirmed" page. This separate table only remembers
    # that the notice was dismissed; no student or payment record is changed or deleted.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS unconfirmed_dismissals (
            student_id INTEGER NOT NULL,
            month_start TEXT NOT NULL,
            amount_paid REAL NOT NULL,
            dismissed_at TEXT NOT NULL,
            PRIMARY KEY (student_id, month_start)
        )
    ''')

    # THIS MONTH PAYMENTS: lets the admin remove a payment from the swipe list after noting it in the
    # manual register. Only remembers the removal (keyed by month + paid amount); no student or payment
    # record is changed. If the student's paid amount changes later, the payment shows up again.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS month_payment_dismissals (
            student_id INTEGER NOT NULL,
            month_start TEXT NOT NULL,
            amount_paid REAL NOT NULL,
            dismissed_at TEXT NOT NULL,
            PRIMARY KEY (student_id, month_start)
        )
    ''')


    # PAUSE BED: a separate, temporary record per paused student. Nothing in students / payments is
    # changed by a pause. Dates are ISO text; end_date is the first day the bed is back in use.
    # Deleting a student also removes only that student's pause row (ON DELETE CASCADE).
    cur.execute('''
        CREATE TABLE IF NOT EXISTS paused_beds (
            id SERIAL PRIMARY KEY,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            start_date TEXT NOT NULL,
            days INTEGER NOT NULL CHECK(days BETWEEN 1 AND 365),
            end_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_paused_student ON paused_beds(student_id)')
    # Lets a student dismiss the green "Payment Confirmed" notification on their dashboard.
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS student_seen INTEGER NOT NULL DEFAULT 0")

    # DELETED STUDENTS: deleting a student now moves a full copy here so the admin can see and RESTORE them.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS deleted_students (
            id SERIAL PRIMARY KEY,
            original_id INTEGER,
            hostel TEXT NOT NULL,
            room_number INTEGER NOT NULL,
            sharing INTEGER NOT NULL,
            name TEXT NOT NULL,
            contact TEXT NOT NULL DEFAULT '',
            total_rent REAL NOT NULL DEFAULT 0,
            amount_paid REAL NOT NULL DEFAULT 0,
            balance REAL NOT NULL DEFAULT 0,
            photo_filename TEXT,
            note TEXT NOT NULL DEFAULT '',
            due_date INTEGER,
            date_of_join TEXT,
            payment_date TEXT,
            payment_mode TEXT,
            cash_amount REAL,
            paid_to TEXT,
            receipt_filename TEXT,
            created_at TEXT,
            deleted_at TEXT NOT NULL,
            deleted_cycle TEXT NOT NULL
        )
    ''')

    # GUESTS: people who stay only for a few days. Kept separate from students and their monthly totals.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS guests (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            contact TEXT NOT NULL DEFAULT '',
            hostel TEXT,
            room_number INTEGER,
            check_in TEXT NOT NULL,
            days INTEGER NOT NULL CHECK(days BETWEEN 1 AND 365),
            rate_per_day REAL NOT NULL DEFAULT 0,
            total_amount REAL NOT NULL DEFAULT 0,
            amount_paid REAL NOT NULL DEFAULT 0,
            payment_mode TEXT,
            paid_to TEXT,
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'STAYING',
            check_out TEXT,
            created_at TEXT NOT NULL
        )
    ''')

    # Old "count this payment in another month" column. No longer used (kept so old databases still load).
    cur.execute("ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS income_month TEXT")
    cur.execute("ALTER TABLE payment_history ADD COLUMN IF NOT EXISTS income_month TEXT")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS income_month TEXT")

    # Audit trail for payments the admin deletes (so a wrong delete can be traced and re-entered).
    cur.execute('''
        CREATE TABLE IF NOT EXISTS payment_delete_log (
            id SERIAL PRIMARY KEY,
            student_id INTEGER,
            name TEXT,
            hostel TEXT,
            room_number INTEGER,
            amount REAL,
            detail TEXT,
            deleted_at TEXT NOT NULL
        )
    ''')

    # Expenditure entries (Canteen / Hostel / Home) and separate canteen income, for the Profit / Loss page.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            category TEXT NOT NULL,
            item TEXT NOT NULL,
            qty REAL NOT NULL DEFAULT 1,
            price REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0,
            expense_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS canteen_income (
            id SERIAL PRIMARY KEY,
            amount REAL NOT NULL,
            income_date TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    ''')

    # Rent months follow the joined date. "Old balance" = balance that Delete All Data added to the Total Rent
    # (shown as a tag until the admin changes it); last_carry = what the LAST Delete All added (still due now).
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS old_balance REAL NOT NULL DEFAULT 0")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS last_carry REAL NOT NULL DEFAULT 0")
    # "Already paid" pressed by the admin: the due date of the rent month he confirmed as fully paid.
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS settled_on TEXT")

    # DELETE ALL DATA: one row per run. 'snapshot' keeps everything that was removed so it can be REVERTED;
    # PERMANENTLY DELETE empties it (status PURGED) so the data is really gone from the database.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS data_cleanups (
            id SERIAL PRIMARY KEY,
            created_at TEXT NOT NULL,
            created_by TEXT,
            cleared_on TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'DELETED',
            summary TEXT NOT NULL DEFAULT '{}',
            snapshot TEXT NOT NULL DEFAULT ''
        )
    ''')

    # ADMIN NOTES: the admin's own notes.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS admin_notes (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    cur.execute('''INSERT INTO app_settings (key, value) VALUES ('schema_version', %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value''', (SCHEMA_VERSION,))
    conn.commit()
    cur.close()
    put_conn(conn)


# --------------------------------------------------------------------------
# UTILITIES
# --------------------------------------------------------------------------

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def validate_mobile(number):
    number = (number or '').strip().replace(' ', '').replace('-', '')
    if number.startswith('+91'):
        number = number[3:]
    elif number.startswith('91') and len(number) == 12:
        number = number[2:]
    return bool(re.fullmatch(r'[6-9]\d{9}', number)), number


def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


app.jinja_env.globals['csrf_token'] = generate_csrf_token
app.jinja_env.globals['HOSTELS'] = HOSTELS
app.jinja_env.globals['ROOMS'] = ROOMS
app.jinja_env.globals['SHARINGS'] = SHARINGS
app.jinja_env.globals['rooms_for_hostel'] = rooms_for_hostel
app.jinja_env.globals['room_label'] = room_label
app.jinja_env.globals['HALL_ROOM_NUMBER'] = HALL_ROOM_NUMBER
app.jinja_env.globals['HALL_HOSTEL'] = HALL_HOSTEL


def validate_csrf(token):
    return token and session.get('_csrf_token') and secrets.compare_digest(token, session['_csrf_token'])


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash('Please log in to access the admin area.', 'error')
            return redirect(url_for('admin_login', next=request.path))
        return decorated_check(f, *args, **kwargs)
    return decorated


def decorated_check(f, *args, **kwargs):
    return f(*args, **kwargs)


def upload_photo_to_supabase(image_bytes, storage_filename, content_type='image/jpeg'):
    """Uploads JPEG bytes to the Supabase Storage bucket. Returns the public URL, or
    raises ValueError with a user-friendly message on failure."""
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{storage_filename}"
    headers = {
        'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}',
        'apikey': SUPABASE_SERVICE_ROLE_KEY,
        'Content-Type': content_type,
        'x-upsert': 'true',
    }
    try:
        resp = requests.post(upload_url, headers=headers, data=image_bytes, timeout=20)
    except requests.RequestException:
        raise ValueError('Could not reach photo storage. Please try uploading again.')

    if resp.status_code not in (200, 201):
        raise ValueError('Photo upload failed. Please try a different image.')

    return f"{SUPABASE_PUBLIC_PREFIX}{storage_filename}"


def delete_photo_from_supabase(photo_value):
    """Best-effort delete of a photo from Supabase Storage. Safely ignores empty
    values and legacy local filenames left over from before the Supabase migration."""
    if not photo_value or not photo_value.startswith(SUPABASE_PUBLIC_PREFIX):
        return
    storage_filename = photo_value[len(SUPABASE_PUBLIC_PREFIX):]
    delete_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{storage_filename}"
    headers = {
        'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}',
        'apikey': SUPABASE_SERVICE_ROLE_KEY,
    }
    try:
        requests.delete(delete_url, headers=headers, timeout=20)
    except requests.RequestException:
        pass  # cleanup is best-effort; don't block the user's request on it


def save_photo(file_storage):
    """Resize/compress an uploaded JPG photo and upload it to Supabase Storage.
    Returns the public URL to store in the student's photo_filename column, or
    None if no file was provided."""
    if not file_storage or file_storage.filename == '':
        return None
    filename = secure_filename(file_storage.filename)
    if not allowed_file(filename):
        raise ValueError('Only JPG/JPEG images are allowed.')
    try:
        img = Image.open(file_storage.stream)
        img.verify()
        file_storage.stream.seek(0)
        img = Image.open(file_storage.stream)
    except Exception:
        raise ValueError('The uploaded file is not a valid image.')

    img = img.convert('RGB')
    img.thumbnail((800, 800), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=78, optimize=True)
    image_bytes = buf.getvalue()

    storage_filename = f"{uuid.uuid4().hex}.jpg"
    return upload_photo_to_supabase(image_bytes, storage_filename)


def archive_student(db, student):
    """Saves a full copy of a student into deleted_students BEFORE the row is removed, so the admin
    can restore them later. The photo is kept in storage for the same reason."""
    st = dict(student)
    db.execute('''
        INSERT INTO deleted_students
        (original_id, hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance,
         photo_filename, note, due_date, date_of_join, payment_date, payment_mode, cash_amount, paid_to,
         receipt_filename, created_at, deleted_at, deleted_cycle)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ''', (st['id'], st['hostel'], st['room_number'], st['sharing'], st['name'], st.get('contact') or '',
          st.get('total_rent') or 0, st.get('amount_paid') or 0, st.get('balance') or 0,
          st.get('photo_filename'), st.get('note') or '', st.get('due_date'), st.get('date_of_join'),
          st.get('payment_date'), st.get('payment_mode'), st.get('cash_amount'), st.get('paid_to'),
          st.get('receipt_filename'), st.get('created_at'), now_ist().isoformat(), current_cycle()))


def do_delete_student(db, student):
    """Deleting a student always goes through here: copy to Deleted Students first, then remove."""
    archive_student(db, student)
    db.execute('DELETE FROM students WHERE id=%s', (student['id'],))
    db.commit()


def do_restore_deleted(db, deleted_id):
    """Puts a deleted student back. Returns (ok, message, row)."""
    row = db.execute('SELECT * FROM deleted_students WHERE id=%s', (deleted_id,)).fetchone()
    if not row:
        return False, 'That student is no longer in the deleted list.', None
    row = dict(row)
    ok, msg = check_capacity(db, row['hostel'], row['room_number'], row['sharing'])
    if not ok:
        return False, f"Cannot restore {row['name']}: {msg} Free a bed first, then restore.", row
    # Paid / Balance are worked out again from the student's payments (re-attached below).
    rent = row['total_rent'] or 0
    now_iso = datetime.utcnow().isoformat()
    new = db.execute('''
        INSERT INTO students
        (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance, photo_filename, note,
         due_date, date_of_join, payment_date, payment_mode, cash_amount, paid_to, receipt_filename,
         created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    ''', (row['hostel'], row['room_number'], row['sharing'], row['name'], row['contact'] or '',
          rent, 0, rent, row['photo_filename'], row['note'] or '', row['due_date'], row['date_of_join'],
          None, None, None, None, None,
          row['created_at'] or now_iso, now_iso)).fetchone()
    # Re-attach this student's old payment notifications (they were detached when the student was deleted).
    db.execute('''UPDATE payment_submissions SET student_id=%s
                  WHERE student_id IS NULL AND name=%s AND hostel=%s AND room_number=%s''',
               (new['id'], row['name'], row['hostel'], row['room_number']))
    db.execute('DELETE FROM deleted_students WHERE id=%s', (deleted_id,))
    db.commit()
    sync_rent(db, [new['id']])
    return True, f"{row['name']} was restored to {row['hostel']} • {room_label(row['room_number'])}.", row


def _log_payment_delete(db, student_id, name, hostel, room_number, amount, detail):
    db.execute('''INSERT INTO payment_delete_log (student_id, name, hostel, room_number, amount, detail, deleted_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)''',
               (student_id, name, hostel, room_number, amount, detail, now_ist().isoformat()))


def do_clear_student_payment(db, student_id):
    """Deletes the payments of a student's CURRENT rent month (what the app shows as Paid): Paid -> 0 and the
    full rent is the balance again. Returns (ok, name, amount_removed)."""
    st = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not st:
        return False, None, 0
    _st, state = all_rent_states(db, [student_id])[0]
    ids = [p['id'] for p in state['counted']]
    amount = state['paid']
    _log_payment_delete(db, st['id'], st['name'], st['hostel'], st['room_number'], amount,
                        f"Payment deleted (mode: {st.get('payment_mode') or '-'}, paid to: {st.get('paid_to') or '-'})")
    if ids:
        db.execute('DELETE FROM payment_submissions WHERE id IN (' + ','.join(['%s'] * len(ids)) + ')', ids)
    db.execute('DELETE FROM month_payment_dismissals WHERE student_id=%s', (student_id,))
    db.commit()
    sync_rent(db, [student_id])
    return True, st['name'], amount


def reset_counts(db):
    """Numbers for the assistant's Delete All Data confirmation."""
    p = delete_all_preview(db)
    return {'students': len(p['full']) + len(p['carry']), 'full': len(p['full']), 'carry': len(p['carry']),
            'carry_total': p['carry_total'], 'records': p['payments'], 'images': p['images']}


def do_reset_all_payments(db):
    """Delete All Data (used by the assistant). Same as the Delete All Data page."""
    s = do_delete_all_data(db, session.get('admin_username') or ADMIN_USERNAME)
    return {'students': s['full'] + s['carried'], 'full': s['full'], 'carry': s['carried'],
            'carry_total': s['carry_total'], 'records': s['payments']}


def delete_photo(photo_value):
    delete_photo_from_supabase(photo_value)


def save_qr(file_storage, key_prefix):
    """Validate a hostel payment QR image (JPG/PNG), store it as a PNG in Supabase Storage and
    return its public URL (None if no file was provided). Stored losslessly so it stays scannable."""
    if not file_storage or file_storage.filename == '':
        return None
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        raise ValueError('QR code must be a JPG or PNG image.')
    try:
        img = Image.open(file_storage.stream)
        img.verify()
        file_storage.stream.seek(0)
        img = Image.open(file_storage.stream)
        img.load()
    except Exception:
        raise ValueError('The uploaded QR code is not a valid image.')
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert('RGB')
    img.thumbnail((1200, 1200), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return upload_photo_to_supabase(buf.getvalue(), f"{key_prefix}_{uuid.uuid4().hex}.png", 'image/png')


def photo_url(photo_value):
    """Resolves a student's stored photo_filename value to a displayable URL.
    New records store a full Supabase Storage URL. Any older, pre-migration
    local filename is routed through the legacy /static/uploads endpoint (which
    will 404 gracefully into the onerror placeholder in the templates)."""
    if not photo_value:
        return None
    if photo_value.startswith('http://') or photo_value.startswith('https://'):
        return photo_value
    return url_for('uploaded_file', filename=photo_value)


PHOTO_PLACEHOLDER = 'data:image/svg+xml;base64,' + base64.b64encode(b'''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" fill="#E2E8F0"/>
<circle cx="50" cy="38" r="18" fill="#94A3B8"/>
<path d="M18 88c0-17.7 14.3-32 32-32s32 14.3 32 32" fill="#94A3B8"/>
</svg>
'''.strip()).decode('ascii')

app.jinja_env.globals['photo_url'] = photo_url
app.jinja_env.globals['cycle_label'] = lambda c: cycle_label(c)   # '2026-10' -> 'October 2026' (resolved at call time)
app.jinja_env.globals['PAYMENT_MODES'] = PAYMENT_MODES
app.jinja_env.globals['PHOTO_PLACEHOLDER'] = PHOTO_PLACEHOLDER


def room_occupancy(db, hostel, room_number, exclude_id=None):
    query = 'SELECT COUNT(*) as c, MAX(sharing) as s FROM students WHERE hostel=%s AND room_number=%s'
    params = [hostel, room_number]
    if exclude_id:
        query += ' AND id != %s'
        params.append(exclude_id)
    row = db.execute(query, params).fetchone()
    return row['c'] or 0, row['s']


def check_capacity(db, hostel, room_number, sharing, exclude_id=None):
    """Returns (ok, message)."""
    count, existing_sharing = room_occupancy(db, hostel, room_number, exclude_id)
    capacity = existing_sharing if existing_sharing else sharing
    if count >= capacity:
        return False, f'{hostel} Room {room_number} is already full ({count}/{capacity} occupied). Please select another room.'
    return True, None


def get_stats(db):
    row = db.execute('''
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN hostel='Old Hostel' THEN 1 ELSE 0 END) AS old_count,
               SUM(CASE WHEN hostel='New Hostel' THEN 1 ELSE 0 END) AS new_count,
               COALESCE(SUM(total_rent), 0) AS total_rent,
               COALESCE(SUM(amount_paid), 0) AS total_paid,
               COALESCE(SUM(balance), 0) AS total_balance
        FROM students
    ''').fetchone()
    return {
        'total': row['total'] or 0,
        'old_count': row['old_count'] or 0,
        'new_count': row['new_count'] or 0,
        'total_rent': row['total_rent'] or 0,
        'total_paid': row['total_paid'] or 0,
        'total_balance': row['total_balance'] or 0,
    }


def get_dashboard_stats(db):
    """Dashboard cards: every student's CURRENT rent month (Paid / Balance kept on the student by sync_rent)."""
    return get_stats(db)


def get_rooms_structure(db, hostel=None):
    """Returns dict: {hostel: {room_number: [students]}} preserving numeric room order."""
    query = 'SELECT * FROM students'
    params = []
    if hostel:
        query += ' WHERE hostel = %s'
        params.append(hostel)
    query += ' ORDER BY LOWER(name)'
    rows = db.execute(query, params).fetchall()

    structure = {}
    hostels_to_build = [hostel] if hostel else HOSTELS
    for h in hostels_to_build:
        structure[h] = {r: [] for r in rooms_for_hostel(h)}
    for row in rows:
        d = dict(row)
        if d['hostel'] in structure and d['room_number'] in structure[d['hostel']]:
            structure[d['hostel']][d['room_number']].append(d)
    return structure


def row_to_student(row):
    return dict(row)


def get_setting(db, key, default=''):
    row = db.execute('SELECT value FROM app_settings WHERE key=%s', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(db, key, value):
    db.execute('''
        INSERT INTO app_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    ''', (key, value))


HOSTEL_SETTING_KEYS = {'Old Hostel': 'old', 'New Hostel': 'new'}


def get_hostel_payment_info(db, hostel):
    """Per-hostel payment details (QR url, payment/PhonePe number, account name) set by the admin."""
    k = HOSTEL_SETTING_KEYS.get(hostel)
    if not k:
        return {'qr_url': '', 'phone': '', 'name': ''}
    return {
        'qr_url': get_setting(db, f'qr_url_{k}', ''),
        'phone': get_setting(db, f'pay_phone_{k}', ''),
        'name': get_setting(db, f'pay_name_{k}', ''),
    }


def student_due_amount(student):
    """The student's actual current due, taken from their own record (never from settings)."""
    return max(round((student.get('total_rent') or 0) - (student.get('amount_paid') or 0), 2), 0)


def student_rent_label(db, student):
    """Which rent a student is paying, by his own due day: 'Rent due 30 Oct 2026' / 'Next rent due 30 Nov 2026'."""
    try:
        _st, s = all_rent_states(db, [student['id']])[0]
    except IndexError:
        return ''
    if s['status'] in ('due', 'old') and not s['no_due_date']:
        return f"Rent due {pretty_date(s['due_on'].isoformat())}"
    if s.get('next_due'):
        return f"Next rent due {pretty_date(s['next_due'].isoformat())}"
    return 'Rent'


def _num(v):
    """Whole numbers show without a trailing .0 in the number inputs."""
    v = v or 0
    return int(v) if float(v) == int(v) else v


def student_prefill_form(student, extra=None):
    """Values for the existing Add Student form when it is opened after RECORD PAYMENT.
    The student's own details come from their record (found via their mobile number) and are locked;
    the payment fields start empty for the student to fill in."""
    form = {
        'hostel': student['hostel'],
        'room_number': student['room_number'],
        'sharing': student['sharing'],
        'name': student['name'],
        'contact': student['contact'],
        'total_rent': _num(student.get('total_rent')),
        'amount_paid': '',
        'date_of_join': student.get('date_of_join') or '',
        'due_date': student.get('due_date') or '',
        'payment_date': '',
        'payment_mode': '',
        'cash_amount': '',
        'paid_to': '',
        'note': '',
    }
    if extra:
        form.update(extra)
    return form


def render_record_payment(student, extra=None):
    """The existing Add Student page, pre-filled with the student's (locked) details."""
    return render_template(
        'record_payment.html',
        form=student_prefill_form(student, extra),
        record_payment=True,
        official_paid=_num(student.get('amount_paid')),
        student_photo=photo_url(student.get('photo_filename')),
    )


def current_payment_student(db):
    """The student identified by mobile number on the payments page (or None)."""
    student_id = session.get('payment_student_id')
    if not student_id:
        return None
    row = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not row:
        session.pop('payment_student_id', None)
        return None
    return dict(row)


# --------------------------------------------------------------------------
# DATES, PAYMENT FIELDS, RECEIPTS
# --------------------------------------------------------------------------

def parse_iso_date(value):
    """'YYYY-MM-DD' -> date, or None if empty/invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def pretty_date(value):
    d = parse_iso_date(value)
    return d.strftime('%d %b %Y') if d else ''


app.jinja_env.filters['pretty_date'] = pretty_date
app.jinja_env.globals['PAYMENT_MODES'] = PAYMENT_MODES
app.jinja_env.globals['today_iso'] = lambda: today_ist().isoformat()


def _form_date(raw, label, errors):
    raw = (raw or '').strip()
    if not raw:
        return None
    d = parse_iso_date(raw)
    if not d:
        errors.append(f'{label} is not a valid date.')
        return None
    return d.isoformat()


def parse_payment_fields(form, amount_paid, errors):
    """Validates the join date / payment detail fields shared by the add + edit forms."""
    date_of_join = _form_date(form.get('date_of_join'), 'Date of join', errors)
    payment_date = _form_date(form.get('payment_date'), 'Payment date', errors)

    mode = (form.get('payment_mode') or '').strip()
    if mode and mode not in PAYMENT_MODES:
        errors.append('Please select a valid payment mode.')
        mode = ''
    mode = mode or None

    paid_to = (form.get('paid_to') or '').strip()[:100] or None

    cash_amount = None
    cash_raw = (form.get('cash_amount') or '').strip()
    if cash_raw:
        try:
            cash_amount = float(cash_raw)
            if cash_amount < 0:
                raise ValueError()
        except ValueError:
            errors.append('Cash amount must be a valid non-negative number.')
            cash_amount = None
    if mode not in ('Cash', 'Online + Cash'):
        cash_amount = None  # a cash amount only makes sense when cash was involved
    if cash_amount is not None and amount_paid is not None and cash_amount > amount_paid:
        errors.append('Cash amount cannot be greater than the amount paid.')

    return {
        'date_of_join': date_of_join,
        'payment_date': payment_date,
        'payment_mode': mode,
        'cash_amount': cash_amount,
        'paid_to': paid_to,
    }


def save_receipt(file_storage):
    """Compress an uploaded payment receipt (JPG/PNG) and upload it to Supabase Storage.
    Returns the public URL, or None if no file was provided."""
    if not file_storage or file_storage.filename == '':
        return None
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        raise ValueError('Receipt must be a JPG or PNG image.')
    try:
        img = Image.open(file_storage.stream)
        img.verify()
        file_storage.stream.seek(0)
        img = Image.open(file_storage.stream)
    except Exception:
        raise ValueError('The uploaded receipt is not a valid image.')

    img = img.convert('RGB')
    img.thumbnail((1400, 1400), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=80, optimize=True)
    return upload_photo_to_supabase(buf.getvalue(), f"receipt_{uuid.uuid4().hex}.jpg")


# --------------------------------------------------------------------------
# RENT CYCLES (JOINED DATE ONLY) + DELETE ALL DATA
#
# THE RULE: rent falls due every month on the DAY OF THE STUDENT'S JOINED DATE.
#   Joined 30 Sep -> due 30 Oct, 30 Nov, 30 Dec ... (Tomorrow list on the 29th, Due List on the 30th).
#   Joined on the 31st -> due on the last day of shorter months (30 Nov, 28/29 Feb).
# On his due day every student goes into the Due List and stays there until that month's rent is paid.
# Money paid BEFORE a due day belongs to the previous month's rent, so it never hides a student on his due
# day (there is no "advance payment"). Paid / Balance shown everywhere in the app = the student's CURRENT
# rent month (from his last due day). There are no calendar months, no month-end question and no income.
#
# DELETE ALL DATA (the admin runs it whenever he wants to free space):
#   * fully paid students     -> their payment details (amount, date, mode, cash, paid to, images, notes)
#                                are removed; only the base details stay.
#   * students with a balance -> the same details are removed AND the balance is added to their Total Rent,
#                                with an "Old balance" tag. They stay in the Due List for that old balance.
#   A copy is kept so the admin can REVERT. PERMANENTLY DELETE removes the copy (and the receipt images).
#   Payments recorded after a Delete All count towards the next due day.
# --------------------------------------------------------------------------

CLEARED_ON_KEY = 'data_cleared_on'   # date of the last Delete All Data (ISO), '' = never


def get_cleared_on(db):
    return parse_iso_date(get_setting(db, CLEARED_ON_KEY, ''))


def _due_on(year, month, day):
    """The due date in a month: the joining day, or the last day of the month if it is shorter."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift_month(d, months, day):
    m = d.month - 1 + months
    return _due_on(d.year + m // 12, m % 12 + 1, day)


def rent_cycle_dates(joined, today):
    """(previous, current, next) due dates for a student who joined on or before today.
    current = the last due date on or before today (the joined date itself is the first one);
    previous is None during the first month."""
    day = joined.day
    cur = _due_on(today.year, today.month, day)
    if cur > today:
        cur = _shift_month(today.replace(day=1), -1, day)
    prv = _shift_month(cur.replace(day=1), -1, day)
    nxt = _shift_month(cur.replace(day=1), 1, day)
    return (prv if prv >= joined else None), cur, nxt


def _pay_date(row):
    return (parse_iso_date(row.get('payment_date'))
            or parse_iso_date((row.get('confirmed_at') or '')[:10])
            or parse_iso_date((row.get('submitted_at') or '')[:10])
            or today_ist())


def confirmed_payments(db, student_ids=None):
    """{student_id: [payment, ...]} of every CONFIRMED payment still stored, oldest first. Each payment has an
    'eff' date: its payment date, but never earlier than the last Delete All (anything recorded after a
    Delete All counts from that day)."""
    cleared = get_cleared_on(db)
    q = "SELECT * FROM payment_submissions WHERE status='CONFIRMED' AND student_id IS NOT NULL"
    params = []
    if student_ids:
        q += ' AND student_id IN (' + ','.join(['%s'] * len(student_ids)) + ')'
        params = list(student_ids)
    out = {}
    for r in db.execute(q + ' ORDER BY id', params).fetchall():
        p = dict(r)
        d_ = _pay_date(p)
        p['date'] = d_
        p['eff'] = max(d_, cleared) if cleared else d_
        out.setdefault(p['student_id'], []).append(p)
    for lst in out.values():
        lst.sort(key=lambda p: (p['eff'], p['id']))
    return out


# ONE-TIME OCTOBER 2026 FIX: rent months that started between 1 and 29 Oct 2026 count every payment made in
# October (the app had no joining-date rule before). The STRICT joining-date rule ("money paid before the due day
# is last month's rent") applies to every due date from 30 Oct 2026 (today) onwards.
HANDOVER_START = date(2026, 10, 1)
STRICT_RULE_FROM = date(2026, 10, 30)


def _in_handover(d):
    """True for a rent month that started 1-29 Oct 2026 (counted the calendar way)."""
    return d is not None and HANDOVER_START <= d < STRICT_RULE_FROM


def rent_state(student, pays, today, cleared):
    """Everything about one student's rent today. Keys:
    paid / balance  -> for the current rent month (what the app shows as Paid / Balance)
    window_start    -> payments on or after this date are counted in 'paid'
    status          -> 'due' (in the Due List), 'old' (only the old balance is due), 'tomorrow', or None
    due_on, due_amount, days_overdue / days_until, next_due, no_due_date, counted (the counted payments)"""
    rent = float(student.get('total_rent') or 0)
    joined = parse_iso_date(student.get('date_of_join'))
    tomorrow = today + timedelta(days=1)
    s = {'status': None, 'no_due_date': False, 'next_due': None, 'cycle_closed': False}

    def take(start):
        counted = [p for p in pays if p['eff'] >= start and p['eff'] <= today] if start else \
                  [p for p in pays if p['eff'] <= today]
        return counted, round(sum(float(p['amount'] or 0) for p in counted), 2)

    if not joined:
        # No joined date: no due day can be worked out, so any balance is shown as due (with a warning).
        start = cleared or LAUNCH_DATE
        counted, paid = take(start)
        owed = max(round(rent - paid, 2), 0)
        settled = parse_iso_date(student.get('settled_on'))
        if settled and (today - settled).days < 30:
            owed = 0.0
            s['settled'] = True
        s.update(no_due_date=True, window_start=start, counted=counted, paid=paid, balance=owed)
        if owed > 0.005:
            s.update(status='due', due_on=today, due_amount=owed, days_overdue=0)
        return s

    if joined > today:
        # Joins later: nothing is due yet; any money already taken counts for the first month.
        counted, paid = take(None)
        s.update(window_start=joined, counted=counted, paid=paid, balance=max(round(rent - paid, 2), 0),
                 next_due=joined)
        return s

    prv, cur, nxt = rent_cycle_dates(joined, today)
    s['next_due'] = nxt
    s['current_due'] = cur
    if cleared and cleared >= cur:
        # Delete All Data ran during this rent month: its balance was moved into Total Rent (Old balance).
        # Money paid since then first clears that old balance; the next due day asks for the full Total Rent.
        counted, paid = take(cleared)
        carry = float(student.get('last_carry') or 0)
        owed_old = max(round(carry - paid, 2), 0)
        s.update(cycle_closed=True, window_start=cleared, counted=counted, paid=paid,
                 balance=max(round(rent - paid, 2), 0), owed_now=owed_old)
        if owed_old > 0.005:
            s.update(status='old', due_on=cur, due_amount=owed_old, days_overdue=(today - cur).days)
        elif nxt == tomorrow and rent - paid > 0.005:
            s.update(status='tomorrow', due_on=nxt, due_amount=round(rent - paid, 2), days_until=1)
        return s

    start = cur
    if cleared and cleared < cur and (prv is None or cleared >= prv):
        start = cleared          # paid after the last Delete All -> counts for this rent month
    if prv is None:
        start = None             # first rent month: money taken before joining (booking advance) counts too
    elif _in_handover(cur):
        start = max(HANDOVER_START, cleared) if cleared else HANDOVER_START   # October: whole calendar month counts
    if parse_iso_date(student.get('settled_on')) == cur:
        # The admin pressed "Already paid" for this rent month: nothing is due until the next due day.
        # Money paid since the previous due day is shown as this month's payment.
        counted, paid = take(min(start, prv) if prv else start)
        s.update(window_start=(min(start, prv) if prv else start) or joined, counted=counted, paid=paid, balance=0.0,
                 owed_now=0.0, settled=True)
        if nxt == tomorrow:
            s.update(status='tomorrow', due_on=nxt, due_amount=rent, days_until=1)
        return s
    counted, paid = take(start)
    owed = max(round(rent - paid, 2), 0)
    s.update(window_start=start or joined, counted=counted, paid=paid, balance=owed, owed_now=owed)
    if owed > 0.005:
        s.update(status='due', due_on=cur, due_amount=owed, days_overdue=(today - cur).days)
    elif nxt == tomorrow:
        # Paid for this month; tomorrow is the next due day (anything paid before it is this month's rent).
        if _in_handover(nxt):
            # October due date: money already paid in October covers it (handover month).
            nxt_start = max(HANDOVER_START, cleared) if cleared else HANDOVER_START
            left = round(rent - sum(float(p['amount'] or 0) for p in pays if p['eff'] >= nxt_start and p['eff'] <= today), 2)
            if left > 0.005:
                s.update(status='tomorrow', due_on=nxt, due_amount=left, days_until=1)
        else:
            s.update(status='tomorrow', due_on=nxt, due_amount=rent, days_until=1)
    return s


def all_rent_states(db, student_ids=None):
    """[(student dict, rent_state)] for every (or the given) student."""
    today = today_ist()
    cleared = get_cleared_on(db)
    pays = confirmed_payments(db, student_ids)
    q = 'SELECT * FROM students'
    params = []
    if student_ids:
        q += ' WHERE id IN (' + ','.join(['%s'] * len(student_ids)) + ')'
        params = list(student_ids)
    out = []
    for r in db.execute(q, params).fetchall():
        st = dict(r)
        out.append((st, rent_state(st, pays.get(st['id'], []), today, cleared)))
    return out


def _summary_fields(counted):
    """Payment date / mode / cash / paid to / receipt shown on the student for the current rent month."""
    if not counted:
        return {'payment_date': None, 'payment_mode': None, 'cash_amount': None, 'paid_to': None,
                'receipt_filename': None}
    last = counted[-1]
    modes = {p.get('payment_mode') or 'Online' for p in counted}
    mode = modes.pop() if len(modes) == 1 else 'Online + Cash'
    cash = 0.0
    for p in counted:
        pm = p.get('payment_mode') or 'Online'
        if pm == 'Cash':
            cash += float(p['amount'] or 0)
        elif pm == 'Online + Cash':
            cash += float(p.get('cash_amount') or 0)
    receipt = next((p['proof_filename'] for p in reversed(counted) if p.get('proof_filename')), None)
    paid_to = next((p['paid_to'] for p in reversed(counted) if p.get('paid_to')), None)
    return {'payment_date': last['date'].isoformat(), 'payment_mode': mode,
            'cash_amount': round(cash, 2) if cash else None, 'paid_to': paid_to, 'receipt_filename': receipt}


def sync_rent(db, student_ids=None, commit=True):
    """Writes each student's CURRENT rent month (Paid, Balance and the last payment's details) onto the student
    row, so the dashboard, PDF, Excel and every page show the same numbers. Only changed rows are written."""
    cols = ('amount_paid', 'balance', 'payment_date', 'payment_mode', 'cash_amount', 'paid_to', 'receipt_filename')
    rows = []
    now_iso = datetime.utcnow().isoformat()
    for st, s in all_rent_states(db, student_ids):
        f = _summary_fields(s['counted'])
        f['amount_paid'] = s['paid']
        f['balance'] = s['balance']
        changed = [k for k, v in f.items()
                   if (st.get(k) or None) != (v or None)
                   and not (isinstance(v, float) and abs(float(st.get(k) or 0) - v) < 0.005)]
        if changed:
            rows.append([f[c] for c in cols] + [now_iso, st['id']])
    if rows:
        # One round trip for all changed students (SPEED).
        cur = db.conn.cursor()
        psycopg2.extras.execute_batch(cur, 'UPDATE students SET ' + ', '.join(f'{c}=%s' for c in cols)
                                      + ', updated_at=%s WHERE id=%s', rows, page_size=200)
    if commit:
        db.commit()


def get_due_and_upcoming(db):
    """(due_list, tomorrow_list) from the joined date only (see THE RULE above)."""
    today = today_ist()
    due_list, up_list = [], []
    for st, s in all_rent_states(db):
        if s['status'] in ('due', 'old'):
            st.update(no_due_date=s['no_due_date'], old_only=(s['status'] == 'old'),
                      due_day=s['due_on'].day, due_on=s['due_on'].isoformat(), days_overdue=s['days_overdue'],
                      due_amount=s['due_amount'], cycles_due=1,
                      next_due=s['next_due'].isoformat() if s['next_due'] else None)
            due_list.append(st)
        elif s['status'] == 'tomorrow':
            st.update(no_due_date=False, due_day=s['due_on'].day, due_on=s['due_on'].isoformat(),
                      days_until=(s['due_on'] - today).days, due_amount=s['due_amount'])
            up_list.append(st)
    due_list.sort(key=lambda x: (-x['days_overdue'], (x['name'] or '').lower()))
    up_list.sort(key=lambda x: (x['days_until'], (x['name'] or '').lower()))
    return due_list, up_list


def get_due_students(db):
    return get_due_and_upcoming(db)[0]


def get_upcoming_due_students(db, days=1):
    return get_due_and_upcoming(db)[1]


def current_cycle():
    """'YYYY-MM' of today. Only used to label when a student was deleted."""
    return today_ist().strftime('%Y-%m')


def cycle_label(cycle):
    """'2026-10' -> 'October 2026'."""
    try:
        return datetime.strptime(cycle, '%Y-%m').strftime('%B %Y')
    except (TypeError, ValueError):
        return cycle or ''


# ---- one-time move of payments typed straight onto a student into the payments table ----

def migrate_record_payments():
    """Before this version a payment could live only on the student row (amount paid typed on the student).
    Every payment must now be one row in payment_submissions (so the rent month it belongs to is known).
    Runs once: whatever part of a student's Paid is not covered by this month's confirmed payments becomes one
    confirmed payment with the student's own payment date / mode / paid to / receipt. Nothing is deleted."""
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT value FROM app_settings WHERE key = 'payments_v2_migrated'")
        if cur.fetchone():
            return
        cur.execute("SELECT value FROM app_settings WHERE key = 'billing_cycle'")
        row = cur.fetchone()
        try:
            period_start = datetime.strptime(row['value'] + '-01', '%Y-%m-%d').date()
        except (TypeError, ValueError, KeyError):
            period_start = today_ist().replace(day=1)
        now_iso = datetime.utcnow().isoformat()
        cur.execute('SELECT * FROM students WHERE amount_paid > 0')
        for st in cur.fetchall():
            cur.execute("""SELECT COALESCE(SUM(amount),0) AS t FROM payment_submissions WHERE student_id=%s
                           AND status='CONFIRMED' AND COALESCE(confirmed_at, submitted_at) >= %s""",
                        (st['id'], period_start.isoformat()))
            covered = float(cur.fetchone()['t'] or 0)
            rest = round(float(st['amount_paid'] or 0) - covered, 2)
            if rest <= 0.005:
                continue
            pdate = parse_iso_date(st.get('payment_date')) or period_start
            mode = st.get('payment_mode') or 'Online'
            cash = None
            if mode == 'Cash':
                cash = rest
            elif mode == 'Online + Cash':
                cash = min(float(st.get('cash_amount') or 0), rest) or None
                if cash is not None and cash >= rest:
                    mode, cash = 'Cash', rest
            cur.execute('''
                INSERT INTO payment_submissions
                (student_id, hostel, room_number, name, contact, amount, for_month, proof_filename, note, status,
                 submitted_at, confirmed_at, confirmed_by, payment_mode, cash_amount, paid_to, payment_date, dismissed)
                VALUES (%s,%s,%s,%s,%s,%s,'',%s,'Recorded on the student by admin','CONFIRMED',%s,%s,'admin',%s,%s,%s,%s,1)
            ''', (st['id'], st['hostel'], st['room_number'], st['name'], st['contact'] or '', rest,
                  st.get('receipt_filename'), pdate.isoformat() + 'T00:00:00', now_iso, mode, cash,
                  st.get('paid_to'), pdate.isoformat()))
        cur.execute("INSERT INTO app_settings (key, value) VALUES ('payments_v2_migrated', %s) ON CONFLICT (key) DO NOTHING",
                    (now_iso,))
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception('Moving student payments into the payments table failed')
        raise
    finally:
        put_conn(conn)


# ---- DELETE ALL DATA (with Revert and Permanently Delete) ----

def delete_all_preview(db):
    """What Delete All Data would do right now."""
    today = today_ist()
    full, carry = [], []
    for st, s in all_rent_states(db):
        if parse_iso_date(st.get('date_of_join')) and parse_iso_date(st.get('date_of_join')) > today:
            continue   # joins later: nothing to close yet
        owed = s.get('owed_now', s['balance'])
        item = {'id': st['id'], 'name': st['name'], 'hostel': st['hostel'], 'room': room_label(st['room_number']),
                'rent': float(st['total_rent'] or 0), 'paid': s['paid'], 'owed': owed,
                'new_rent': float(st['total_rent'] or 0) + (0 if s['cycle_closed'] else owed) - (s['paid'] if s['cycle_closed'] else 0)}
        (carry if owed > 0.005 else full).append(item)
    pays = db.execute("SELECT COUNT(*) AS n, COUNT(proof_filename) AS img FROM payment_submissions WHERE status='CONFIRMED'").fetchone()
    hist = db.execute('SELECT COUNT(*) AS n FROM payment_history').fetchone()
    return {'full': full, 'carry': carry, 'carry_total': round(sum(c['owed'] for c in carry), 2),
            'payments': pays['n'] or 0, 'images': pays['img'] or 0, 'history': hist['n'] or 0,
            'pending': db.execute("SELECT COUNT(*) AS n FROM payment_submissions WHERE status='PENDING'").fetchone()['n'] or 0}


STUDENT_SNAPSHOT_COLS = ('total_rent', 'amount_paid', 'balance', 'payment_date', 'payment_mode', 'cash_amount',
                         'paid_to', 'receipt_filename', 'note', 'old_balance', 'last_carry')


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def do_delete_all_data(db, admin_name):
    """Runs Delete All Data (see the rule at the top of this section). Returns the summary dict."""
    today = today_ist()
    states = all_rent_states(db)
    snapshot = {'students': [], 'payments': [], 'history': [], 'dismissals': [],
                'prev_cleared': get_setting(db, CLEARED_ON_KEY, '')}
    summary = {'full': 0, 'carried': 0, 'carry_total': 0.0, 'payments': 0, 'images': 0, 'history': 0}
    now_iso = datetime.utcnow().isoformat()
    for st, s in states:
        joined = parse_iso_date(st.get('date_of_join'))
        if joined and joined > today:
            continue
        before = {k: st.get(k) for k in STUDENT_SNAPSHOT_COLS}
        rent = float(st['total_rent'] or 0)
        old_tag = float(st.get('old_balance') or 0)
        owed = round(s.get('owed_now', s['balance']), 2)
        if s['cycle_closed']:
            # Already closed by an earlier Delete All this rent month: what was paid since then is taken off
            # the Total Rent (it paid the old balance), and whatever is still owed stays as the old balance.
            paid = s['paid']
            new_rent = round(rent - paid, 2)
            new_tag = max(round(old_tag - paid, 2), 0)
            new_carry = owed
        else:
            new_rent = round(rent + owed, 2)
            new_tag = round(old_tag + owed, 2)
            new_carry = owed
        if owed > 0.005:
            summary['carried'] += 1
            summary['carry_total'] += owed
        else:
            summary['full'] += 1
        db.execute('''UPDATE students SET total_rent=%s, old_balance=%s, last_carry=%s, amount_paid=0, balance=%s,
                      payment_date=NULL, payment_mode=NULL, cash_amount=NULL, paid_to=NULL, receipt_filename=NULL,
                      note='', updated_at=%s WHERE id=%s''',
                   (new_rent, new_tag, new_carry, new_rent, now_iso, st['id']))
        snapshot['students'].append({'id': st['id'], 'name': st['name'], 'before': before,
                                     'after_rent': new_rent, 'after_tag': new_tag, 'carried': owed})
    for r in db.execute("SELECT * FROM payment_submissions WHERE status='CONFIRMED'").fetchall():
        snapshot['payments'].append(dict(r))
    for r in db.execute('SELECT * FROM payment_history').fetchall():
        snapshot['history'].append(dict(r))
    for r in db.execute('SELECT * FROM month_payment_dismissals').fetchall():
        snapshot['dismissals'].append(dict(r))
    summary['payments'] = len(snapshot['payments'])
    summary['images'] = len({p['proof_filename'] for p in snapshot['payments'] if p.get('proof_filename')})
    summary['history'] = len(snapshot['history'])
    summary['carry_total'] = round(summary['carry_total'], 2)
    # Students who join later keep their payments (they were not closed above).
    later = [st['id'] for st, _ in states if parse_iso_date(st.get('date_of_join')) and parse_iso_date(st.get('date_of_join')) > today]
    if later:
        keep = ','.join(['%s'] * len(later))
        snapshot['payments'] = [p for p in snapshot['payments'] if p['student_id'] not in later]
        summary['payments'] = len(snapshot['payments'])
        db.execute(f"DELETE FROM payment_submissions WHERE status='CONFIRMED' AND (student_id IS NULL OR student_id NOT IN ({keep}))", later)
    else:
        db.execute("DELETE FROM payment_submissions WHERE status='CONFIRMED'")
    db.execute('DELETE FROM payment_history')
    db.execute('DELETE FROM month_payment_dismissals')
    db.execute('DELETE FROM unconfirmed_dismissals')
    set_setting(db, CLEARED_ON_KEY, today.isoformat())
    db.execute('''INSERT INTO data_cleanups (created_at, created_by, cleared_on, status, summary, snapshot)
                  VALUES (%s, %s, %s, 'DELETED', %s, %s)''',
               (now_ist().isoformat(), admin_name, today.isoformat(), json.dumps(summary),
                json.dumps(snapshot, default=_json_default)))
    db.commit()
    sync_rent(db)
    return summary


def get_cleanups(db):
    out = []
    rows = db.execute('SELECT id, created_at, created_by, cleared_on, status, summary FROM data_cleanups ORDER BY id DESC').fetchall()
    latest_deleted = next((r['id'] for r in rows if r['status'] == 'DELETED'), None)
    for r in rows:
        d = dict(r)
        try:
            d['summary'] = json.loads(d['summary'] or '{}')
        except ValueError:
            d['summary'] = {}
        d['can_revert'] = d['status'] == 'DELETED' and d['id'] == latest_deleted
        out.append(d)
    return out


def do_revert_cleanup(db, cleanup_id):
    """Puts everything one Delete All Data removed back. Only the latest one can be reverted.
    Returns (ok, message)."""
    row = db.execute('SELECT * FROM data_cleanups WHERE id=%s', (cleanup_id,)).fetchone()
    if not row or row['status'] != 'DELETED':
        return False, 'That delete can no longer be reverted.'
    latest = db.execute("SELECT MAX(id) AS m FROM data_cleanups WHERE status='DELETED'").fetchone()['m']
    if latest != cleanup_id:
        return False, 'Revert the most recent Delete All Data first.'
    snap = json.loads(row['snapshot'] or '{}')
    existing = {r['id'] for r in db.execute('SELECT id FROM students').fetchall()}
    now_iso = datetime.utcnow().isoformat()
    for s in snap.get('students', []):
        if s['id'] not in existing:
            continue
        b = s['before']
        cur_row = db.execute('SELECT total_rent, note FROM students WHERE id=%s', (s['id'],)).fetchone()
        # Undo only what the delete changed, so a rent edited afterwards is kept.
        rent = round(float(cur_row['total_rent'] or 0) - (float(s['after_rent']) - float(b.get('total_rent') or 0)), 2)
        note = cur_row['note'] or b.get('note') or ''
        db.execute('''UPDATE students SET total_rent=%s, old_balance=%s, last_carry=%s, note=%s, updated_at=%s WHERE id=%s''',
                   (rent, b.get('old_balance') or 0, b.get('last_carry') or 0, note, now_iso, s['id']))
    pay_cols = None
    for p in snap.get('payments', []):
        p = dict(p)
        if p.get('student_id') not in existing:
            p['student_id'] = None
        pay_cols = pay_cols or [c for c in p.keys()]
        cols = [c for c in pay_cols if c in p]
        db.execute(f"INSERT INTO payment_submissions ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
                   'ON CONFLICT (id) DO NOTHING', [p[c] for c in cols])
    for h in snap.get('history', []):
        cols = list(h.keys())
        db.execute(f"INSERT INTO payment_history ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
                   'ON CONFLICT (id) DO NOTHING', [h[c] for c in cols])
    for m in snap.get('dismissals', []):
        cols = list(m.keys())
        db.execute(f"INSERT INTO month_payment_dismissals ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
                   'ON CONFLICT DO NOTHING', [m[c] for c in cols])
    # Keep the id counters ahead of the restored rows.
    db.execute("SELECT setval(pg_get_serial_sequence('payment_submissions','id'), GREATEST((SELECT COALESCE(MAX(id),0) FROM payment_submissions), 1))")
    db.execute("SELECT setval(pg_get_serial_sequence('payment_history','id'), GREATEST((SELECT COALESCE(MAX(id),0) FROM payment_history), 1))")
    set_setting(db, CLEARED_ON_KEY, snap.get('prev_cleared') or '')
    db.execute("UPDATE data_cleanups SET status='REVERTED', snapshot='' WHERE id=%s", (cleanup_id,))
    db.commit()
    sync_rent(db)
    return True, 'Everything from that Delete All Data was put back.'


def do_purge_cleanup(db, cleanup_id):
    """Permanently deletes the saved copy of one Delete All Data (and its receipt images). Returns (ok, message)."""
    row = db.execute('SELECT * FROM data_cleanups WHERE id=%s', (cleanup_id,)).fetchone()
    if not row or row['status'] != 'DELETED':
        return False, 'Nothing left to delete for that entry.'
    snap = json.loads(row['snapshot'] or '{}')
    files = {p.get('proof_filename') for p in snap.get('payments', []) if p.get('proof_filename')}
    files |= {h.get('receipt_filename') for h in snap.get('history', []) if h.get('receipt_filename')}
    files |= {s['before'].get('receipt_filename') for s in snap.get('students', []) if s['before'].get('receipt_filename')}
    # An image still used by a current payment / student / deleted student is kept.
    in_use = {r['f'] for r in db.execute('''SELECT proof_filename AS f FROM payment_submissions WHERE proof_filename IS NOT NULL
                                            UNION SELECT receipt_filename FROM students WHERE receipt_filename IS NOT NULL
                                            UNION SELECT receipt_filename FROM deleted_students WHERE receipt_filename IS NOT NULL''').fetchall()}
    db.execute("UPDATE data_cleanups SET status='PURGED', snapshot='' WHERE id=%s", (cleanup_id,))
    db.commit()
    for f in files - in_use:
        try:
            delete_photo(f)
        except Exception:
            pass
    return True, 'The deleted data was removed permanently from the database.'


# --------------------------------------------------------------------------
# INITIALIZE DATABASE ON STARTUP
# --------------------------------------------------------------------------

OCT_DELETED_SEED = [
    # hostel, room, sharing, name, contact, rent, paid, join date, pay date, mode, paid to, deleted on
    ('Old Hostel', 3, 6, 'B.Ram charan', '9121534983', 6000, 0, '2026-03-10', None, None, None, '2026-10-16'),
    ('Old Hostel', 4, 4, 'Ch Srikanth', '8309184011', 3000, 3000, '2026-04-24', '2026-10-07', 'Online', 'Ravi Teja', '2026-10-16'),
    ('Old Hostel', 6, 4, 'M Ajay', '7036692116', 3000, 3000, '2026-06-22', '2026-10-08', 'Online', 'Narayana', '2026-10-16'),
    ('New Hostel', 4, 5, 'B Raju kumar', '8639759087', 2000, 2000, '2026-06-23', None, None, None, '2026-10-16'),
    ('New Hostel', 4, 5, 'Vinodh', '9948772275', 2600, 2900, None, None, None, None, '2026-10-20'),
    ('New Hostel', 11, 9, 'E. Vikram', '8555808628', 5500, 0, '2026-09-08', None, None, None, '2026-10-16'),
]


def seed_october_deleted_students():
    """One time only: puts the students deleted in October 2026 into the Deleted Students list so they can be
    restored. Skips anyone who is already a current student or already listed."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM app_settings WHERE key = 'seed_deleted_oct2026'")
        if cur.fetchone():
            return
        now_iso = datetime.utcnow().isoformat()
        for (hostel, room, sharing, name, contact, rent, paid, joined, pdate, mode, paid_to, deleted_on) in OCT_DELETED_SEED:
            cur.execute('SELECT 1 FROM students WHERE LOWER(name)=LOWER(%s) AND hostel=%s AND room_number=%s', (name, hostel, room))
            if cur.fetchone():
                continue
            cur.execute('SELECT 1 FROM deleted_students WHERE LOWER(name)=LOWER(%s) AND hostel=%s AND room_number=%s', (name, hostel, room))
            if cur.fetchone():
                continue
            cur.execute('''
                INSERT INTO deleted_students
                (original_id, hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance, note,
                 date_of_join, payment_date, payment_mode, paid_to, created_at, deleted_at, deleted_cycle)
                VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,%s,'',%s,%s,%s,%s,%s,%s,'2026-10')
            ''', (hostel, room, sharing, name, contact, rent, paid, max(rent - paid, 0), joined, pdate, mode, paid_to,
                  now_iso, deleted_on + 'T00:00:00'))
        cur.execute("INSERT INTO app_settings (key, value) VALUES ('seed_deleted_oct2026', 'done') ON CONFLICT (key) DO NOTHING")
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception('Could not add the October deleted students')
    finally:
        put_conn(conn)


# Students the admin confirmed on 29 Oct 2026 as FULLY PAID for their current rent month.
PAID_CONFIRMED_OCT2026 = ['K Madhav', 'K Manohar', 'Dinesh sai teja', 'B Raju kumar', 'Mano srikarana', 'Ramakrishna A',
                          'Soujith kumar', 'Srikanth L.', 'T Hanumanth', 'Vishnu', 'Manthani sanjay']


def _name_key(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def settle_confirmed_students():
    """One time: marks the students above as fully paid for the rent month that was running on 29 Oct 2026, so they
    are not in the Due List until their next due day. The result is written as an admin note."""
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT 1 FROM app_settings WHERE key = 'settled_paid_oct2026'")
        if cur.fetchone():
            return
        cur.execute('SELECT id, name, date_of_join FROM students')
        by_key = {}
        for r in cur.fetchall():
            by_key.setdefault(_name_key(r['name']), []).append(r)
        on = date(2026, 10, 29)
        done, missing = [], []
        for name in PAID_CONFIRMED_OCT2026:
            rows = by_key.get(_name_key(name), [])
            if not rows:
                missing.append(name)
                continue
            for r in rows:
                joined = parse_iso_date(r['date_of_join'])
                if joined and joined <= on:
                    mark = rent_cycle_dates(joined, on)[1].isoformat()
                else:
                    mark = on.isoformat()
                cur.execute('UPDATE students SET settled_on=%s WHERE id=%s', (mark, r['id']))
                done.append(r['name'])
        now_iso = now_ist().isoformat()
        body = 'Marked as fully paid for their current rent month (not shown in the Due List until their next due day):\n' \
               + ', '.join(done or ['none'])
        if missing:
            body += '\n\nNOT FOUND in the app (check the spelling and press "Already paid" on the Due List): ' + ', '.join(missing)
        cur.execute('INSERT INTO admin_notes (title, body, pinned, created_at, updated_at) VALUES (%s,%s,1,%s,%s)',
                    ('Fully paid students (29 Oct 2026)', body, now_iso, now_iso))
        cur.execute("INSERT INTO app_settings (key, value) VALUES ('settled_paid_oct2026', %s) ON CONFLICT (key) DO NOTHING", (now_iso,))
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception('Marking the confirmed-paid students failed')
    finally:
        put_conn(conn)


def startup_rent_sync():
    """Every start: recalculate every student's Paid / Balance with the current rules, so the dashboard cards
    (Total Paid, Balance Due) are right straight after an update. One batched query."""
    conn = get_conn()
    try:
        sync_rent(DBWrapper(conn), commit=True)
    except Exception:
        conn.rollback()
        app.logger.exception('Startup rent sync failed')
    finally:
        put_conn(conn)


init_db()
seed_october_deleted_students()
migrate_record_payments()
settle_confirmed_students()
startup_rent_sync()


@app.before_request
def daily_rent_sync():
    """Once a day (first request after midnight IST) every student's Paid / Balance moves to their current
    rent month, so a new due day shows up everywhere without anyone touching the student."""
    if request.endpoint in (None, 'static', 'uploaded_file'):
        return
    try:
        db = get_db()
        today = today_ist().isoformat()
        if get_setting(db, 'rent_synced_on', '') != today:
            sync_rent(db, commit=False)
            set_setting(db, 'rent_synced_on', today)
            db.commit()
    except Exception:
        app.logger.exception('Daily rent sync failed')
        try:
            get_db().conn.rollback()
        except Exception:
            pass


# --------------------------------------------------------------------------
# PUBLIC ROUTES
# --------------------------------------------------------------------------

@app.route('/', methods=['GET', 'POST'], endpoint='index')
@app.route('/add-student', methods=['GET', 'POST'], endpoint='add_student')
def add_student():
    db = get_db()
    is_admin = bool(session.get('admin_logged_in'))

    # "RECORD PAYMENT" opens this same page with the identified student's details pre-filled.
    # It only works for a student identified by mobile number on the payments page.
    if request.method == 'GET' and request.args.get('record_payment') == '1':
        student = current_payment_student(db)
        if not student:
            flash('Please enter your mobile number first.', 'error')
            return redirect(url_for('payments_lookup'))
        return render_record_payment(student)

    # Everyone except the admin sees the welcome page; adding students is admin-only.
    if not is_admin:
        if request.method == 'POST':
            flash('Only the admin can add students. Please log in as admin.', 'error')
            return redirect(url_for('admin_login', next=url_for('add_student')))
        return redirect(url_for('payments_lookup'))

    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for(request.endpoint))

        hostel = request.form.get('hostel', '').strip()
        room_number = request.form.get('room_number', '').strip()
        sharing = request.form.get('sharing', '').strip()
        name = request.form.get('name', '').strip()
        contact = request.form.get('contact', '').strip()
        total_rent = request.form.get('total_rent', '').strip()

        errors = []

        if hostel not in HOSTELS:
            errors.append('Please select a valid hostel.')
        try:
            room_number = int(room_number)
            if room_number not in rooms_for_hostel(hostel if hostel in HOSTELS else None):
                errors.append('Please select a valid room number.')
        except (ValueError, TypeError):
            errors.append('Please select a valid room number.')
            room_number = None
        try:
            sharing = int(sharing)
            if sharing not in SHARINGS:
                errors.append('Please select a valid sharing option.')
        except (ValueError, TypeError):
            errors.append('Please select a valid sharing option.')
            sharing = None
        if not name or len(name) < 2:
            errors.append('Please enter a valid student name.')

        mobile_ok, clean_contact = validate_mobile(contact)
        if not mobile_ok:
            errors.append('Please enter a valid 10-digit Indian mobile number.')

        try:
            total_rent = float(total_rent)
            if total_rent < 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append('Total rent must be a valid non-negative number.')
            total_rent = None
        date_of_join = _form_date(request.form.get('date_of_join'), 'Date of join', errors)

        if not errors and room_number and sharing:
            ok, msg = check_capacity(db, hostel, room_number, sharing)
            if not ok:
                errors.append(msg)

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('add_student.html', form=request.form)

        # Registration only: no payment is recorded here (that happens through Record Payment).
        total_rent = round(total_rent, 2)
        now = datetime.utcnow().isoformat()

        db.execute('''
            INSERT INTO students
            (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance, photo_filename, note, due_date,
             date_of_join, payment_date, payment_mode, cash_amount, paid_to, receipt_filename, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (hostel, room_number, sharing, name, clean_contact, total_rent, 0, total_rent, None, '', None,
              date_of_join, None, None, None, None, None, now, now))
        db.commit()

        flash(f'{name} was saved successfully to {hostel} - Room {room_number}!', 'success')
        return redirect(url_for(request.endpoint, saved=1, hostel=hostel, room=room_number, name=name))

    return render_template('add_student.html', form={})


@app.route('/static/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# --------------------------------------------------------------------------
# ADMIN AUTH
# --------------------------------------------------------------------------

@app.route('/adminlogin', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('admin_login'))

        username = request.form.get('username', '')
        password = request.form.get('password', '')

        if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
            session['admin_logged_in'] = True
            session['admin_username'] = username
            session.permanent = True
            flash('Welcome back, admin!', 'success')
            next_url = request.args.get('next')
            return redirect(next_url if next_url else url_for('admin_dashboard'))
        else:
            flash('Invalid username or password.', 'error')

    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))


# --------------------------------------------------------------------------
# ADMIN DASHBOARD
# --------------------------------------------------------------------------

@app.route('/admin')
@login_required
def admin_dashboard():
    db = get_db()
    stats = get_dashboard_stats(db)
    rooms_structure = get_rooms_structure(db)
    all_students = db.execute('SELECT * FROM students ORDER BY LOWER(name)').fetchall()
    all_students = [row_to_student(r) for r in all_students]

    due_now, due_tomorrow = get_due_and_upcoming(db)
    due_count = len(due_now)
    pending_row = db.execute("SELECT COUNT(*) AS c FROM payment_submissions WHERE status='PENDING'").fetchone()
    now = now_ist()
    greeting = 'Good morning' if now.hour < 12 else ('Good afternoon' if now.hour < 17 else 'Good evening')

    return render_template(
        'admin.html',
        stats=stats,
        rooms_structure=rooms_structure,
        hostels=HOSTELS,
        rooms=ROOMS,
        all_students=all_students,
        due_count=due_count,
        due_today_count=sum(1 for s in due_now if s.get('days_overdue') == 0),
        tomorrow_count=len(due_tomorrow),
        due_amount_total=round(sum(s['due_amount'] for s in due_now), 2),
        pending_payments_count=pending_row['c'] or 0,
        greeting=greeting,
        is_night=(now.hour >= 18 or now.hour < 6),
        today_label=now.strftime('%A, %d %B %Y'),
    )


# --------------------------------------------------------------------------
# ADMIN BOT (chat helper on the admin dashboard)
#   1) Check Vacancy   2) Make Empty (remove a member from a room)   3) Per-day rent
# The old /admin/vacancy page was removed; the bot's "Check Vacancy" replaces it.
# --------------------------------------------------------------------------

RENT_MONTH_DAYS = 30  # only used as a fallback when a room's sharing is unknown

# Daily rent by sharing type: 1, 2 and 3 sharing = Rs.300/day; every other sharing type = Rs.250/day.
DAILY_RENT_PREMIUM = 300
DAILY_RENT_STANDARD = 250
DAILY_RENT_PREMIUM_SHARINGS = (1, 2, 3)


def daily_rent_for_sharing(sharing):
    try:
        sharing = int(sharing)
    except (TypeError, ValueError):
        return None
    return DAILY_RENT_PREMIUM if sharing in DAILY_RENT_PREMIUM_SHARINGS else DAILY_RENT_STANDARD


def admin_api_required(f):
    """Like login_required, but answers JSON (401) instead of redirecting to the login page."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'ok': False, 'error': 'Your session expired. Please log in again.'}), 401
        return f(*args, **kwargs)
    return decorated


def bot_csrf_ok():
    return validate_csrf(request.headers.get('X-CSRF-Token', ''))


def _bot_json_body():
    return request.get_json(silent=True) or {}


def _bot_parse_hostel_room(hostel, room_raw):
    """Returns (hostel, room_number, error)."""
    hostel = (hostel or '').strip()
    if hostel not in HOSTELS:
        return None, None, 'Please choose Old Hostel or New Hostel.'
    raw = str(room_raw or '').strip().lower()
    if raw == 'hall':
        room_number = HALL_ROOM_NUMBER
    elif raw.isdigit():
        room_number = int(raw)
    else:
        return None, None, 'Please type a valid room number.'
    if room_number not in rooms_for_hostel(hostel):
        valid = '1-10' + (' or Hall' if hostel == HALL_HOSTEL else '')
        return None, None, f'{hostel} has no such room. Valid rooms: {valid}.'
    return hostel, room_number, None


def prorated_rent(monthly_rent, days, sharing=None):
    """per-day rent x number of days. The per-day rent comes from the sharing type
    (Rs.300 for 1/2/3 sharing, Rs.250 otherwise). Without a sharing, the old monthly / 30 rule is used."""
    per_day = daily_rent_for_sharing(sharing)
    if per_day is None:
        per_day = (monthly_rent or 0) / RENT_MONTH_DAYS
    return per_day, round(per_day * days, 2)


@app.route('/admin/api/bot/vacancy')
@admin_api_required
def bot_vacancy():
    db = get_db()
    students = db.execute('SELECT hostel, room_number, sharing FROM students').fetchall()
    note_rows = db.execute('SELECT * FROM room_notes').fetchall()
    notes_map = {(n['hostel'], n['room_number']): n['note'] for n in note_rows}

    occupied = {}
    for row in students:
        key = (row['hostel'], row['room_number'])
        info = occupied.setdefault(key, {'sharing': row['sharing'], 'count': 0})
        info['count'] += 1

    hostels = []
    total = {'capacity': 0, 'occupied': 0, 'vacant': 0}
    for hostel in HOSTELS:
        h = {'name': hostel, 'capacity': 0, 'occupied': 0, 'vacant': 0, 'rooms': []}
        for room_number in rooms_for_hostel(hostel):
            key = (hostel, room_number)
            note = notes_map.get(key, '')
            if key in occupied:
                sharing = occupied[key]['sharing']
                count = occupied[key]['count']
                vacant = max(sharing - count, 0)
                h['capacity'] += sharing
                h['occupied'] += count
                h['vacant'] += vacant
                h['rooms'].append({'room_number': room_number, 'label': room_label(room_number),
                                   'sharing': sharing, 'occupied': count, 'vacant': vacant,
                                   'assigned': True, 'note': note})
            else:
                h['rooms'].append({'room_number': room_number, 'label': room_label(room_number),
                                   'sharing': None, 'occupied': 0, 'vacant': None,
                                   'assigned': False, 'note': note})
        for k in total:
            total[k] += h[k]
        hostels.append(h)
    return jsonify({'ok': True, 'hostels': hostels, 'total': total})


@app.route('/admin/api/bot/room')
@admin_api_required
def bot_room():
    hostel, room_number, err = _bot_parse_hostel_room(request.args.get('hostel'), request.args.get('room'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    db = get_db()
    rows = db.execute(
        'SELECT id, name, contact, sharing, total_rent, amount_paid, balance FROM students '
        'WHERE hostel=%s AND room_number=%s ORDER BY LOWER(name)', (hostel, room_number)).fetchall()
    members = [dict(r) for r in rows]
    sharing = members[0]['sharing'] if members else None
    return jsonify({
        'ok': True, 'hostel': hostel, 'room_number': room_number, 'label': room_label(room_number),
        'sharing': sharing, 'occupied': len(members),
        'vacant': max(sharing - len(members), 0) if sharing else None,
        'members': members,
    })


@app.route('/admin/api/bot/make-empty', methods=['POST'])
@admin_api_required
def bot_make_empty():
    """Removes one member from their room (frees the bed). Same effect as the dashboard Delete button."""
    if not bot_csrf_ok():
        return jsonify({'ok': False, 'error': 'Security check failed. Please refresh the page.'}), 400
    try:
        student_id = int(_bot_json_body().get('student_id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Invalid member.'}), 400

    db = get_db()
    student = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not student:
        return jsonify({'ok': False, 'error': 'That member was not found (maybe already removed).'}), 404
    student = dict(student)
    archive_student(db, student)   # keep a copy so it can be restored from "Deleted Students"
    db.execute('DELETE FROM students WHERE id=%s', (student_id,))
    db.commit()

    count, _ = room_occupancy(db, student['hostel'], student['room_number'])
    vacant = max(student['sharing'] - count, 0)
    return jsonify({'ok': True, 'name': student['name'], 'hostel': student['hostel'],
                    'label': room_label(student['room_number']), 'occupied': count,
                    'sharing': student['sharing'], 'vacant': vacant})


def _bot_days(value):
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if 1 <= days <= 31 else None


@app.route('/admin/api/bot/rent/calc', methods=['POST'])
@admin_api_required
def bot_rent_calc():
    if not bot_csrf_ok():
        return jsonify({'ok': False, 'error': 'Security check failed. Please refresh the page.'}), 400
    body = _bot_json_body()
    days = _bot_days(body.get('days'))
    if days is None:
        return jsonify({'ok': False, 'error': 'Please enter the number of days as a whole number between 1 and 31.'}), 400
    try:
        student_id = int(body.get('student_id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Invalid member.'}), 400
    db = get_db()
    student = db.execute('SELECT id, name, sharing, total_rent, amount_paid FROM students WHERE id=%s', (student_id,)).fetchone()
    if not student:
        return jsonify({'ok': False, 'error': 'Member not found.'}), 404
    monthly = student['total_rent'] or 0
    per_day, total = prorated_rent(monthly, days, student['sharing'])
    return jsonify({'ok': True, 'student_id': student['id'], 'name': student['name'], 'days': days,
                    'sharing': student['sharing'],
                    'monthly_rent': monthly, 'per_day': round(per_day, 2), 'total': total,
                    'amount_paid': student['amount_paid'] or 0,
                    'new_balance': max(round(total - (student['amount_paid'] or 0), 2), 0),
                    'overpaid': (student['amount_paid'] or 0) > total})


@app.route('/admin/api/bot/rent/update', methods=['POST'])
@admin_api_required
def bot_rent_update():
    """Saves the calculated per-day rent as this student's total rent (and recomputes the balance)."""
    if not bot_csrf_ok():
        return jsonify({'ok': False, 'error': 'Security check failed. Please refresh the page.'}), 400
    body = _bot_json_body()
    days = _bot_days(body.get('days'))
    if days is None:
        return jsonify({'ok': False, 'error': 'Invalid number of days.'}), 400
    try:
        student_id = int(body.get('student_id'))
        expected_monthly = float(body.get('monthly_rent'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Invalid request.'}), 400

    db = get_db()
    student = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not student:
        return jsonify({'ok': False, 'error': 'Member not found.'}), 404
    monthly = student['total_rent'] or 0
    # Guards against a double tap applying the same proration twice on an already-updated rent.
    if abs(monthly - expected_monthly) > 0.005:
        return jsonify({'ok': False, 'error': "This member's rent has changed since you calculated. Please calculate again."}), 409

    _, new_total = prorated_rent(monthly, days, student['sharing'])
    paid = student['amount_paid'] or 0
    new_balance = max(round(new_total - paid, 2), 0)
    db.execute('UPDATE students SET total_rent=%s, balance=%s, updated_at=%s WHERE id=%s',
               (new_total, new_balance, datetime.utcnow().isoformat(), student_id))
    db.commit()
    sync_rent(db, [student_id])
    return jsonify({'ok': True, 'name': student['name'], 'total_rent': new_total, 'balance': new_balance})


# --------------------------------------------------------------------------
# PAUSE BED (admin)
# --------------------------------------------------------------------------

PAUSE_MAX_DAYS = 365


def _fmt_date(d):
    return d.strftime('%d %b %Y')


def pause_progress(start_date, days, today=None):
    """Everything about a pause is derived from its start date and length, so it expires by itself.
    The pause covers `days` days from start_date; end is the first day the bed is back in use."""
    today = today or today_ist()
    try:
        start = date.fromisoformat(start_date)
    except (TypeError, ValueError):
        start = today
    days = int(days or 0)
    end = start + timedelta(days=days)
    elapsed = min(max((today - start).days, 0), days)
    remaining = max(days - elapsed, 0)
    active = remaining > 0
    return {
        'start': start, 'end': end, 'start_label': _fmt_date(start), 'end_label': _fmt_date(end),
        'days': days, 'elapsed': elapsed, 'remaining': remaining, 'is_active': active,
        'status': 'Active Pause' if active else 'Expired',
        'percent': int(round(elapsed * 100 / days)) if days else 100,
    }


def get_pauses(db):
    """All pause records joined with the student's current details (active first, then expired)."""
    rows = db.execute('''
        SELECT p.id AS pause_id, p.start_date, p.days, p.created_at,
               s.id AS student_id, s.name, s.contact, s.hostel, s.room_number, s.sharing
        FROM paused_beds p
        JOIN students s ON s.id = p.student_id
        ORDER BY p.id DESC
    ''').fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.update(pause_progress(d['start_date'], d['days']))
        d['room_text'] = room_label(d['room_number'])
        out.append(d)
    out.sort(key=lambda x: (not x['is_active'], x['remaining'] if x['is_active'] else 0, -x['pause_id']))
    return out


@app.context_processor
def inject_admin_nav():
    """Small counters for the admin sidebar badges. Only runs on admin pages for a logged-in admin."""
    if not (session.get('admin_logged_in') and request.endpoint != 'admin_login'):
        return {}
    counts = {'pending': 0, 'pauses': 0, 'deleted': 0, 'guests': 0, 'notes': 0, 'trash': 0}
    try:
        db = get_db()
        # SPEED: all sidebar counters in ONE query.
        row = db.execute('''SELECT
            (SELECT COUNT(*) FROM deleted_students) AS deleted,
            (SELECT COUNT(*) FROM guests WHERE status='STAYING') AS guests,
            (SELECT COUNT(*) FROM admin_notes) AS notes,
            (SELECT COUNT(*) FROM data_cleanups WHERE status='DELETED') AS trash,
            (SELECT COUNT(*) FROM payment_submissions WHERE status='PENDING') AS pending,
            (SELECT COUNT(*) FROM paused_beds WHERE end_date > %s) AS pauses''', (today_ist().isoformat(),)).fetchone()
        for k in counts:
            if k in row:
                counts[k] = row[k] or 0
    except Exception:
        try:
            get_db().conn.rollback()
        except Exception:
            pass
    return {'admin_nav': counts}


@app.route('/admin/pause-bed')
@login_required
def pause_bed():
    db = get_db()
    pauses = get_pauses(db)
    active = [p for p in pauses if p['is_active']]
    expired = [p for p in pauses if not p['is_active']]
    paused_ids = {p['student_id'] for p in active}
    structure = get_rooms_structure(db)
    wizard = {}
    for hostel in HOSTELS:
        wizard[hostel] = [{
            'room': rn,
            'label': room_label(rn),
            'students': [{'id': st['id'], 'name': st['name'], 'contact': st['contact'], 'sharing': st['sharing'],
                          'paused': st['id'] in paused_ids} for st in studs],
        } for rn, studs in structure[hostel].items()]
    return render_template(
        'pause_bed.html',
        active=active, expired=expired, wizard=wizard,
        expiring_soon=len([p for p in active if p['remaining'] <= 2]),
        max_days=PAUSE_MAX_DAYS, today_label=_fmt_date(today_ist()), today_iso=today_ist().isoformat(),
    )


@app.route('/admin/pause-bed/create', methods=['POST'])
@login_required
def pause_bed_create():
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('pause_bed'))

    ids = []
    for v in request.form.getlist('student_ids'):
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i not in ids:
            ids.append(i)
    try:
        days = int((request.form.get('days') or '').strip())
    except ValueError:
        days = 0

    if not ids:
        flash('Please select at least one student to pause.', 'error')
        return redirect(url_for('pause_bed'))
    if not 1 <= days <= PAUSE_MAX_DAYS:
        flash(f'Please enter the number of days (1 to {PAUSE_MAX_DAYS}).', 'error')
        return redirect(url_for('pause_bed'))
    ids = ids[:50]

    db = get_db()
    marks = ','.join(['%s'] * len(ids))
    students = {r['id']: dict(r) for r in db.execute(f'SELECT id, name FROM students WHERE id IN ({marks})', ids).fetchall()}
    existing = db.execute(f'SELECT student_id, start_date, days FROM paused_beds WHERE student_id IN ({marks})', ids).fetchall()
    already = {e['student_id'] for e in existing if pause_progress(e['start_date'], e['days'])['is_active']}

    start = today_ist()
    end = start + timedelta(days=days)
    admin_name = session.get('admin_username') or ADMIN_USERNAME
    created, skipped = [], []
    for sid in ids:
        st = students.get(sid)
        if not st:
            continue
        if sid in already:
            skipped.append(st['name'])
            continue
        db.execute('''
            INSERT INTO paused_beds (student_id, start_date, days, end_date, created_at, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (sid, start.isoformat(), days, end.isoformat(), datetime.utcnow().isoformat(), admin_name))
        created.append(st['name'])
    db.commit()

    if created:
        flash(f"Bed paused for {', '.join(created)} — {days} day{'s' if days != 1 else ''} (until {_fmt_date(end)}).", 'success')
    if skipped:
        flash(f"Already paused (not changed): {', '.join(skipped)}.", 'error')
    if not created and not skipped:
        flash('No matching students were found.', 'error')
    return redirect(url_for('pause_bed'))


@app.route('/admin/pause-bed/<int:pause_id>/delete', methods=['POST'])
@login_required
def pause_bed_delete(pause_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('pause_bed'))
    db = get_db()
    # Only the pause record is removed. The student and their payments are never touched here.
    db.execute('DELETE FROM paused_beds WHERE id=%s', (pause_id,))
    db.commit()
    flash('Paused-bed record deleted.', 'success')
    return redirect(url_for('pause_bed'))


# --------------------------------------------------------------------------
# AI ASSISTANTS (user-side help bot + admin assistant) - logic lives in assistant.py
# --------------------------------------------------------------------------

import assistant

assistant.register(app, {
    'get_db': get_db,
    'validate_csrf': validate_csrf,
    'admin_api_required': admin_api_required,
    'check_capacity': check_capacity,
    'room_occupancy': room_occupancy,
    'rooms_for_hostel': rooms_for_hostel,
    'get_dashboard_stats': get_dashboard_stats,
    'daily_rate': daily_rent_for_sharing,
    'HOSTELS': HOSTELS,
    'current_payment_student': current_payment_student,
    'student_due_amount': student_due_amount,
    'room_label': room_label,
    'do_delete_student': do_delete_student,
    'do_restore_deleted': do_restore_deleted,
    'do_clear_student_payment': do_clear_student_payment,
    'do_reset_all_payments': do_reset_all_payments,
    'reset_counts': reset_counts,
})


# --------------------------------------------------------------------------
# DUE LIST
# --------------------------------------------------------------------------

@app.route('/admin/due-list')
@login_required
def due_list():
    db = get_db()
    today = today_ist()
    students, upcoming = get_due_and_upcoming(db)
    return render_template(
        'due_list.html',
        students=students,
        upcoming=upcoming,
        today=today.isoformat(),
        tomorrow=(today + timedelta(days=1)).isoformat(),
        cleared_on=get_setting(db, CLEARED_ON_KEY, ''),
    )


@app.route('/admin/due-list/<int:student_id>/settle', methods=['POST'])
@login_required
def settle_student(student_id):
    """"Already paid": the admin confirms this student has fully paid his current rent month. He leaves the Due List
    and comes back on his next due day. Undo from the student's Payments page."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('due_list'))
    db = get_db()
    rows = all_rent_states(db, [student_id])
    if not rows:
        abort(404)
    st, s = rows[0]
    undo = request.form.get('undo') == '1'
    mark = None if undo else (s.get('current_due') or today_ist()).isoformat()
    db.execute('UPDATE students SET settled_on=%s, updated_at=%s WHERE id=%s', (mark, datetime.utcnow().isoformat(), student_id))
    db.commit()
    sync_rent(db, [student_id])
    if undo:
        flash(f"{st['name']} is no longer marked as paid.", 'success')
        return redirect(url_for('student_payment_details', student_id=student_id))
    nxt = s.get('next_due')
    flash(f"{st['name']} marked as fully paid." + (f" Next due {pretty_date(nxt.isoformat())}." if nxt else ''), 'success')
    back = request.form.get('back') or ''
    return redirect(back if back.startswith('/') and not back.startswith('//') else url_for('due_list'))


@app.route('/admin/due-list/<int:student_id>/note', methods=['POST'])
@login_required
def save_due_note(student_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('due_list'))
    db = get_db()
    student = db.execute('SELECT id, name FROM students WHERE id=%s', (student_id,)).fetchone()
    if not student:
        abort(404)
    note = request.form.get('note', '').strip()[:300]
    db.execute('UPDATE students SET note=%s, updated_at=%s WHERE id=%s',
               (note, datetime.utcnow().isoformat(), student_id))
    db.commit()
    flash(f"Note saved for {student['name']}." if note else f"Note cleared for {student['name']}.", 'success')
    return redirect(url_for('due_list') + f'#student-{student_id}')


# --------------------------------------------------------------------------
# PAYMENT SETTINGS (per-hostel QR, payment number and account name only)
# --------------------------------------------------------------------------

@app.route('/admin/settings/payment', methods=['GET', 'POST'])
@login_required
def payment_settings():
    db = get_db()
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('payment_settings'))

        hostel = request.form.get('hostel', '').strip()
        if hostel not in HOSTEL_SETTING_KEYS:
            flash('Invalid hostel.', 'error')
            return redirect(url_for('payment_settings'))
        k = HOSTEL_SETTING_KEYS[hostel]

        if request.form.get('action') == 'remove':
            old_qr = get_setting(db, f'qr_url_{k}', '')
            set_setting(db, f'qr_url_{k}', '')
            set_setting(db, f'pay_phone_{k}', '')
            set_setting(db, f'pay_name_{k}', '')
            db.commit()
            if old_qr:
                delete_photo_from_supabase(old_qr)
            flash(f'{hostel} saved payment details removed.', 'success')
            return redirect(url_for('payment_settings') + f'#{k}-hostel')

        phone_raw = request.form.get('pay_phone', '').strip()
        pay_name = request.form.get('pay_name', '').strip()[:100]
        phone = ''
        if phone_raw:
            ok, phone = validate_mobile(phone_raw)
            if not ok:
                flash('Payment / PhonePe number must be a valid 10-digit mobile number.', 'error')
                return redirect(url_for('payment_settings'))

        old_qr = get_setting(db, f'qr_url_{k}', '')
        new_qr = None
        qr_file = request.files.get('qr_image')
        if qr_file and qr_file.filename:
            try:
                new_qr = save_qr(qr_file, f'qr_{k}')
            except ValueError as e:
                flash(str(e), 'error')
                return redirect(url_for('payment_settings'))

        set_setting(db, f'pay_phone_{k}', phone)
        set_setting(db, f'pay_name_{k}', pay_name)
        if new_qr:
            set_setting(db, f'qr_url_{k}', new_qr)
        db.commit()
        if new_qr and old_qr and old_qr != new_qr:
            delete_photo_from_supabase(old_qr)

        flash(f'{hostel} payment details updated.', 'success')
        return redirect(url_for('payment_settings') + f'#{k}-hostel')

    hostel_payment = {h: get_hostel_payment_info(db, h) for h in HOSTELS}
    return render_template('payment_settings.html', hostel_payment=hostel_payment)


# --------------------------------------------------------------------------
# STUDENT PAYMENTS (public — identified by contact number, no password)
# --------------------------------------------------------------------------

@app.route('/payments', methods=['GET', 'POST'])
def payments_lookup():
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('payments_lookup'))

        contact = request.form.get('contact', '').strip()
        mobile_ok, clean_contact = validate_mobile(contact)
        if not mobile_ok:
            flash('Please enter a valid 10-digit mobile number.', 'error')
            return redirect(url_for('payments_lookup'))

        db = get_db()
        student = db.execute(
            'SELECT * FROM students WHERE contact=%s ORDER BY id DESC LIMIT 1', (clean_contact,)
        ).fetchone()
        if not student:
            flash('No student found with that contact number. Please check with the hostel admin.', 'error')
            return redirect(url_for('payments_lookup'))

        session['payment_student_id'] = student['id']
        return redirect(url_for('payments_dashboard'))

    return render_template('payments_lookup.html')


@app.route('/payments/dashboard')
def payments_dashboard():
    student_id = session.get('payment_student_id')
    if not student_id:
        flash('Please enter your contact number first.', 'error')
        return redirect(url_for('payments_lookup'))

    db = get_db()
    student = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not student:
        session.pop('payment_student_id', None)
        flash('Student record not found. Please try again.', 'error')
        return redirect(url_for('payments_lookup'))
    student = dict(student)

    pay_info = get_hostel_payment_info(db, student['hostel'])
    due_amount = student_due_amount(student)
    recent = db.execute('''
        SELECT * FROM payment_submissions WHERE student_id=%s ORDER BY id DESC LIMIT 5
    ''', (student_id,)).fetchall()

    # Notifications are read from the existing payment data and are scoped to THIS student only.
    since = (datetime.utcnow() - timedelta(days=14)).isoformat()
    confirmed_notes = db.execute('''
        SELECT id, amount, confirmed_at FROM payment_submissions
        WHERE student_id=%s AND status='CONFIRMED' AND COALESCE(student_seen, 0) = 0 AND confirmed_at >= %s
        ORDER BY id DESC LIMIT 3
    ''', (student_id, since)).fetchall()
    pending_row = db.execute('''
        SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS amt FROM payment_submissions
        WHERE student_id=%s AND status='PENDING'
    ''', (student_id,)).fetchone()

    return render_template(
        'payments_dashboard.html',
        student=student,
        due_amount=due_amount,
        pay_info=pay_info,
        for_month=student_rent_label(db, student),
        recent=[dict(r) for r in recent],
        confirmed_notes=[dict(r) for r in confirmed_notes],
        pending_count=pending_row['c'] or 0,
        pending_amount=pending_row['amt'] or 0,
        pay_success=session.pop('pay_success', None),
    )


@app.route('/payments/notifications/<int:payment_id>/dismiss', methods=['POST'])
def payments_dismiss_notification(payment_id):
    student_id = session.get('payment_student_id')
    if not student_id:
        return redirect(url_for('payments_lookup'))
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('payments_dashboard'))
    db = get_db()
    # Only this student's own notification can be dismissed; the payment itself is unchanged.
    db.execute('UPDATE payment_submissions SET student_seen=1 WHERE id=%s AND student_id=%s', (payment_id, student_id))
    db.commit()
    return redirect(url_for('payments_dashboard'))


@app.route('/payments/submit', methods=['GET', 'POST'])
def payments_submit():
    db = get_db()
    student = current_payment_student(db)
    if not student:
        flash('Please enter your mobile number first.', 'error')
        return redirect(url_for('payments_lookup'))

    # There is no separate payment page: the form is the existing Add Student page (pre-filled).
    if request.method == 'GET':
        return redirect(url_for('add_student', record_payment=1))

    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('add_student', record_payment=1))

    # Student identity always comes from the database record, never from submitted fields.
    # The Add Student form's own fields are reused: "Amount Paid" is the payment being recorded.
    note = request.form.get('note', '').strip()[:300]
    entered = {
        'amount_paid': request.form.get('amount_paid', '').strip(),
        'payment_date': request.form.get('payment_date', '').strip(),
        'payment_mode': request.form.get('payment_mode', '').strip(),
        'cash_amount': request.form.get('cash_amount', '').strip(),
        'paid_to': request.form.get('paid_to', '').strip(),
        'note': note,
    }

    errors = []
    amount_val = None
    try:
        amount_val = float(entered['amount_paid'])
        if amount_val <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        errors.append('Please enter a valid payment amount.')
        amount_val = None
    if amount_val is not None and amount_val > student_due_amount(student):
        errors.append('Amount paid cannot be greater than your current due.')

    pay = parse_payment_fields(request.form, amount_val, errors)
    if not pay['payment_mode']:
        errors.append('Please select the payment mode.')

    if errors:
        for e in errors:
            flash(e, 'error')
        return render_record_payment(student, entered)

    proof_url = None
    try:
        receipt_file = request.files.get('receipt')
        if receipt_file and receipt_file.filename:
            proof_url = save_receipt(receipt_file)
    except ValueError as e:
        flash(str(e), 'error')
        return render_record_payment(student, entered)

    # Saved as PENDING only: the student's official paid amount and due are NOT touched here.
    # They change only when the admin clicks CONFIRM PAYMENT.
    now = datetime.utcnow().isoformat()
    db.execute('''
        INSERT INTO payment_submissions
        (student_id, hostel, room_number, name, contact, amount, for_month, proof_filename, note, status, submitted_at,
         payment_mode, cash_amount, paid_to, payment_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s)
    ''', (student['id'], student['hostel'], student['room_number'], student['name'], student['contact'],
          amount_val, student_rent_label(db, student), proof_url, note, now,
          pay['payment_mode'], pay['cash_amount'], pay['paid_to'], pay['payment_date']))
    db.commit()

    # Shown once, as the "Payment Submitted" card on the dashboard (it is NOT a confirmation).
    session['pay_success'] = {
        'name': student['name'],
        'amount': amount_val,
        'mode': pay['payment_mode'],
        'date': pay['payment_date'] or today_ist().isoformat(),
    }
    flash('Payment recorded! It is now PENDING CONFIRMATION by the hostel admin.', 'success')
    return redirect(url_for('payments_dashboard'))


# --------------------------------------------------------------------------
# ADMIN — TODAY'S PAYMENTS
# --------------------------------------------------------------------------

@app.route('/admin/payments')
@login_required
def admin_payments():
    db = get_db()
    today_str = today_ist().isoformat()
    rows = db.execute('SELECT * FROM payment_submissions WHERE COALESCE(dismissed, 0) = 0 ORDER BY id DESC LIMIT 200').fetchall()
    submissions = [dict(r) for r in rows]
    for s in submissions:
        s['is_today'] = (s['submitted_at'] or '').startswith(today_str)
    pending = [s for s in submissions if s['status'] == 'PENDING']
    confirmed = [s for s in submissions if s['status'] == 'CONFIRMED']
    return render_template('admin_payments.html', pending=pending, confirmed=confirmed)


@app.route('/admin/payments/<int:payment_id>/confirm', methods=['POST'])
@login_required
def confirm_payment(payment_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('admin_payments'))

    db = get_db()
    payment = db.execute('SELECT * FROM payment_submissions WHERE id=%s', (payment_id,)).fetchone()
    if not payment:
        abort(404)
    payment = dict(payment)

    if payment['status'] == 'CONFIRMED':
        flash('This payment is already confirmed.', 'error')
        return redirect(url_for('admin_payments'))

    now = datetime.utcnow().isoformat()
    admin_name = session.get('admin_username') or ADMIN_USERNAME

    db.execute('''
        UPDATE payment_submissions SET status='CONFIRMED', confirmed_at=%s, confirmed_by=%s WHERE id=%s
    ''', (now, admin_name, payment_id))

    # The student's Paid / Balance are worked out again from his payments (current rent month only).
    if not payment.get('payment_date'):
        db.execute('UPDATE payment_submissions SET payment_date=%s WHERE id=%s', (today_ist().isoformat(), payment_id))
    db.commit()
    if payment['student_id']:
        sync_rent(db, [payment['student_id']])
    flash(f"Payment of ₹{payment['amount']:,.0f} from {payment['name']} confirmed.", 'success')
    return redirect(url_for('admin_payments'))


@app.route('/admin/payments/<int:payment_id>/delete', methods=['POST'])
@login_required
def delete_payment_notification(payment_id):
    """Deletes a payment notification from Today's Payments.
    PENDING  -> removed entirely (the student's money/balance was never touched, so nothing else changes).
    CONFIRMED -> only hidden from this list; the payment stays applied to the student and in the reports."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('admin_payments'))
    db = get_db()
    payment = db.execute('SELECT id, name, status FROM payment_submissions WHERE id=%s', (payment_id,)).fetchone()
    if not payment:
        flash('That notification no longer exists.', 'error')
        return redirect(url_for('admin_payments'))
    if payment['status'] == 'PENDING':
        db.execute('DELETE FROM payment_submissions WHERE id=%s', (payment_id,))
    else:
        db.execute('UPDATE payment_submissions SET dismissed=1 WHERE id=%s', (payment_id,))
    db.commit()
    flash(f"Notification from {payment['name']} deleted.", 'success')
    return redirect(url_for('admin_payments'))


@app.route('/admin/payments/clear-confirmed', methods=['POST'])
@login_required
def clear_confirmed_notifications():
    """Hides every CONFIRMED notification from the list (records are kept)."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('admin_payments'))
    db = get_db()
    db.execute("UPDATE payment_submissions SET dismissed=1 WHERE status='CONFIRMED'")
    db.commit()
    flash('Confirmed notifications cleared.', 'success')
    return redirect(url_for('admin_payments'))


# --------------------------------------------------------------------------
# DELETED STUDENTS (view + restore)
# --------------------------------------------------------------------------

def _deleted_student_rows(db):
    out = []
    for r in db.execute('SELECT * FROM deleted_students ORDER BY id DESC').fetchall():
        d = dict(r)
        count, cap = room_occupancy(db, d['hostel'], d['room_number'])
        d['room_full'] = count >= (cap if cap else d['sharing'])
        d['deleted_on'] = (d.get('deleted_at') or '')[:10]
        d['month_label'] = cycle_label(d.get('deleted_cycle'))
        out.append(d)
    return out


@app.route('/admin/deleted-students')
@login_required
def deleted_students():
    db = get_db()
    students = _deleted_student_rows(db)
    totals = {
        'count': len(students),
        'rent': sum(s['total_rent'] or 0 for s in students),
        'paid': sum(s['amount_paid'] or 0 for s in students),
        'balance': sum(s['balance'] or 0 for s in students),
    }
    return render_template('deleted_students.html', students=students, totals=totals)


@app.route('/admin/deleted-students/<int:deleted_id>/restore', methods=['POST'])
@login_required
def restore_student(deleted_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('deleted_students'))
    ok, msg, _row = do_restore_deleted(get_db(), deleted_id)
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('deleted_students'))


@app.route('/admin/deleted-students/<int:deleted_id>/purge', methods=['POST'])
@login_required
def purge_deleted_student(deleted_id):
    """Permanently removes a student from the deleted list (cannot be restored afterwards)."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('deleted_students'))
    db = get_db()
    row = db.execute('SELECT * FROM deleted_students WHERE id=%s', (deleted_id,)).fetchone()
    if not row:
        flash('That student is no longer in the deleted list.', 'error')
        return redirect(url_for('deleted_students'))
    delete_photo(row['photo_filename'])
    db.execute('DELETE FROM deleted_students WHERE id=%s', (deleted_id,))
    db.commit()
    flash(f"{row['name']} was permanently removed.", 'success')
    return redirect(url_for('deleted_students'))


# --------------------------------------------------------------------------
# GUESTS (short stays, charged per day)
# --------------------------------------------------------------------------

def _num(raw, default=None):
    try:
        return float(str(raw).replace(',', '').strip())
    except (TypeError, ValueError):
        return default


@app.route('/admin/guests')
@login_required
def guests():
    db = get_db()
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM guests ORDER BY CASE WHEN status='STAYING' THEN 0 ELSE 1 END, id DESC").fetchall()]
    for g_ in rows:
        ci = parse_iso_date(g_['check_in'])
        g_['last_day'] = (ci + timedelta(days=g_['days'] - 1)).isoformat() if ci else None
        g_['balance'] = max(round((g_['total_amount'] or 0) - (g_['amount_paid'] or 0), 2), 0)
        g_['place'] = (f"{g_['hostel']} • {room_label(g_['room_number'])}" if g_['hostel'] and g_['room_number']
                       else (g_['hostel'] or ''))
    staying = [g_ for g_ in rows if g_['status'] == 'STAYING']
    totals = {
        'staying': len(staying),
        'total': sum(g_['total_amount'] or 0 for g_ in rows),
        'paid': sum(g_['amount_paid'] or 0 for g_ in rows),
        'balance': sum(g_['balance'] for g_ in rows),
    }
    return render_template('guests.html', guests=rows, totals=totals)


@app.route('/admin/guests/add', methods=['POST'])
@login_required
def guest_add():
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('guests'))
    f = request.form
    name = (f.get('name') or '').strip()
    contact = (f.get('contact') or '').strip()
    hostel = (f.get('hostel') or '').strip() or None
    room = f.get('room_number') or ''
    days = _num(f.get('days'))
    rate = _num(f.get('rate_per_day'), 0)
    paid = _num(f.get('amount_paid'), 0) or 0
    mode = (f.get('payment_mode') or '').strip()
    paid_to = (f.get('paid_to') or '').strip()
    note = (f.get('note') or '').strip()[:300]
    check_in = parse_iso_date(f.get('check_in')) or today_ist()

    errors = []
    if not name:
        errors.append('Guest name is required.')
    if contact:
        ok, contact = validate_mobile(contact)
        if not ok:
            errors.append('Enter a valid 10-digit mobile number (or leave it blank).')
    if hostel and hostel not in HOSTELS:
        errors.append('Choose a valid hostel.')
    room_number = None
    if room:
        try:
            room_number = int(room)
        except ValueError:
            room_number = None
        if room_number is None or not 1 <= room_number <= HALL_ROOM_NUMBER:
            errors.append('Choose a valid room.')
    if days is None or days < 1 or days != int(days) or days > 365:
        errors.append('Number of days must be a whole number from 1 to 365.')
    if rate is None or rate < 0:
        errors.append('Rate per day cannot be negative.')
    total = round((days or 0) * (rate or 0), 2)
    if paid < 0 or paid > total:
        errors.append('Amount paid must be between 0 and the total amount.')
    if paid > 0 and mode not in PAYMENT_MODES:
        errors.append('Choose how the guest paid (Online / Cash).')
    if errors:
        for e in errors:
            flash(e, 'error')
        return redirect(url_for('guests'))

    db = get_db()
    db.execute('''
        INSERT INTO guests (name, contact, hostel, room_number, check_in, days, rate_per_day, total_amount,
                            amount_paid, payment_mode, paid_to, note, status, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'STAYING',%s)
    ''', (name, contact, hostel, room_number if hostel else None, check_in.isoformat(), int(days), rate, total,
          paid, mode if paid > 0 else None, paid_to if paid > 0 else None, note, datetime.utcnow().isoformat()))
    db.commit()
    flash(f"Guest {name} added for {int(days)} day(s): ₹{total:,.0f} total.", 'success')
    return redirect(url_for('guests'))


@app.route('/admin/guests/<int:guest_id>/pay', methods=['POST'])
@login_required
def guest_pay(guest_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('guests'))
    db = get_db()
    g_ = db.execute('SELECT * FROM guests WHERE id=%s', (guest_id,)).fetchone()
    if not g_:
        flash('That guest no longer exists.', 'error')
        return redirect(url_for('guests'))
    amount = _num(request.form.get('amount'))
    mode = (request.form.get('payment_mode') or '').strip()
    balance = round((g_['total_amount'] or 0) - (g_['amount_paid'] or 0), 2)
    if amount is None or amount <= 0:
        flash('Enter an amount greater than 0.', 'error')
    elif amount > balance + 0.005:
        flash(f"Amount is more than the balance (₹{balance:,.0f}).", 'error')
    elif mode not in PAYMENT_MODES:
        flash('Choose how the guest paid (Online / Cash).', 'error')
    else:
        db.execute('UPDATE guests SET amount_paid = amount_paid + %s, payment_mode=%s, paid_to=COALESCE(NULLIF(%s,\'\'), paid_to) WHERE id=%s',
                   (amount, mode, (request.form.get('paid_to') or '').strip(), guest_id))
        db.commit()
        flash(f"₹{amount:,.0f} received from {g_['name']}.", 'success')
    return redirect(url_for('guests'))


@app.route('/admin/guests/<int:guest_id>/extend', methods=['POST'])
@login_required
def guest_extend(guest_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('guests'))
    db = get_db()
    g_ = db.execute('SELECT * FROM guests WHERE id=%s', (guest_id,)).fetchone()
    extra = _num(request.form.get('days'))
    if not g_:
        flash('That guest no longer exists.', 'error')
    elif extra is None or extra < 1 or extra != int(extra) or (g_['days'] + int(extra)) > 365:
        flash('Enter a whole number of extra days (1 or more).', 'error')
    else:
        extra = int(extra)
        db.execute('UPDATE guests SET days = days + %s, total_amount = total_amount + %s, status=\'STAYING\', check_out=NULL WHERE id=%s',
                   (extra, round(extra * (g_['rate_per_day'] or 0), 2), guest_id))
        db.commit()
        flash(f"{g_['name']}'s stay extended by {extra} day(s).", 'success')
    return redirect(url_for('guests'))


@app.route('/admin/guests/<int:guest_id>/checkout', methods=['POST'])
@login_required
def guest_checkout(guest_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('guests'))
    db = get_db()
    g_ = db.execute('SELECT name FROM guests WHERE id=%s', (guest_id,)).fetchone()
    if g_:
        db.execute("UPDATE guests SET status='CHECKED OUT', check_out=%s WHERE id=%s", (today_ist().isoformat(), guest_id))
        db.commit()
        flash(f"{g_['name']} checked out.", 'success')
    return redirect(url_for('guests'))


@app.route('/admin/guests/<int:guest_id>/delete', methods=['POST'])
@login_required
def guest_delete(guest_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('guests'))
    db = get_db()
    g_ = db.execute('SELECT name FROM guests WHERE id=%s', (guest_id,)).fetchone()
    if g_:
        db.execute('DELETE FROM guests WHERE id=%s', (guest_id,))
        db.commit()
        flash(f"Guest {g_['name']} deleted.", 'success')
    return redirect(url_for('guests'))


# --------------------------------------------------------------------------
# DELETE PAYMENTS: one student (from This Month Payments) or everything (Reset All Payments)
# --------------------------------------------------------------------------

@app.route('/admin/month-payments/<int:student_id>/remove', methods=['POST'])
@login_required
def month_payment_remove(student_id):
    """Really deletes this student's payment: Total Paid goes back to 0 (balance = full rent)."""
    view = 'deleted' if request.form.get('view') == 'deleted' else None
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('month_payments', view=view))
    ok, name, amount = do_clear_student_payment(get_db(), student_id)
    if ok:
        flash(f"{name}'s payment of ₹{amount:,.0f} was deleted. Paid is now ₹0.", 'success')
    else:
        flash('That student no longer exists.', 'error')
    try:
        idx = max(int(request.form.get('i', 0)), 0)
    except ValueError:
        idx = 0
    return redirect(url_for('month_payments', i=idx, view=view, q=request.form.get('q') or None, hostel=request.form.get('hostel') or None))


@app.route('/admin/delete-all-data')
@login_required
def reset_payments():
    db = get_db()
    return render_template('reset_payments.html', p=delete_all_preview(db), cleanups=get_cleanups(db),
                           cleared_on=get_setting(db, CLEARED_ON_KEY, ''))


@app.route('/admin/delete-all-data/confirm', methods=['POST'])
@login_required
def reset_payments_confirm():
    """Delete All Data. Only runs when the admin typed DELETE."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('reset_payments'))
    if (request.form.get('confirm_text') or '').strip().upper() != 'DELETE':
        flash('Nothing was deleted. Type DELETE in the box to confirm.', 'error')
        return redirect(url_for('reset_payments'))
    db = get_db()
    try:
        s = do_delete_all_data(db, session.get('admin_username') or ADMIN_USERNAME)
    except Exception:
        app.logger.exception('Delete All Data failed')
        db.conn.rollback()
        flash('Could not delete the data. Nothing was changed. Please try again.', 'error')
        return redirect(url_for('reset_payments'))
    flash(f"Deleted: {s['full']} fully paid student(s) cleared, {s['carried']} student(s) had their balance "
          f"(₹{s['carry_total']:,.0f} in total) added to Total Rent. {s['payments']} payment record(s) moved to the "
          "trash below. You can still Revert.", 'success')
    return redirect(url_for('reset_payments'))


@app.route('/admin/delete-all-data/<int:cleanup_id>/revert', methods=['POST'])
@login_required
def cleanup_revert(cleanup_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('reset_payments'))
    db = get_db()
    try:
        ok, msg = do_revert_cleanup(db, cleanup_id)
    except Exception:
        app.logger.exception('Revert failed')
        db.conn.rollback()
        ok, msg = False, 'Could not revert. Nothing was changed. Please try again.'
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('reset_payments'))


@app.route('/admin/delete-all-data/<int:cleanup_id>/purge', methods=['POST'])
@login_required
def cleanup_purge(cleanup_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('reset_payments'))
    if (request.form.get('confirm_text') or '').strip().upper() != 'DELETE':
        flash('Nothing was removed. Type DELETE to permanently delete.', 'error')
        return redirect(url_for('reset_payments'))
    ok, msg = do_purge_cleanup(get_db(), cleanup_id)
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('reset_payments'))


# --------------------------------------------------------------------------
# ADMIN NOTES (the admin's own notes)
# --------------------------------------------------------------------------

@app.route('/admin/notes', methods=['GET', 'POST'])
@login_required
def admin_notes():
    db = get_db()
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('admin_notes'))
        action = request.form.get('action')
        note_id = request.form.get('id', type=int)
        title = (request.form.get('title') or '').strip()[:120]
        body = (request.form.get('body') or '').strip()[:5000]
        now_iso = now_ist().isoformat()
        if action in ('add', 'edit'):
            if not title and not body:
                flash('Write something in the note first.', 'error')
                return redirect(url_for('admin_notes'))
            if action == 'add':
                db.execute('INSERT INTO admin_notes (title, body, pinned, created_at, updated_at) VALUES (%s,%s,0,%s,%s)',
                           (title, body, now_iso, now_iso))
                flash('Note saved.', 'success')
            else:
                db.execute('UPDATE admin_notes SET title=%s, body=%s, updated_at=%s WHERE id=%s', (title, body, now_iso, note_id))
                flash('Note updated.', 'success')
        elif action == 'pin':
            db.execute('UPDATE admin_notes SET pinned = 1 - pinned WHERE id=%s', (note_id,))
        elif action == 'delete':
            db.execute('DELETE FROM admin_notes WHERE id=%s', (note_id,))
            flash('Note deleted.', 'success')
        db.commit()
        return redirect(url_for('admin_notes'))
    notes = [dict(r) for r in db.execute('SELECT * FROM admin_notes ORDER BY pinned DESC, updated_at DESC, id DESC').fetchall()]
    return render_template('admin_notes.html', notes=notes, edit_id=request.args.get('edit', type=int))


# --------------------------------------------------------------------------
# EDIT / DELETE STUDENT
# --------------------------------------------------------------------------

@app.route('/admin/students/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_student(student_id):
    db = get_db()
    student = db.execute('SELECT * FROM students WHERE id = %s', (student_id,)).fetchone()
    if not student:
        abort(404)
    student = dict(student)

    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('edit_student', student_id=student_id))

        hostel = request.form.get('hostel', '').strip()
        room_number = request.form.get('room_number', '').strip()
        sharing = request.form.get('sharing', '').strip()
        name = request.form.get('name', '').strip()
        contact = request.form.get('contact', '').strip()
        total_rent = request.form.get('total_rent', '').strip()

        errors = []
        if hostel not in HOSTELS:
            errors.append('Please select a valid hostel.')
        try:
            room_number = int(room_number)
            if room_number not in rooms_for_hostel(hostel if hostel in HOSTELS else None):
                errors.append('Please select a valid room number.')
        except (ValueError, TypeError):
            errors.append('Please select a valid room number.')
            room_number = None
        try:
            sharing = int(sharing)
            if sharing not in SHARINGS:
                errors.append('Please select a valid sharing option.')
        except (ValueError, TypeError):
            errors.append('Please select a valid sharing option.')
            sharing = None
        if not name or len(name) < 2:
            errors.append('Please enter a valid student name.')

        mobile_ok, clean_contact = validate_mobile(contact)
        if not mobile_ok:
            errors.append('Please enter a valid 10-digit Indian mobile number.')

        try:
            total_rent = float(total_rent)
            if total_rent < 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append('Total rent must be a valid non-negative number.')
            total_rent = None
        # The stored amount paid is never edited here; it is only used to keep rent >= paid.
        amount_paid = _num(student.get('amount_paid'))
        if total_rent is not None and amount_paid > total_rent:
            errors.append('Total rent cannot be less than the amount already paid.')

        date_of_join = _form_date(request.form.get('date_of_join'), 'Date of join', errors)
        old_balance = _num(request.form.get('old_balance'), student.get('old_balance') or 0)
        if old_balance is None or old_balance < 0:
            errors.append('Old balance tag must be 0 or more.')
            old_balance = 0

        if not errors and room_number and sharing:
            room_changed = (hostel != student['hostel'] or room_number != student['room_number'])
            ok, msg = check_capacity(db, hostel, room_number, sharing, exclude_id=student_id)
            if not ok:
                errors.append(msg)

        if errors:
            for e in errors:
                flash(e, 'error')
            merged = dict(student)
            merged.update(request.form)
            return render_template('edit_student.html', student=merged)

        balance = round(total_rent - amount_paid, 2)
        now = datetime.utcnow().isoformat()

        # Only these columns are written. Payment fields, photo, receipt, note and due date stay untouched.
        db.execute('''
            UPDATE students
            SET hostel=%s, room_number=%s, sharing=%s, name=%s, contact=%s, total_rent=%s, balance=%s,
                date_of_join=%s, old_balance=%s, updated_at=%s
            WHERE id=%s
        ''', (hostel, room_number, sharing, name, clean_contact, total_rent, balance, date_of_join,
              round(old_balance, 2), now, student_id))
        db.commit()
        sync_rent(db, [student_id])   # a new rent or joined date can change the current rent month

        flash(f'{name} was updated successfully.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('edit_student.html', student=student)


@app.route('/admin/students/<int:student_id>/delete', methods=['POST'])
@login_required
def delete_student(student_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('admin_dashboard'))

    db = get_db()
    student = db.execute('SELECT * FROM students WHERE id = %s', (student_id,)).fetchone()
    if not student:
        abort(404)
    archive_student(db, student)   # keep a copy so it can be restored from "Deleted Students"
    db.execute('DELETE FROM students WHERE id = %s', (student_id,))
    db.commit()
    flash(f"{student['name']} was deleted. You can bring them back from Deleted Students.", 'success')
    return redirect(url_for('admin_dashboard'))


# --------------------------------------------------------------------------
# EXCEL EXPORT / IMPORT
# --------------------------------------------------------------------------

EXCEL_COLUMNS = ['ID', 'Hostel', 'Room Number', 'Sharing', 'Name', 'Contact',
                  'Total Rent', 'Amount Paid', 'Balance', 'Photo', 'Created Date']


def _style_header(ws, row_idx=1):
    header_fill = PatternFill(start_color='1E3A8A', end_color='1E3A8A', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF', size=11)
    for cell in ws[row_idx]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')


def _autosize(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                max_len = max(max_len, len(str(cell.value)) if cell.value is not None else 0)
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max(max_len + 4, 10), 40)


def _write_students_sheet(ws, students):
    ws.append(EXCEL_COLUMNS)
    for s in students:
        ws.append([
            s['id'], s['hostel'], s['room_number'], s['sharing'], s['name'], s['contact'],
            s['total_rent'], s['amount_paid'], s['balance'], s['photo_filename'] or '',
            s['created_at'][:10] if s['created_at'] else ''
        ])
    _style_header(ws)
    _autosize(ws)
    ws.freeze_panes = 'A2'


@app.route('/admin/export-excel')
@login_required
def export_excel():
    db = get_db()
    rows = db.execute('SELECT * FROM students ORDER BY hostel, room_number, LOWER(name)').fetchall()
    students = [dict(r) for r in rows]

    wb = openpyxl.Workbook()
    old_ws = wb.active
    old_ws.title = 'Old Hostel'
    _write_students_sheet(old_ws, [s for s in students if s['hostel'] == 'Old Hostel'])

    new_ws = wb.create_sheet('New Hostel')
    _write_students_sheet(new_ws, [s for s in students if s['hostel'] == 'New Hostel'])

    summary_ws = wb.create_sheet('Summary')
    stats = get_stats(db)
    summary_ws.append(['Metric', 'Value'])
    summary_ws.append(['Total Students', stats['total']])
    summary_ws.append(['Old Hostel Students', stats['old_count']])
    summary_ws.append(['New Hostel Students', stats['new_count']])
    summary_ws.append(['Total Rent (₹)', stats['total_rent']])
    summary_ws.append(['Total Paid (₹)', stats['total_paid']])
    summary_ws.append(['Total Balance (₹)', stats['total_balance']])
    summary_ws.append(['Generated On', datetime.now().strftime('%d-%m-%Y %H:%M')])
    _style_header(summary_ws)
    _autosize(summary_ws)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"hostel_students_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# --------------------------------------------------------------------------
# THIS MONTH PAYMENTS (swipe through one payment at a time, to note it in the manual register)
# Every payment recorded this month (student submission confirmed by admin, Add/Edit Student, etc.)
# lives in students.amount_paid, so it shows up here automatically. Removing one only hides it
# here (see Deleted Payments); it can be restored any time.
# --------------------------------------------------------------------------

def _month_payment_dict(row):
    d = dict(row)
    return {
        'id': d['id'],
        'hostel': d['hostel'],
        'room': room_label(d['room_number']),
        'name': d['name'],
        'contact': d['contact'],
        'total_rent': d['total_rent'] or 0,
        'amount_paid': d['amount_paid'] or 0,
        'balance': d['balance'] or 0,
        'paid_to': d.get('paid_to') or '',
        'mode': d.get('payment_mode') or '',
        'cash_amount': d.get('cash_amount') or 0,
        'payment_date': pretty_date(d.get('payment_date')) or '',
        'note': d.get('note') or '',
    }


def get_month_payments(db):
    """Returns (active, deleted): every student who has paid for his CURRENT rent month (from his due day)."""
    states = {st['id']: s for st, s in all_rent_states(db)}
    rows = db.execute('''
        SELECT * FROM students WHERE amount_paid > 0
        ORDER BY hostel, room_number, LOWER(name)
    ''').fetchall()
    dismissed = {
        (r['student_id'], r['month_start']): r['amount_paid']
        for r in db.execute('SELECT student_id, month_start, amount_paid FROM month_payment_dismissals').fetchall()
    }
    active, deleted = [], []
    for row in rows:
        item = _month_payment_dict(row)
        s = states.get(row['id'])
        key = (row['id'], s['window_start'].isoformat() if s and s.get('window_start') else '')
        item['since'] = pretty_date(key[1]) if key[1] else ''
        item['old_balance'] = row.get('old_balance') or 0
        if key in dismissed and abs((row['amount_paid'] or 0) - dismissed[key]) < 0.005:
            deleted.append(item)
        else:
            active.append(item)
    return active, deleted


def _rent_month_key(db, student_id):
    """Key of a student's current rent month (its first day), used to remember a hidden card."""
    try:
        _st, s = all_rent_states(db, [student_id])[0]
    except IndexError:
        return ''
    return s['window_start'].isoformat() if s.get('window_start') else ''


@app.route('/admin/month-payments')
@login_required
def month_payments():
    db = get_db()
    active, deleted = get_month_payments(db)
    view = 'deleted' if request.args.get('view') == 'deleted' else 'swipe'
    try:
        start_index = int(request.args.get('i', 0))
    except ValueError:
        start_index = 0
    start_index = max(0, min(start_index, max(len(active) - 1, 0)))
    totals = {
        'count': len(active),
        'total': sum(p['total_rent'] for p in active),
        'paid': sum(p['amount_paid'] for p in active),
        'balance': sum(p['balance'] for p in active),
    }
    return render_template('month_payments.html', payments=active, deleted=deleted, view=view,
                           start_index=start_index, totals=totals, q=(request.args.get('q') or '').strip(),
                           hostel_filter=request.args.get('hostel') or '')


CALC_CARDS = {
    'students': ('Total Students', 'count', None),
    'old': ('Old Hostel Students', 'count', 'Old Hostel'),
    'new': ('New Hostel Students', 'count', 'New Hostel'),
    'rent': ('Total Rent to Receive', 'rent', None),
    'paid': ('Total Paid', 'paid', None),
    'due': ('Balance Due', 'due', None),
}


def build_calculation(db, card):
    """Explains a dashboard card student by student, using EXACTLY the same numbers as the card:
    rent = each student's total rent; paid = what he paid for his CURRENT rent month (from his due day);
    due = rent minus that (never below 0)."""
    title, kind, only_hostel = CALC_CARDS[card]
    confirmed = {r['id']: (r['amount_paid'] or 0) for r in db.execute('SELECT id, amount_paid FROM students').fetchall()}
    students = [dict(r) for r in db.execute('SELECT * FROM students').fetchall()]
    students.sort(key=lambda x: (0 if x['hostel'] == 'Old Hostel' else 1, x['room_number'], (x['name'] or '').lower()))
    layout = [('Old Hostel', 'Old Hostel', lambda x: x['hostel'] == 'Old Hostel'),
              ('New Hostel (Rooms 1-10)', 'New Hostel', lambda x: x['hostel'] == 'New Hostel' and x['room_number'] != HALL_ROOM_NUMBER),
              ('Hall (New Hostel)', 'New Hostel', lambda x: x['hostel'] == 'New Hostel' and x['room_number'] == HALL_ROOM_NUMBER)]
    zero_hidden = 0
    groups = []
    not_counted = []
    for gtitle, ghostel, pick in layout:
        if only_hostel and ghostel != only_hostel:
            continue
        rooms = {}
        for st in students:
            if not pick(st):
                continue
            rent = st['total_rent'] or 0
            conf = confirmed.get(st['id'], 0)
            if kind == 'count':
                value, detail = 1, ''
            elif kind == 'rent':
                value, detail = rent, ''
            elif kind == 'paid':
                value, detail = conf, ''
            else:
                value = max(rent - conf, 0)
                detail = f"{rent:,.0f} − {conf:,.0f}"
            if kind in ('paid', 'due') and value <= 0:
                zero_hidden += 1
                continue
            rooms.setdefault(st['room_number'], []).append({'name': st['name'], 'value': value, 'detail': detail,
                                                            'contact': st.get('contact') or '', 'id': st['id']})
        room_list = []
        for rn in sorted(rooms):
            items = rooms[rn]
            room_list.append({'label': f"{ghostel} • {room_label(rn)}", 'students': items, 'value': sum(i['value'] for i in items),
                              'formula': ' + '.join(f"{i['value']:,.0f}" for i in items)})
        if room_list:
            gval = sum(r['value'] for r in room_list)
            groups.append({'title': gtitle, 'rooms': room_list, 'value': gval,
                           'formula': ' + '.join(f"{r['value']:,.0f}" for r in room_list)})
    grand = sum(g['value'] for g in groups)
    stats = get_dashboard_stats(db)
    expected = {'students': stats['total'], 'old': stats['old_count'], 'new': stats['new_count'],
                'rent': stats['total_rent'], 'paid': stats['total_paid'], 'due': stats['total_balance']}[card]
    extra = {}
    if kind == 'rent':
        d = db.execute('SELECT COUNT(*) AS n, COALESCE(SUM(total_rent),0) AS r FROM deleted_students').fetchone()
        extra = {'deleted_count': d['n'] or 0, 'deleted_rent': d['r'] or 0}
    return {'title': title, 'kind': kind, 'card': card, 'groups': groups, 'grand': grand, 'grand_formula': ' + '.join(f"{g['value']:,.0f}" for g in groups),
            'matches': abs(grand - expected) < 0.01, 'expected': expected, 'zero_hidden': zero_hidden,
            'not_counted': not_counted, 'month_label': 'current rent month', 'extra': extra,
            'student_count': sum(len(r['students']) for g in groups for r in g['rooms'])}


@app.route('/admin/students/<int:student_id>/payment-details')
@login_required
def student_payment_details(student_id):
    """A student's rent: his due day, what counts for his current rent month, and every stored payment
    (edit / delete each one). Payments are kept until Delete All Data."""
    db = get_db()
    row = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not row:
        abort(404)
    st = dict(row)
    subs = [dict(r) for r in db.execute(
        'SELECT * FROM payment_submissions WHERE student_id=%s ORDER BY id DESC LIMIT 30', (student_id,)).fetchall()]
    _s, state = all_rent_states(db, [student_id])[0]
    counted_ids = {p['id'] for p in state['counted']}
    pays = []
    for p in sorted(confirmed_payments(db, [student_id]).get(student_id, []), key=lambda p: (p['date'], p['id']), reverse=True):
        pays.append({'kind': 'submission', 'ref_id': p['id'], 'date': p['date'], 'amount': p['amount'] or 0,
                     'mode': p.get('payment_mode'), 'paid_to': p.get('paid_to'), 'cash': p.get('cash_amount'),
                     'proof': p.get('proof_filename'), 'note': p.get('note') or '', 'counted': p['id'] in counted_ids,
                     'name': st['name']})
    return render_template('student_payment_details.html', st=st, subs=subs, confirmed=state['paid'],
                           state=state, pays=pays,
                           remaining=max(round((st['total_rent'] or 0) - (st['amount_paid'] or 0), 2), 0),
                           today_iso=today_ist().isoformat())


@app.route('/admin/students/<int:student_id>/record-payment', methods=['POST'])
@login_required
def admin_record_payment(student_id):
    """Admin marks rent as paid (full or part) in one step. It is saved as a CONFIRMED payment and counts for the
    student's current rent month (money paid before his due day counts for the previous month)."""
    back = redirect(url_for('student_payment_details', student_id=student_id))
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return back
    db = get_db()
    st = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not st:
        abort(404)
    errors, v = _parse_payment_edit(request.form)
    if errors:
        for e_ in errors:
            flash(e_, 'error')
        return back
    now = datetime.utcnow().isoformat()
    db.execute('''
        INSERT INTO payment_submissions
        (student_id, hostel, room_number, name, contact, amount, for_month, proof_filename, note, status, submitted_at,
         confirmed_at, confirmed_by, payment_mode, cash_amount, paid_to, payment_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, 'CONFIRMED', %s, %s, %s, %s, %s, %s, %s)
    ''', (student_id, st['hostel'], st['room_number'], st['name'], st['contact'], v['amount'],
          student_rent_label(db, dict(st)), 'Recorded by admin', now, now, session.get('admin_username') or ADMIN_USERNAME,
          v['mode'], v['cash'], v['paid_to'], v['date'].isoformat()))
    db.commit()
    sync_rent(db, [student_id])
    flash(f"₹{v['amount']:,.0f} recorded for {st['name']}.", 'success')
    return back


def hostel_room_data(db):
    """Every hostel -> every room (in order, empty rooms too) -> every student with rent / paid / balance and the
    payments counted in his current rent month. Same numbers as the dashboard cards."""
    states = {st['id']: (st, s) for st, s in all_rent_states(db)}
    out = []
    for hostel in HOSTELS:
        rooms = []
        for rn in rooms_for_hostel(hostel):
            studs = []
            for st, s in sorted((v for v in states.values() if v[0]['hostel'] == hostel and v[0]['room_number'] == rn),
                                key=lambda v: (v[0]['name'] or '').lower()):
                studs.append({
                    'id': st['id'], 'name': st['name'], 'contact': st.get('contact') or '',
                    'rent': round(float(st.get('total_rent') or 0), 2),
                    'paid': round(float(st.get('amount_paid') or 0), 2),
                    'balance': round(float(st.get('balance') or 0), 2),
                    'joined': pretty_date(st.get('date_of_join')) or '',
                    'due_day': parse_iso_date(st.get('date_of_join')).day if parse_iso_date(st.get('date_of_join')) else None,
                    'next_due': pretty_date(s['next_due'].isoformat()) if s.get('next_due') else '',
                    'month_from': pretty_date(s['window_start'].isoformat()) if s.get('window_start') else '',
                    'status': s.get('status') or ('paid' if (st.get('balance') or 0) <= 0.005 else ''),
                    'old_balance': round(float(st.get('old_balance') or 0), 2),
                    'payments': [{'date': pretty_date(p_['date'].isoformat()), 'amount': round(float(p_['amount'] or 0), 2),
                                  'mode': p_.get('payment_mode') or 'Online', 'paid_to': p_.get('paid_to') or '',
                                  'cash': round(float(p_.get('cash_amount') or 0), 2),
                                  'receipt': photo_url(p_.get('proof_filename')) or ''} for p_ in s['counted']],
                })
            rooms.append({'room': rn, 'label': room_label(rn), 'sharing': studs and max(
                              (states[x['id']][0].get('sharing') or 0) for x in studs) or None,
                          'students': studs,
                          'rent': round(sum(x['rent'] for x in studs), 2), 'paid': round(sum(x['paid'] for x in studs), 2),
                          'balance': round(sum(x['balance'] for x in studs), 2)})
        out.append({'hostel': hostel, 'rooms': rooms,
                    'rent': round(sum(r['rent'] for r in rooms), 2), 'paid': round(sum(r['paid'] for r in rooms), 2),
                    'balance': round(sum(r['balance'] for r in rooms), 2),
                    'students': sum(len(r['students']) for r in rooms)})
    return out


@app.route('/admin/hostel-calculator')
@login_required
def hostel_calculator():
    """Pick a hostel, go room by room (Next / Previous), tap a name for his payments, and add amounts in the
    calculator at the bottom. The calculator keeps its value when you switch hostel."""
    db = get_db()
    return render_template('hostel_calculator.html', data=hostel_room_data(db), today=pretty_date(today_ist().isoformat()))


@app.route('/admin/how-cards-work')
@login_required
def how_cards_work():
    """Animated explainer (Telugu) of Total Rent / Total Paid / Balance Due, drawn from today's live numbers."""
    db = get_db()
    data = hostel_room_data(db)
    rooms = {h['hostel']: {r['label']: [[s['name'], s['rent'], s['paid'], s['balance']] for s in r['students']]
                           for r in h['rooms']} for h in data}
    return render_template('cards_explained_frame.html', rooms=rooms, today=today_ist().strftime('%d %b %Y'))


@app.route('/admin/how-cards-work/page')
@login_required
def how_cards_work_page():
    """The explainer inside the admin layout (the animation runs in a frame so its styles stay separate)."""
    return render_template('cards_explained.html')


@app.route('/admin/calculation/<card>')
@login_required
def calculation(card):
    if card not in CALC_CARDS:
        abort(404)
    return render_template('calculation.html', c=build_calculation(get_db(), card))


# ---- Expenditure & Profit / Loss ----------------------------------------------------------------

EXPENSE_CATEGORIES = ('Canteen', 'Hostel', 'Home')


def _expense_month_keys():
    today = today_ist()
    return today.strftime('%Y-%m'), (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


def _expense_month_key():
    """Month shown on the expenditure page: ?month=YYYY-MM, or ?m=last, else this month."""
    this_key, last_key = _expense_month_keys()
    raw = (request.values.get('month') or '').strip()
    if raw:
        try:
            return datetime.strptime(raw, '%Y-%m').strftime('%Y-%m')
        except ValueError:
            pass
    return last_key if request.values.get('m') == 'last' else this_key


def _pos_num(raw):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


@app.route('/admin/expenditure', methods=['GET', 'POST'])
@login_required
def expenditure():
    db = get_db()
    key = _expense_month_key()
    today = today_ist()

    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('expenditure', month=key))
        action = request.form.get('action')
        d = parse_iso_date(request.form.get('date'))
        now_iso = datetime.utcnow().isoformat()

        if action == 'add_expense':
            category = request.form.get('category')
            item = (request.form.get('item') or '').strip()[:120]
            qty = _pos_num(request.form.get('qty'))
            price = _pos_num(request.form.get('price'))
            if category not in EXPENSE_CATEGORIES or not item or qty is None or price is None or not d:
                flash('Please fill category, item, quantity, price and date correctly.', 'error')
            else:
                total = round(qty * price, 2)
                db.execute('''INSERT INTO expenses (category, item, qty, price, total, expense_date, created_at)
                              VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                           (category, item, qty, price, total, d.isoformat(), now_iso))
                db.commit()
                flash(f'Expense added: {item} — ₹{total:,.0f}', 'success')
            key = d.strftime('%Y-%m') if d else key

        elif action == 'add_canteen_income':
            amount = _pos_num(request.form.get('amount'))
            note = (request.form.get('note') or '').strip()[:120]
            if amount is None or not d:
                flash('Please enter the canteen income amount and date.', 'error')
            else:
                db.execute('INSERT INTO canteen_income (amount, income_date, note, created_at) VALUES (%s, %s, %s, %s)',
                           (amount, d.isoformat(), note, now_iso))
                db.commit()
                flash(f'Canteen income added: ₹{amount:,.0f}', 'success')
            key = d.strftime('%Y-%m') if d else key

        elif action == 'delete_expense':
            db.execute('DELETE FROM expenses WHERE id=%s', (request.form.get('id', type=int),))
            db.commit()
            flash('Expense deleted.', 'success')

        elif action == 'delete_canteen_income':
            db.execute('DELETE FROM canteen_income WHERE id=%s', (request.form.get('id', type=int),))
            db.commit()
            flash('Canteen income entry deleted.', 'success')

        return redirect(url_for('expenditure', month=key))

    # ---- GET: build the month's picture ----
    like = key + '-%'
    expenses = [dict(r) for r in db.execute(
        'SELECT * FROM expenses WHERE expense_date LIKE %s ORDER BY expense_date DESC, id DESC', (like,)).fetchall()]
    canteen_rows = [dict(r) for r in db.execute(
        'SELECT * FROM canteen_income WHERE income_date LIKE %s ORDER BY income_date DESC, id DESC', (like,)).fetchall()]

    cat_totals = {c: 0.0 for c in EXPENSE_CATEGORIES}
    days = {}
    for e in expenses:
        cat_totals[e['category']] = cat_totals.get(e['category'], 0) + (e['total'] or 0)
        day = days.setdefault(e['expense_date'], {'date': e['expense_date'], 'rows': [], 'total': 0.0,
                                                    'cats': {c: 0.0 for c in EXPENSE_CATEGORIES}})
        day['rows'].append(e)
        day['total'] += e['total'] or 0
        day['cats'][e['category']] = day['cats'].get(e['category'], 0) + (e['total'] or 0)
    daily = sorted(days.values(), key=lambda x: x['date'], reverse=True)

    canteen_income = round(sum(r['amount'] for r in canteen_rows), 2)
    total_expense = round(sum(cat_totals.values()), 2)

    this_key, last_key = _expense_month_keys()
    summary = {
        'canteen_income': canteen_income,
        'total_expense': total_expense,
        'cat_totals': cat_totals,
        'canteen_net': round(canteen_income - cat_totals['Canteen'], 2),
    }
    default_date = today.isoformat() if key == this_key else f'{key}-01'
    return render_template('expenditure.html', key=key, label=cycle_label(key), s=summary, daily=daily,
                           canteen_rows=canteen_rows, categories=EXPENSE_CATEGORIES,
                           this_key=this_key, last_key=last_key, default_date=default_date,
                           today_iso=today.isoformat())


# ---- edit / delete ONE payment ----------------------------------------------------------------

def _find_payment_event(db, kind, ref_id):
    if kind != 'submission':
        return None
    r = db.execute("SELECT * FROM payment_submissions WHERE id=%s AND status='CONFIRMED'", (ref_id,)).fetchone()
    if not r:
        return None
    p = dict(r)
    return {'kind': 'submission', 'ref_id': p['id'], 'student_id': p['student_id'], 'name': p['name'],
            'hostel': p['hostel'], 'room_number': p['room_number'], 'date': _pay_date(p), 'amount': p['amount'] or 0,
            'mode': p.get('payment_mode'), 'paid_to': p.get('paid_to'), 'cash': p.get('cash_amount')}


def _parse_payment_edit(form):
    """Returns (errors, values)."""
    errors = []
    amount = _num(form.get('amount'))
    if amount is None or amount <= 0 or amount > 1000000:
        errors.append('Amount must be more than 0.')
    pdate = parse_iso_date(form.get('payment_date'))
    if not pdate:
        errors.append('Choose a valid payment date.')
    elif pdate > today_ist():
        errors.append('The payment date cannot be in the future.')
    mode = (form.get('payment_mode') or '').strip()
    if mode not in PAYMENT_MODES:
        errors.append('Choose how it was paid (Online / Cash / Online + Cash).')
    cash = None
    if mode == 'Cash' and amount:
        cash = amount
    elif mode == 'Online + Cash':
        cash = _num(form.get('cash_amount'))
        if cash is None or cash <= 0 or (amount and cash >= amount):
            errors.append('For Online + Cash, enter the cash part (more than 0 and less than the amount).')
    paid_to = (form.get('paid_to') or '').strip()[:80]
    return errors, {'amount': amount, 'date': pdate, 'mode': mode, 'cash': cash, 'paid_to': paid_to}


def _after_payment_redirect(form, student_id=None):
    sid = form.get('student', type=int) or student_id
    if sid:
        return redirect(url_for('student_payment_details', student_id=sid))
    return redirect(url_for('month_payments'))


@app.route('/admin/payments/edit/<kind>/<int:ref_id>', methods=['GET', 'POST'])
@login_required
def edit_payment(kind, ref_id):
    db = get_db()
    ev = _find_payment_event(db, kind, ref_id)
    if not ev:
        flash('That payment was not found.', 'error')
        return redirect(url_for('month_payments'))
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('edit_payment', kind=kind, ref_id=ref_id))
        errors, v = _parse_payment_edit(request.form)
        if errors:
            for e_ in errors:
                flash(e_, 'error')
            return redirect(url_for('edit_payment', kind=kind, ref_id=ref_id, student=request.form.get('student')))
        db.execute('''UPDATE payment_submissions SET amount=%s, payment_date=%s, payment_mode=%s, cash_amount=%s,
                      paid_to=%s WHERE id=%s''',
                   (v['amount'], v['date'].isoformat(), v['mode'], v['cash'], v['paid_to'], ref_id))
        db.commit()
        if ev['student_id']:
            sync_rent(db, [ev['student_id']])
        flash(f"Payment of {ev['name']} updated.", 'success')
        return _after_payment_redirect(request.form, ev['student_id'])
    return render_template('edit_payment.html', ev=ev, today_iso=today_ist().isoformat(),
                           back_student=request.args.get('student', type=int) or ev['student_id'])


@app.route('/admin/payments/delete/<kind>/<int:ref_id>', methods=['POST'])
@login_required
def delete_payment_event(kind, ref_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return _after_payment_redirect(request.form)
    db = get_db()
    ev = _find_payment_event(db, kind, ref_id)
    if not ev:
        flash('That payment was not found.', 'error')
        return _after_payment_redirect(request.form)
    detail = f"One payment deleted (mode: {ev.get('mode') or '-'}, paid to: {ev.get('paid_to') or '-'}, date: {ev['date'].isoformat()})"
    db.execute('DELETE FROM payment_submissions WHERE id=%s', (ref_id,))
    _log_payment_delete(db, ev['student_id'], ev['name'], ev['hostel'], ev['room_number'], ev['amount'], detail)
    db.commit()
    if ev['student_id']:
        sync_rent(db, [ev['student_id']])
    flash(f"Payment of ₹{ev['amount']:,.0f} from {ev['name']} deleted.", 'success')
    return _after_payment_redirect(request.form, ev['student_id'])


@app.route('/admin/month-payments/<int:student_id>/delete', methods=['POST'])
@login_required
def month_payment_delete(student_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('month_payments'))
    db = get_db()
    row = db.execute('SELECT name, amount_paid FROM students WHERE id=%s', (student_id,)).fetchone()
    if not row:
        flash('That payment no longer exists.', 'error')
        return redirect(url_for('month_payments'))
    month_start = _rent_month_key(db, student_id)
    db.execute('''
        INSERT INTO month_payment_dismissals (student_id, month_start, amount_paid, dismissed_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (student_id, month_start)
        DO UPDATE SET amount_paid = EXCLUDED.amount_paid, dismissed_at = EXCLUDED.dismissed_at
    ''', (student_id, month_start, row['amount_paid'] or 0, datetime.utcnow().isoformat()))
    db.commit()
    flash(f"{row['name']}'s payment moved to Deleted Payments.", 'success')
    try:
        idx = int(request.form.get('i', 0))
    except ValueError:
        idx = 0
    return redirect(url_for('month_payments', i=idx, q=request.form.get('q') or None, hostel=request.form.get('hostel') or None))


@app.route('/admin/month-payments/<int:student_id>/restore', methods=['POST'])
@login_required
def month_payment_restore(student_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('month_payments', view='deleted'))
    db = get_db()
    db.execute('DELETE FROM month_payment_dismissals WHERE student_id=%s', (student_id,))
    db.commit()
    flash('Payment restored to This Month Payments.', 'success')
    return redirect(url_for('month_payments', view='deleted'))


@app.route('/admin/import-excel', methods=['POST'])
@login_required
def import_excel():
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('admin_dashboard'))

    file = request.files.get('excel_file')
    if not file or file.filename == '':
        flash('Please choose an Excel file to import.', 'error')
        return redirect(url_for('admin_dashboard'))

    if not file.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('Only .xlsx files are supported for import.', 'error')
        return redirect(url_for('admin_dashboard'))

    try:
        wb = openpyxl.load_workbook(file, data_only=True)
        ws = wb.active
    except Exception:
        flash('Could not read the uploaded Excel file. Please check the format.', 'error')
        return redirect(url_for('admin_dashboard'))

    db = get_db()
    header = [str(c.value).strip().lower() if c.value else '' for c in ws[1]]

    def col_index(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    idx = {
        'hostel': col_index('hostel'),
        'room': col_index('room number', 'room_number', 'room'),
        'sharing': col_index('sharing'),
        'name': col_index('name'),
        'contact': col_index('contact'),
        'rent': col_index('total rent', 'total_rent', 'rent'),
        'paid': col_index('amount paid', 'amount_paid', 'paid'),
    }

    if any(v is None for k, v in idx.items() if k in ('hostel', 'room', 'sharing', 'name', 'contact', 'rent', 'paid')):
        flash('Excel file is missing required columns: Hostel, Room Number, Sharing, Name, Contact, Total Rent, Amount Paid.', 'error')
        return redirect(url_for('admin_dashboard'))

    imported = 0
    skipped = 0
    now = datetime.utcnow().isoformat()

    for row in ws.iter_rows(min_row=2, values_only=True):
        try:
            hostel_raw = str(row[idx['hostel']]).strip() if row[idx['hostel']] else ''
            hostel = next((h for h in HOSTELS if h.lower() == hostel_raw.lower()), None)
            room_number = int(row[idx['room']])
            sharing = int(row[idx['sharing']])
            name = str(row[idx['name']]).strip() if row[idx['name']] else ''
            contact_raw = str(row[idx['contact']]).strip() if row[idx['contact']] else ''
            total_rent = float(row[idx['rent']])
            amount_paid = float(row[idx['paid']])

            mobile_ok, clean_contact = validate_mobile(contact_raw)

            if (not hostel or room_number not in ROOMS or sharing not in SHARINGS
                    or not name or len(name) < 2 or not mobile_ok
                    or total_rent < 0 or amount_paid < 0 or amount_paid > total_rent):
                skipped += 1
                continue

            ok, _ = check_capacity(db, hostel, room_number, sharing)
            if not ok:
                skipped += 1
                continue

            balance = round(total_rent - amount_paid, 2)
            new = db.execute('''
                INSERT INTO students
                (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance, photo_filename, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s) RETURNING id
            ''', (hostel, room_number, sharing, name, clean_contact, total_rent, amount_paid, balance, now, now)).fetchone()
            if amount_paid > 0:
                db.execute('''
                    INSERT INTO payment_submissions
                    (student_id, hostel, room_number, name, contact, amount, for_month, note, status, submitted_at,
                     confirmed_at, confirmed_by, payment_mode, payment_date, dismissed)
                    VALUES (%s,%s,%s,%s,%s,%s,'','Imported from Excel','CONFIRMED',%s,%s,'admin','Online',%s,1)
                ''', (new['id'], hostel, room_number, name, clean_contact, amount_paid, now, now, today_ist().isoformat()))
            imported += 1
        except Exception:
            skipped += 1
            continue

    db.commit()
    sync_rent(db)
    flash(f'Successfully imported: {imported} students. Skipped/Invalid: {skipped} rows.', 'success' if imported else 'error')
    return redirect(url_for('admin_dashboard'))


# --------------------------------------------------------------------------
# PDF GENERATION
# --------------------------------------------------------------------------

def _pdf_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle('MainTitle', fontSize=20, leading=24, alignment=TA_CENTER,
                               textColor=colors.HexColor('#1E3A8A'), spaceAfter=4, fontName='Helvetica-Bold'))
    styles.add(ParagraphStyle('SubTitle', fontSize=10, leading=14, alignment=TA_CENTER,
                               textColor=colors.HexColor('#555555'), spaceAfter=12))
    styles.add(ParagraphStyle('HostelHeading', fontSize=16, leading=20, alignment=TA_LEFT,
                               textColor=colors.white, fontName='Helvetica-Bold',
                               backColor=colors.HexColor('#1E3A8A'), spaceAfter=10, spaceBefore=14,
                               leftIndent=6, borderPadding=(6, 6, 6, 6)))
    styles.add(ParagraphStyle('RoomHeading', fontSize=13, leading=16, alignment=TA_LEFT,
                               textColor=colors.HexColor('#1E3A8A'), fontName='Helvetica-Bold',
                               spaceBefore=10, spaceAfter=4, borderWidth=0,
                               borderColor=colors.HexColor('#93C5FD'), borderPadding=2))
    styles.add(ParagraphStyle('EmptyRoom', fontSize=9.5, leading=12, alignment=TA_LEFT,
                               textColor=colors.HexColor('#888888'), fontName='Helvetica-Oblique',
                               spaceAfter=8))
    styles.add(ParagraphStyle('SummaryLabel', fontSize=10.5, leading=14, fontName='Helvetica-Bold',
                               textColor=colors.HexColor('#1E3A8A')))
    return styles


def _footer(canvas_obj, doc):
    canvas_obj.saveState()
    canvas_obj.setFont('Helvetica', 8)
    canvas_obj.setFillColor(colors.HexColor('#888888'))
    canvas_obj.drawString(20 * mm, 12 * mm, f"Generated on {datetime.now().strftime('%d-%m-%Y %H:%M')}")
    canvas_obj.drawRightString(doc.pagesize[0] - 20 * mm, 12 * mm, f"Page {doc.page}")
    canvas_obj.restoreState()


def _cell(text, style):
    return Paragraph(xml_escape(str(text)) if text not in (None, '') else '-', style)


def _receipt_cell(url, style):
    if url and str(url).startswith(('http://', 'https://')):
        return Paragraph(f'<link href="{xml_escape(url, {chr(34): "&quot;"})}" color="#1D4ED8"><u>View</u></link>', style)
    return Paragraph('-', style)


def _pdf_date(value):
    return pretty_date(value) or '-'


def _room_table(students, styles):
    if not students:
        return Paragraph('No students assigned.', styles['EmptyRoom'])

    cell = ParagraphStyle('PdfCell', parent=styles['Normal'], fontSize=8.5, leading=10.5)
    data = [['Name', 'Contact', 'Sharing', 'Total Rent', 'Paid', 'Balance',
             'Payment Date', 'Mode', 'Cash Amt', 'Paid To', 'Receipt']]
    for s in students:
        cash = s.get('cash_amount')
        data.append([
            _cell(s['name'], cell), s['contact'], f"{s['sharing']} Sharing",
            f"Rs.{s['total_rent']:,.0f}", f"Rs.{s['amount_paid']:,.0f}", f"Rs.{s['balance']:,.0f}",
            _pdf_date(s.get('payment_date')), _cell(s.get('payment_mode'), cell),
            f"Rs.{cash:,.0f}" if cash else '-', _cell(s.get('paid_to'), cell),
            _receipt_cell(s.get('receipt_filename'), cell),
        ])

    table = Table(data, colWidths=[40 * mm, 24 * mm, 19 * mm, 22 * mm, 22 * mm, 22 * mm,
                                   26 * mm, 24 * mm, 20 * mm, 30 * mm, 16 * mm], repeatRows=1)
    style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#DBEAFE')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1E3A8A')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('GRID', (0, 0), (-1, -1), 0.6, colors.HexColor('#B0BEC5')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
    ])
    table.setStyle(style)
    return table


def _payments_recorded_table(db, hostel, styles):
    """Every confirmed payment still stored (since the last Delete All Data) for one hostel."""
    rows = db.execute(
        "SELECT * FROM payment_submissions WHERE status='CONFIRMED' AND hostel=%s "
        "ORDER BY COALESCE(payment_date, SUBSTRING(confirmed_at, 1, 10)), id", (hostel,)).fetchall()
    if not rows:
        return Paragraph('No confirmed payments recorded.', styles['EmptyRoom'])

    cell = ParagraphStyle('PdfCell2', parent=styles['Normal'], fontSize=8.5, leading=10.5)
    data = [['Date', 'Name', 'Room', 'Amount', 'Mode', 'Cash Amt', 'Paid To', 'Receipt / Proof', 'Confirmed By']]
    for r in rows:
        r = dict(r)
        cash = r.get('cash_amount')
        data.append([
            _pdf_date(r.get('payment_date') or (r.get('confirmed_at') or '')[:10]),
            _cell(r['name'], cell), room_label(r['room_number']),
            f"Rs.{r['amount']:,.0f}", _cell(r.get('payment_mode'), cell),
            f"Rs.{cash:,.0f}" if cash else '-', _cell(r.get('paid_to'), cell),
            _receipt_cell(r.get('proof_filename'), cell), _cell(r.get('confirmed_by'), cell),
        ])
    table = Table(data, colWidths=[26 * mm, 45 * mm, 20 * mm, 24 * mm, 26 * mm, 22 * mm, 36 * mm, 24 * mm, 30 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#DCFCE7')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#166534')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('GRID', (0, 0), (-1, -1), 0.6, colors.HexColor('#B0BEC5')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (2, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F0FDF4')]),
    ]))
    return table


def _build_hostel_section(story, styles, hostel_name, rooms_dict, db=None):
    story.append(Paragraph(hostel_name.upper(), styles['HostelHeading']))
    for room_no in rooms_for_hostel(hostel_name):  # includes the Hall for the New Hostel
        students = rooms_dict.get(room_no, [])
        occ = len(students)
        cap = students[0]['sharing'] if students else None
        cap_text = f" ({occ}/{cap} occupied)" if cap else " (0 occupied)"
        title = 'HALL' if room_no == HALL_ROOM_NUMBER else f"ROOM {room_no}"
        room_block = [Paragraph(f"{title}{cap_text}", styles['RoomHeading']),
                      _room_table(students, styles)]
        story.append(KeepTogether(room_block))
        story.append(Spacer(1, 4))
    if db is not None:
        story.append(KeepTogether([
            Paragraph('PAYMENTS RECORDED (since the last Delete All Data)', styles['RoomHeading']),
            _payments_recorded_table(db, hostel_name, styles),
        ]))


def generate_pdf(hostel_filter=None):
    db = get_db()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                             topMargin=16 * mm, bottomMargin=18 * mm,
                             leftMargin=16 * mm, rightMargin=16 * mm)
    styles = _pdf_styles()
    story = []

    if hostel_filter:
        title = f"{hostel_filter.upper()} — STUDENT REPORT"
    else:
        title = "HOSTEL MANAGEMENT REPORT"

    story.append(Paragraph(title, styles['MainTitle']))
    story.append(Paragraph(f"Generated Date: {datetime.now().strftime('%d-%m-%Y')}", styles['SubTitle']))

    stats = get_stats(db)
    if not hostel_filter:
        summary_data = [
            ['Total Students', str(stats['total'])],
            ['Total Rent', f"Rs.{stats['total_rent']:,.0f}"],
            ['Total Paid', f"Rs.{stats['total_paid']:,.0f}"],
            ['Total Balance', f"Rs.{stats['total_balance']:,.0f}"],
        ]
    else:
        rows = db.execute(
            'SELECT COUNT(*) c, COALESCE(SUM(total_rent),0) r, COALESCE(SUM(amount_paid),0) p, COALESCE(SUM(balance),0) b '
            'FROM students WHERE hostel=%s', (hostel_filter,)).fetchone()
        summary_data = [
            ['Total Students', str(rows['c'])],
            ['Total Rent', f"Rs.{rows['r']:,.0f}"],
            ['Total Paid', f"Rs.{rows['p']:,.0f}"],
            ['Total Balance', f"Rs.{rows['b']:,.0f}"],
        ]

    summary_table = Table(summary_data, colWidths=[60 * mm, 60 * mm])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#EFF6FF')),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#1E3A8A')),
        ('GRID', (0, 0), (-1, -1), 0.6, colors.HexColor('#B0BEC5')),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 10))

    if hostel_filter:
        rooms_structure = get_rooms_structure(db, hostel=hostel_filter)
        _build_hostel_section(story, styles, hostel_filter, rooms_structure[hostel_filter], db)
    else:
        rooms_structure = get_rooms_structure(db)
        for h in HOSTELS:
            _build_hostel_section(story, styles, h, rooms_structure[h], db)
            if h != HOSTELS[-1]:
                story.append(PageBreak())

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return buf


@app.route('/admin/pdf/old')
@login_required
def pdf_old():
    buf = generate_pdf(hostel_filter='Old Hostel')
    return send_file(buf, as_attachment=True, download_name='old_hostel_report.pdf', mimetype='application/pdf')


@app.route('/admin/pdf/new')
@login_required
def pdf_new():
    buf = generate_pdf(hostel_filter='New Hostel')
    return send_file(buf, as_attachment=True, download_name='new_hostel_report.pdf', mimetype='application/pdf')


@app.route('/admin/pdf/complete')
@login_required
def pdf_complete():
    buf = generate_pdf(hostel_filter=None)
    return send_file(buf, as_attachment=True, download_name='complete_hostel_report.pdf', mimetype='application/pdf')


# --------------------------------------------------------------------------
# JSON API (used by client-side search/filter for instant results)
# --------------------------------------------------------------------------

@app.route('/admin/api/students')
@login_required
def api_students():
    db = get_db()
    rows = db.execute('SELECT * FROM students ORDER BY LOWER(name)').fetchall()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------
# ERROR HANDLERS
# --------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, message='Page not found.'), 404


@app.errorhandler(413)
def too_large(e):
    return render_template('error.html', code=413, message='The uploaded file is too large. Maximum size is 8 MB.'), 413


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500, message='Something went wrong on our end. Please try again.'), 500


@app.errorhandler(400)
def bad_request(e):
    return render_template('error.html', code=400, message='Invalid request.'), 400


# --------------------------------------------------------------------------
# ENTRYPOINT
# --------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)