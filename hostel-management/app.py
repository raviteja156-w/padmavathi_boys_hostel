"""
Hostel Management System
A complete, production-ready Flask application for managing hostel students.
"""
import os
import re
import io
import base64
import calendar
import secrets
import uuid
from datetime import datetime, timedelta, date
from functools import wraps

import psycopg2
import psycopg2.extras
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


# Due-date tracking and the monthly payment reset start on this date (12:00 AM IST).
try:
    LAUNCH_DATE = datetime.strptime(os.environ.get('LAUNCH_DATE', '2026-10-01').strip(), '%Y-%m-%d').date()
except ValueError:
    LAUNCH_DATE = date(2026, 10, 1)


def previous_month_label(d):
    """'YYYY-MM' label of the month before the month that date d falls in."""
    return (d.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')

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


def get_db():
    if 'db' not in g:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        g.db = DBWrapper(conn)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.conn.close()


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
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

    # The billing cycle the students' payment fields currently belong to. Starts as the month
    # before launch, so the very first request on/after LAUNCH_DATE archives + resets them.
    cur.execute('''
        INSERT INTO app_settings (key, value) VALUES ('billing_cycle', %s)
        ON CONFLICT (key) DO NOTHING
    ''', (previous_month_label(LAUNCH_DATE),))

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

    # Which month a payment counts in for INCOME reports when the admin overrides it ('YYYY-MM'); NULL = by payment date.
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
    conn.commit()
    cur.close()
    conn.close()


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
    # If the month changed since they were deleted, their old payment details no longer apply.
    same_month = row['deleted_cycle'] == current_cycle()
    rent = row['total_rent'] or 0
    paid = (row['amount_paid'] or 0) if same_month else 0
    balance = max(round(rent - paid, 2), 0)
    now_iso = datetime.utcnow().isoformat()
    new = db.execute('''
        INSERT INTO students
        (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance, photo_filename, note,
         due_date, date_of_join, payment_date, payment_mode, cash_amount, paid_to, receipt_filename,
         created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    ''', (row['hostel'], row['room_number'], row['sharing'], row['name'], row['contact'] or '',
          rent, paid, balance, row['photo_filename'], row['note'] or '', row['due_date'], row['date_of_join'],
          row['payment_date'] if same_month else None, row['payment_mode'] if same_month else None,
          row['cash_amount'] if same_month else None, row['paid_to'] if same_month else None,
          row['receipt_filename'] if same_month else None,
          row['created_at'] or now_iso, now_iso)).fetchone()
    # Re-attach this student's old payment notifications (they were detached when the student was deleted).
    db.execute('''UPDATE payment_submissions SET student_id=%s
                  WHERE student_id IS NULL AND name=%s AND hostel=%s AND room_number=%s''',
               (new['id'], row['name'], row['hostel'], row['room_number']))
    db.execute('DELETE FROM deleted_students WHERE id=%s', (deleted_id,))
    db.commit()
    return True, f"{row['name']} was restored to {row['hostel']} • {room_label(row['room_number'])}.", row


def _log_payment_delete(db, student_id, name, hostel, room_number, amount, detail):
    db.execute('''INSERT INTO payment_delete_log (student_id, name, hostel, room_number, amount, detail, deleted_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)''',
               (student_id, name, hostel, room_number, amount, detail, now_ist().isoformat()))


def do_clear_student_payment(db, student_id):
    """Deletes a student's payment details for this month: Total Paid -> 0, full rent is the balance, and
    their confirmed payment notifications of this month are removed. Returns (ok, name, amount_removed)."""
    st = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not st:
        return False, None, 0
    amount = st['amount_paid'] or 0
    month_start = today_ist().replace(day=1).isoformat()
    _log_payment_delete(db, st['id'], st['name'], st['hostel'], st['room_number'], amount,
                        f"Payment deleted (mode: {st.get('payment_mode') or '-'}, paid to: {st.get('paid_to') or '-'})")
    db.execute('''UPDATE students SET amount_paid=0, balance=total_rent, payment_date=NULL, payment_mode=NULL,
                  cash_amount=NULL, paid_to=NULL, receipt_filename=NULL, income_month=NULL, updated_at=%s WHERE id=%s''',
               (datetime.utcnow().isoformat(), student_id))
    db.execute('''DELETE FROM payment_submissions WHERE student_id=%s AND status='CONFIRMED'
                  AND COALESCE(confirmed_at, submitted_at) >= %s''', (student_id, month_start))
    db.execute('DELETE FROM month_payment_dismissals WHERE student_id=%s', (student_id,))
    db.commit()
    return True, st['name'], amount


def reset_counts(db):
    r = db.execute('SELECT COUNT(*) AS n, COALESCE(SUM(amount_paid),0) AS amt FROM students WHERE amount_paid > 0').fetchone()
    p = db.execute("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END),0) AS pend FROM payment_submissions").fetchone()
    return {'students': r['n'] or 0, 'amount': r['amt'] or 0, 'records': p['n'] or 0, 'pending': p['pend'] or 0}


def do_reset_all_payments(db):
    """Deletes ALL payment details: every student's Paid goes to 0 (balance = full rent) and every
    payment record / receipt that students uploaded is removed. Returns the counts that were deleted."""
    counts = reset_counts(db)
    files = {r['proof_filename'] for r in db.execute('SELECT proof_filename FROM payment_submissions WHERE proof_filename IS NOT NULL').fetchall()}
    files |= {r['receipt_filename'] for r in db.execute('SELECT receipt_filename FROM students WHERE receipt_filename IS NOT NULL').fetchall()}
    _log_payment_delete(db, None, '[ALL STUDENTS]', None, None, counts['amount'],
                        f"ALL payment details deleted: {counts['students']} student(s) back to Paid 0, {counts['records']} payment record(s) removed")
    db.execute('''UPDATE students SET amount_paid=0, balance=total_rent, payment_date=NULL, payment_mode=NULL,
                  cash_amount=NULL, paid_to=NULL, receipt_filename=NULL, income_month=NULL, updated_at=%s''', (datetime.utcnow().isoformat(),))
    db.execute('DELETE FROM payment_submissions')
    db.execute('DELETE FROM month_payment_dismissals')
    db.commit()
    for f in files:                      # best effort: remove the uploaded receipt images too
        try:
            delete_photo(f)
        except Exception:
            pass
    return counts


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
    """Dashboard cards only. Same as get_stats, except Total Paid / Balance Due count ONLY payments the
    admin has confirmed this month (payment_submissions.status = 'CONFIRMED'). Anything merely marked
    as paid on a student record, or still pending, is ignored. Excel/PDF keep using get_stats."""
    stats = get_stats(db)
    month_start = today_ist().replace(day=1).isoformat()
    row = db.execute('''
        SELECT COALESCE(SUM(COALESCE(c.confirmed, 0)), 0) AS total_paid,
               COALESCE(SUM(GREATEST(COALESCE(s.total_rent, 0) - COALESCE(c.confirmed, 0), 0)), 0) AS total_balance
        FROM students s
        LEFT JOIN (
            SELECT student_id, SUM(amount) AS confirmed
            FROM payment_submissions
            WHERE status = 'CONFIRMED' AND confirmed_at >= %s
            GROUP BY student_id
        ) c ON c.student_id = s.id
    ''', (month_start,)).fetchone()
    stats['total_paid'] = row['total_paid'] or 0
    stats['total_balance'] = row['total_balance'] or 0
    return stats


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


def next_month_label():
    today = today_ist()
    month, year = today.month + 1, today.year
    if month > 12:
        month, year = 1, year + 1
    return f'{calendar.month_name[month]} {year}'


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
# DUE LIST LOGIC + MONTHLY RESET
# --------------------------------------------------------------------------

def effective_due_day(student, year, month):
    """Day of the month rent is due: the admin-set due_date if present, otherwise the day of
    the student's join date. Capped to the month's length (join day 31 -> 30 in April, etc.)."""
    day = student.get('due_date')
    if not day:
        joined = parse_iso_date(student.get('date_of_join'))
        if not joined:
            return None
        day = joined.day
    return min(int(day), calendar.monthrange(year, month)[1])


EARLY_PAY_DAYS = 2   # a payment counts for a due date from 2 days BEFORE that date (set by the admin)
UPCOMING_DAYS = 7    # "Coming up" shows rent falling due in the next 7 days


def _billing_period_start(db):
    """First day of the month whose payments are still stored on the students (changes only when the admin
    answers Yes to the month-end question)."""
    row = db.execute("SELECT value FROM app_settings WHERE key = 'billing_cycle'").fetchone()
    try:
        return datetime.strptime(row['value'] + '-01', '%Y-%m-%d').date()
    except (TypeError, ValueError, KeyError):
        return today_ist().replace(day=1)


def build_payment_events(db):
    """Every payment the app knows about, each with its own DATE and an identity (kind + ref_id) so it can be
    edited: 'history' (a saved month), 'submission' (a confirmed student payment), 'record' (what the admin
    recorded on the student), 'deleted' (a deleted student). month_key is the month it counts in for income:
    the admin's override if set, otherwise the month of the payment date. Read only."""
    today = today_ist()
    period_start = _billing_period_start(db)
    active = {r['id']: dict(r) for r in db.execute('SELECT * FROM students').fetchall()}
    events = []

    for r in db.execute('SELECT * FROM payment_history WHERE amount_paid > 0').fetchall():
        d_ = (parse_iso_date(r['payment_date']) or parse_iso_date((r['billing_month'] or '') + '-01') or period_start)
        events.append({'kind': 'history', 'ref_id': r['id'], 'income_month': r.get('income_month'), 'base': 0,
                       'student_id': r['student_id'], 'name': r['name'], 'hostel': r['hostel'], 'room_number': r['room_number'],
                       'date': d_, 'amount': r['amount_paid'] or 0, 'mode': r['payment_mode'], 'paid_to': r['paid_to'],
                       'cash': r['cash_amount'], 'deleted': r['student_id'] not in active, 'source': 'history'})

    subs = {}
    for r in db.execute("""SELECT * FROM payment_submissions WHERE status='CONFIRMED' AND student_id IS NOT NULL
                           AND COALESCE(confirmed_at, submitted_at) >= %s""", (period_start.isoformat(),)).fetchall():
        subs.setdefault(r['student_id'], []).append(dict(r))
    for sid, st in active.items():
        paid = st['amount_paid'] or 0
        if paid <= 0:
            continue
        base = {'student_id': sid, 'name': st['name'], 'hostel': st['hostel'], 'room_number': st['room_number'],
                'deleted': False, 'source': 'current'}
        own = subs.get(sid, [])
        sub_total = sum(x['amount'] or 0 for x in own)
        if own and sub_total <= paid + 0.005:
            for x in own:
                d_ = (parse_iso_date(x.get('payment_date')) or parse_iso_date((x.get('confirmed_at') or x.get('submitted_at') or '')[:10])
                      or period_start)
                events.append(dict(base, kind='submission', ref_id=x['id'], income_month=x.get('income_month'), base_amount=0,
                                   date=d_, amount=x['amount'] or 0, mode=x.get('payment_mode') or 'Online',
                                   paid_to=x.get('paid_to'), cash=x.get('cash_amount')))
            rest = round(paid - sub_total, 2)
            if rest > 0.005:
                events.append(dict(base, kind='record', ref_id=sid, income_month=st.get('income_month'), base_amount=sub_total,
                                   date=parse_iso_date(st.get('payment_date')) or period_start, amount=rest,
                                   mode=st.get('payment_mode'), paid_to=st.get('paid_to'), cash=None))
        else:
            events.append(dict(base, kind='record', ref_id=sid, income_month=st.get('income_month'), base_amount=0,
                               date=parse_iso_date(st.get('payment_date')) or period_start, amount=paid,
                               mode=st.get('payment_mode'), paid_to=st.get('paid_to'), cash=st.get('cash_amount')))

    for r in db.execute('SELECT * FROM deleted_students WHERE amount_paid > 0').fetchall():
        d_ = parse_iso_date(r['payment_date']) or parse_iso_date((r['deleted_at'] or '')[:10]) or today
        events.append({'kind': 'deleted', 'ref_id': r['id'], 'income_month': None, 'base': 0,
                       'student_id': None, 'name': r['name'], 'hostel': r['hostel'], 'room_number': r['room_number'],
                       'date': d_, 'amount': r['amount_paid'] or 0, 'mode': r['payment_mode'], 'paid_to': r['paid_to'],
                       'cash': r['cash_amount'], 'deleted': True, 'source': 'deleted'})
    for e in events:
        e.setdefault('base_amount', 0)
        e['month_key'] = e.get('income_month') or e['date'].strftime('%Y-%m')
        e['overridden'] = bool(e.get('income_month')) and e['income_month'] != e['date'].strftime('%Y-%m')
    return events


def _due_cycles(raw_day, start, end):
    """The student's due dates between start and end: the same day every month (31 becomes 30 or 28 in short months)."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d_ = date(y, m, min(raw_day, calendar.monthrange(y, m)[1]))
        if start <= d_ <= end:
            out.append(d_)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def evaluate_student_due(student, events, today):
    """THE RULE. Rent falls due on the student's due day every month (the day of their join date, or the due
    day the admin set). Payments are matched to due dates in order, oldest first, and a payment can only pay a
    due date that is at most 2 days before it. So a payment made 8 days late belongs to the OLD due date, and
    the next due date still comes up on its own day. Returns None when the student has no due day on record,
    else {'due': [(date, amount)...], 'upcoming': [(date, amount)...]}."""
    joined = parse_iso_date(student.get('date_of_join'))
    raw = student.get('due_date') or (joined.day if joined else None)
    if not raw:
        return None
    rent = float(student.get('total_rent') or 0)
    start = max(LAUNCH_DATE, joined or LAUNCH_DATE)
    end = today + timedelta(days=UPCOMING_DAYS)
    # A payment dated in the future does not count yet (protects against a wrongly typed date hiding a due student).
    pool = sorted([[e['date'], float(e['amount'] or 0)] for e in events if e['date'] <= today], key=lambda x: x[0])
    due, upcoming = [], []
    for d_ in _due_cycles(int(raw), start, end):
        need = rent
        for ev in pool:
            if need <= 0.005:
                break
            if ev[1] <= 0.005 or ev[0] < d_ - timedelta(days=EARLY_PAY_DAYS):
                continue
            take = min(ev[1], need)
            ev[1] -= take
            need -= take
        if need > 0.005:
            (due if d_ <= today else upcoming).append((d_, round(need, 2)))
    return {'due': due, 'upcoming': upcoming}


def get_due_and_upcoming(db):
    """(due_now, coming_up_in_7_days). Based on due dates and payment DATES, not on the calendar-month balance."""
    today = today_ist()
    if today < LAUNCH_DATE:
        return [], []
    by_student = {}
    for e in build_payment_events(db):
        if e['student_id'] is not None and not e['deleted']:
            by_student.setdefault(e['student_id'], []).append(e)
    due_list, up_list = [], []
    for row in db.execute('SELECT * FROM students').fetchall():
        s = dict(row)
        joined = parse_iso_date(s.get('date_of_join'))
        if joined and joined > today:
            continue
        res = evaluate_student_due(s, by_student.get(s['id'], []), today)
        if res is None:
            # No join date and no due date on record: fall back to the plain balance so nobody is hidden.
            if (s.get('balance') or 0) > 0:
                s.update(no_due_date=True, due_day=1, due_on=today.replace(day=1).isoformat(), days_overdue=today.day - 1,
                         due_amount=s['balance'], cycles_due=1)
                due_list.append(s)
            continue
        s['no_due_date'] = False
        if res['due']:
            first = res['due'][0][0]
            s.update(due_day=first.day, due_on=first.isoformat(), days_overdue=(today - first).days,
                     due_amount=round(sum(a for _, a in res['due']), 2), cycles_due=len(res['due']))
            due_list.append(s)
        elif res['upcoming']:
            nxt = res['upcoming'][0][0]
            s.update(due_day=nxt.day, due_on=nxt.isoformat(), days_until=(nxt - today).days, due_amount=res['upcoming'][0][1])
            up_list.append(s)
    due_list.sort(key=lambda x: (-x['days_overdue'], (x['name'] or '').lower()))
    up_list.sort(key=lambda x: (x['days_until'], (x['name'] or '').lower()))
    return due_list, up_list


def get_due_students(db):
    return get_due_and_upcoming(db)[0]


def get_upcoming_due_students(db, days=7):
    return get_due_and_upcoming(db)[1]


def _income_stats(rows):
    total = round(sum(e['amount'] for e in rows), 2)
    cash = 0.0
    for e in rows:
        if e['mode'] == 'Cash':
            cash += e['amount']
        elif e['mode'] == 'Online + Cash':
            cash += min(e['cash'] or 0, e['amount'])
    return {'rows': rows, 'total': total, 'count': len(rows), 'payers': len({(e['name'], e['room_number']) for e in rows}),
            'cash': round(cash, 2), 'online': round(total - cash, 2)}


def income_month_keys():
    today = today_ist()
    return today.strftime('%Y-%m'), (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


def month_income_summary(db, which='this'):
    """Money RECEIVED in a calendar month: each payment counts in the month of its payment date, unless the admin
    moved it to this/last month. Includes payments of deleted students."""
    this_key, last_key = income_month_keys()
    key = last_key if which == 'last' else this_key
    rows = [e for e in build_payment_events(db) if e['month_key'] == key]
    rows.sort(key=lambda e: (e['date'], e['name'] or ''), reverse=True)
    out = _income_stats(rows)
    out.update({'key': key, 'label': cycle_label(key)})
    return out


def current_cycle():
    return today_ist().strftime('%Y-%m')


def cycle_label(cycle):
    """'2026-10' -> 'October 2026'."""
    try:
        return datetime.strptime(cycle, '%Y-%m').strftime('%B %Y')
    except (TypeError, ValueError):
        return cycle or ''


def pending_rollover_cycle(db):
    """The month whose payment details are still on the students although a new month has started
    (from 12:00 AM on the 1st, IST). None when nothing is waiting. NOTHING is deleted automatically:
    the admin is asked every time the admin area is opened until they say yes."""
    today = today_ist()
    if today < LAUNCH_DATE:
        return None
    row = db.execute("SELECT value FROM app_settings WHERE key = 'billing_cycle'").fetchone()
    if row is None:
        # First run: same starting point the old automatic reset used.
        db.execute("INSERT INTO app_settings (key, value) VALUES ('billing_cycle', %s) ON CONFLICT (key) DO NOTHING",
                   (previous_month_label(LAUNCH_DATE),))
        db.commit()
        row = db.execute("SELECT value FROM app_settings WHERE key = 'billing_cycle'").fetchone()
    closing = row['value']
    return closing if closing < current_cycle() else None


def perform_monthly_rollover():
    """Only called after the admin answers YES. Copies every student's payment details into
    payment_history, then blanks them (amount paid, payment date, mode, receipt, cash amount,
    paid to, note) and moves the billing cycle to the current month. Safe with several workers:
    the billing_cycle row is locked. Returns the closed month label, or None if nothing to do."""
    cycle = current_cycle()
    conn = psycopg2.connect(DATABASE_URL)
    closed = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'billing_cycle' FOR UPDATE")
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        closing_cycle = row[0]
        if closing_cycle < cycle:
            now_iso = datetime.utcnow().isoformat()
            cur.execute('''
                INSERT INTO payment_history
                (student_id, hostel, room_number, name, contact, billing_month, total_rent, amount_paid, balance,
                 payment_date, payment_mode, cash_amount, paid_to, receipt_filename, note, archived_at, income_month)
                SELECT id, hostel, room_number, name, contact, %s, total_rent, amount_paid, balance,
                       payment_date, payment_mode, cash_amount, paid_to, receipt_filename, note, %s, income_month
                FROM students
            ''', (closing_cycle, now_iso))
            cur.execute('''
                UPDATE students
                SET amount_paid = 0, balance = total_rent, payment_date = NULL, payment_mode = NULL,
                    cash_amount = NULL, paid_to = NULL, receipt_filename = NULL, note = '', income_month = NULL, updated_at = %s
            ''', (now_iso,))
            cur.execute("UPDATE app_settings SET value = %s WHERE key = 'billing_cycle'", (cycle,))
            closed = cycle_label(closing_cycle)
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception('Monthly payment rollover failed')
        raise
    finally:
        conn.close()
    return closed


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
    conn = psycopg2.connect(DATABASE_URL)
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
        conn.close()


init_db()
seed_october_deleted_students()


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
# PAID WITHOUT ADMIN CONFIRMATION
# A student counts here when their recorded "amount paid" this month is more than the payments the
# admin has actually confirmed for them (e.g. the amount was typed in on Add/Edit Student or loaded
# from a register, instead of coming through a student submission that the admin confirmed).
# --------------------------------------------------------------------------

def get_unconfirmed_paid(db):
    month_start = today_ist().replace(day=1).isoformat()
    rows = db.execute('''
        SELECT s.*, COALESCE(c.confirmed_total, 0) AS confirmed_total
        FROM students s
        LEFT JOIN (
            SELECT student_id, SUM(amount) AS confirmed_total
            FROM payment_submissions
            WHERE status = 'CONFIRMED' AND confirmed_at >= %s
            GROUP BY student_id
        ) c ON c.student_id = s.id
        WHERE s.amount_paid > 0
        ORDER BY s.hostel, s.room_number, LOWER(s.name)
    ''', (month_start,)).fetchall()
    dismissed = {
        r['student_id']: r['amount_paid']
        for r in db.execute('SELECT student_id, amount_paid FROM unconfirmed_dismissals WHERE month_start=%s', (month_start,)).fetchall()
    }
    result = []
    for row in rows:
        d = dict(row)
        # A dismissed notice stays hidden unless the student's marked-paid amount changes afterwards.
        if d['id'] in dismissed and abs((d['amount_paid'] or 0) - dismissed[d['id']]) < 0.005:
            continue
        d['unconfirmed_amount'] = round((d['amount_paid'] or 0) - (d['confirmed_total'] or 0), 2)
        if d['unconfirmed_amount'] > 0.005:
            result.append(d)
    return result


@app.route('/admin/paid-unconfirmed')
@login_required
def paid_unconfirmed():
    db = get_db()
    students = get_unconfirmed_paid(db)
    totals = {
        'count': len(students),
        'paid': sum(s['amount_paid'] or 0 for s in students),
        'unconfirmed': sum(s['unconfirmed_amount'] for s in students),
    }
    return render_template('unconfirmed_paid.html', students=students, totals=totals)


@app.route('/admin/paid-unconfirmed/<int:student_id>/dismiss', methods=['POST'])
@login_required
def dismiss_unconfirmed(student_id):
    """Removes one notice from the Paid-Not-Confirmed page only. The student, payments, PDF and Excel data are untouched."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('paid_unconfirmed'))
    db = get_db()
    row = db.execute('SELECT name, amount_paid FROM students WHERE id=%s', (student_id,)).fetchone()
    if not row:
        flash('That notification no longer exists.', 'error')
        return redirect(url_for('paid_unconfirmed'))
    month_start = today_ist().replace(day=1).isoformat()
    db.execute('''
        INSERT INTO unconfirmed_dismissals (student_id, month_start, amount_paid, dismissed_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (student_id, month_start)
        DO UPDATE SET amount_paid = EXCLUDED.amount_paid, dismissed_at = EXCLUDED.dismissed_at
    ''', (student_id, month_start, row['amount_paid'] or 0, datetime.utcnow().isoformat()))
    db.commit()
    flash(f"Notification for {row['name']} removed.", 'success')
    return redirect(url_for('paid_unconfirmed'))


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

    due_count = len(get_due_students(db))
    unconfirmed_paid_count = len(get_unconfirmed_paid(db))
    pending_row = db.execute("SELECT COUNT(*) AS c FROM payment_submissions WHERE status='PENDING'").fetchone()

    return render_template(
        'admin.html',
        stats=stats,
        rooms_structure=rooms_structure,
        hostels=HOSTELS,
        rooms=ROOMS,
        all_students=all_students,
        due_count=due_count,
        pending_payments_count=pending_row['c'] or 0,
        unconfirmed_paid_count=unconfirmed_paid_count,
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
    if not (session.get('admin_logged_in') and request.path.startswith('/admin')):
        return {}
    counts = {'pending': 0, 'pauses': 0, 'deleted': 0, 'guests': 0}
    month_end = None
    try:
        db = get_db()
        counts['deleted'] = db.execute('SELECT COUNT(*) AS c FROM deleted_students').fetchone()['c'] or 0
        counts['guests'] = db.execute("SELECT COUNT(*) AS c FROM guests WHERE status='STAYING'").fetchone()['c'] or 0
        closing = pending_rollover_cycle(db)
        if closing:
            month_end = {'closing': closing, 'label': cycle_label(closing), 'now_label': cycle_label(current_cycle())}
        counts['pending'] = db.execute("SELECT COUNT(*) AS c FROM payment_submissions WHERE status='PENDING'").fetchone()['c'] or 0
        counts['pauses'] = db.execute('SELECT COUNT(*) AS c FROM paused_beds WHERE end_date > %s',
                                      (today_ist().isoformat(),)).fetchone()['c'] or 0
    except Exception:
        try:
            get_db().conn.rollback()
        except Exception:
            pass
    return {'admin_nav': counts, 'month_end': month_end}


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
    'month_income': month_income_summary,
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
        tracking_started=(today >= LAUNCH_DATE),
        launch_date=LAUNCH_DATE.isoformat(),
    )


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
        for_month=next_month_label(),
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
          amount_val, next_month_label(), proof_url, note, now,
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

    # Reflect the confirmed payment against the student's running balance.
    if payment['student_id']:
        student = db.execute('SELECT * FROM students WHERE id=%s', (payment['student_id'],)).fetchone()
        if student:
            new_paid = (student['amount_paid'] or 0) + payment['amount']
            new_balance = max(round((student['total_rent'] or 0) - new_paid, 2), 0)
            old_mode = student.get('payment_mode')
            paid_mode = payment.get('payment_mode') or 'Online'
            new_mode = paid_mode if not old_mode or old_mode == paid_mode else 'Online + Cash'
            if paid_mode == 'Cash':
                cash_part = payment['amount']
            elif paid_mode == 'Online + Cash':
                cash_part = payment.get('cash_amount') or 0
            else:
                cash_part = 0
            new_cash = (student.get('cash_amount') or 0) + cash_part if cash_part else student.get('cash_amount')
            new_receipt = payment['proof_filename'] or student.get('receipt_filename')
            new_paid_to = payment.get('paid_to') or student.get('paid_to')
            new_pay_date = payment.get('payment_date') or today_ist().isoformat()
            db.execute('''
                UPDATE students
                SET amount_paid=%s, balance=%s, payment_date=%s, payment_mode=%s, cash_amount=%s, paid_to=%s,
                    receipt_filename=%s, updated_at=%s
                WHERE id=%s
            ''', (new_paid, new_balance, new_pay_date, new_mode, new_cash, new_paid_to, new_receipt, now, payment['student_id']))

    db.commit()
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
    return redirect(url_for('month_payments', i=idx, view=view))


@app.route('/admin/reset-payments')
@login_required
def reset_payments():
    return render_template('reset_payments.html', counts=reset_counts(get_db()))


@app.route('/admin/reset-payments/confirm', methods=['POST'])
@login_required
def reset_payments_confirm():
    """Deletes ALL payment details. Only runs when the admin typed DELETE."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('reset_payments'))
    if (request.form.get('confirm_text') or '').strip().upper() != 'DELETE':
        flash('Nothing was deleted. Type DELETE in the box to confirm.', 'error')
        return redirect(url_for('reset_payments'))
    try:
        counts = do_reset_all_payments(get_db())
    except Exception:
        app.logger.exception('Reset all payments failed')
        get_db().conn.rollback()
        flash('Could not delete the payment details. Nothing was changed. Please try again.', 'error')
        return redirect(url_for('reset_payments'))
    flash(f"All payment details deleted: {counts['students']} student(s) are now Paid ₹0 "
          f"(₹{counts['amount']:,.0f} removed) and {counts['records']} payment record(s) were removed.", 'success')
    return redirect(url_for('admin_dashboard'))


# --------------------------------------------------------------------------
# MONTH END: "Can I delete last month's payment details?"
# --------------------------------------------------------------------------

@app.route('/admin/month-end/confirm', methods=['POST'])
@login_required
def month_end_confirm():
    """Runs the monthly reset, but ONLY when the admin presses Yes."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('admin_dashboard'))
    try:
        closed = perform_monthly_rollover()
    except Exception:
        flash('Could not delete last month\'s payment details. Nothing was changed. Please try again.', 'error')
        return redirect(url_for('admin_dashboard'))
    if closed:
        flash(f"{closed} payment details were deleted (a copy is saved in payment history). New month started.", 'success')
    else:
        flash('Nothing to delete: the new month was already started.', 'success')
    return redirect(url_for('admin_dashboard'))


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
                date_of_join=%s, updated_at=%s
            WHERE id=%s
        ''', (hostel, room_number, sharing, name, clean_contact, total_rent, balance, date_of_join, now, student_id))
        db.commit()

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
    """Returns (active, deleted) lists of this month's payments."""
    month_start = today_ist().replace(day=1).isoformat()
    rows = db.execute('''
        SELECT * FROM students WHERE amount_paid > 0
        ORDER BY hostel, room_number, LOWER(name)
    ''').fetchall()
    dismissed = {
        r['student_id']: r['amount_paid']
        for r in db.execute('SELECT student_id, amount_paid FROM month_payment_dismissals WHERE month_start=%s',
                            (month_start,)).fetchall()
    }
    active, deleted = [], []
    for row in rows:
        item = _month_payment_dict(row)
        if row['id'] in dismissed and abs((row['amount_paid'] or 0) - dismissed[row['id']]) < 0.005:
            deleted.append(item)
        else:
            active.append(item)
    return active, deleted


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
                           start_index=start_index, totals=totals,
                           income_this=month_income_summary(db, 'this'))


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
    rent = each student's monthly rent; paid = payments the admin CONFIRMED this calendar month;
    due = rent minus confirmed payments (never below 0)."""
    title, kind, only_hostel = CALC_CARDS[card]
    month_start = today_ist().replace(day=1).isoformat()
    confirmed = {r['student_id']: (r['c'] or 0) for r in db.execute(
        """SELECT student_id, SUM(amount) AS c FROM payment_submissions
           WHERE status='CONFIRMED' AND confirmed_at >= %s AND student_id IS NOT NULL GROUP BY student_id""",
        (month_start,)).fetchall()}
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
                if (st['amount_paid'] or 0) > conf + 0.005:
                    not_counted.append({'name': st['name'], 'place': f"{st['hostel']} {room_label(st['room_number'])}",
                                        'record': st['amount_paid'] or 0, 'confirmed': conf, 'diff': round((st['amount_paid'] or 0) - conf, 2)})
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
            'not_counted': not_counted, 'month_label': today_ist().strftime('%B %Y'), 'extra': extra,
            'student_count': sum(len(r['students']) for g in groups for r in g['rooms'])}


@app.route('/admin/students/<int:student_id>/payment-details')
@login_required
def student_payment_details(student_id):
    """What a student paid: the payment photo(s) if there are any, otherwise the details the student submitted
    (and, when nothing was submitted, what the admin recorded on the student's record). Read only."""
    db = get_db()
    row = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not row:
        abort(404)
    st = dict(row)
    subs = [dict(r) for r in db.execute(
        'SELECT * FROM payment_submissions WHERE student_id=%s ORDER BY id DESC LIMIT 30', (student_id,)).fetchall()]
    month_start = today_ist().replace(day=1).isoformat()
    confirmed = sum((x['amount'] or 0) for x in subs
                    if x['status'] == 'CONFIRMED' and (x.get('confirmed_at') or '') >= month_start)
    return render_template('student_payment_details.html', st=st, subs=subs, confirmed=confirmed,
                           month_label=today_ist().strftime('%B %Y'))


@app.route('/admin/calculation/<card>')
@login_required
def calculation(card):
    if card not in CALC_CARDS:
        abort(404)
    return render_template('calculation.html', c=build_calculation(get_db(), card))


@app.route('/admin/income')
@login_required
def income():
    """All payments of a month (by the month they count in) or of one student, with edit / delete for each."""
    db = get_db()
    sid = request.args.get('student', type=int)
    if sid:
        row = db.execute('SELECT * FROM students WHERE id=%s', (sid,)).fetchone()
        if not row:
            abort(404)
        evs = [e for e in build_payment_events(db) if e['student_id'] == sid and not e['deleted']]
        evs.sort(key=lambda e: (e['date'], e['name'] or ''), reverse=True)
        inc = _income_stats(evs)
        inc.update({'label': f"All payments of {row['name']}", 'key': None})
        return render_template('income.html', inc=inc, which='student', student=dict(row), due_total=0, due_count=0,
                               rent_roll=0, last_has_data=True)
    which = 'last' if request.args.get('m') == 'last' else 'this'
    inc = month_income_summary(db, which)
    due_now = get_due_students(db) if which == 'this' else []
    roll = db.execute('SELECT COALESCE(SUM(total_rent),0) AS r FROM students').fetchone()['r'] or 0
    return render_template('income.html', inc=inc, which=which, student=None, due_total=sum(x['due_amount'] for x in due_now),
                           due_count=len(due_now), rent_roll=roll,
                           last_has_data=bool(month_income_summary(db, 'last')['rows']))


# ---- edit / delete ONE payment ----------------------------------------------------------------

def _find_payment_event(db, kind, ref_id):
    for e in build_payment_events(db):
        if e['kind'] == kind and e['ref_id'] == ref_id:
            return e
    return None


def _shift_student_paid(db, student_id, delta):
    """Adds delta (can be negative) to a student's Paid and recalculates the balance. Returns the new Paid."""
    st = db.execute('SELECT * FROM students WHERE id=%s', (student_id,)).fetchone()
    if not st:
        return None
    new_paid = max(round((st['amount_paid'] or 0) + delta, 2), 0)
    new_bal = max(round((st['total_rent'] or 0) - new_paid, 2), 0)
    db.execute('UPDATE students SET amount_paid=%s, balance=%s, updated_at=%s WHERE id=%s',
               (new_paid, new_bal, datetime.utcnow().isoformat(), student_id))
    return new_paid


def _clear_student_payment_fields(db, student_id):
    db.execute('''UPDATE students SET amount_paid=0, balance=total_rent, payment_date=NULL, payment_mode=NULL,
                  cash_amount=NULL, paid_to=NULL, receipt_filename=NULL, income_month=NULL, updated_at=%s WHERE id=%s''',
               (datetime.utcnow().isoformat(), student_id))


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
    counted = form.get('counted_in') or 'auto'
    this_key, last_key = income_month_keys()
    income_month = {'this': this_key, 'last': last_key}.get(counted)
    return errors, {'amount': amount, 'date': pdate, 'mode': mode, 'cash': cash, 'paid_to': paid_to, 'income_month': income_month}


def _after_payment_redirect(form):
    if form.get('student'):
        return redirect(url_for('income', student=form.get('student', type=int)))
    return redirect(url_for('income', m='last' if form.get('m') == 'last' else 'this'))


@app.route('/admin/payments/edit/<kind>/<int:ref_id>', methods=['GET', 'POST'])
@login_required
def edit_payment(kind, ref_id):
    if kind not in ('submission', 'record', 'history'):
        flash('Payments of deleted students can only be changed after you restore the student.', 'error')
        return redirect(url_for('income'))
    db = get_db()
    ev = _find_payment_event(db, kind, ref_id)
    if not ev:
        flash('That payment was not found.', 'error')
        return redirect(url_for('income'))
    this_key, last_key = income_month_keys()
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Security check failed. Please try again.', 'error')
            return redirect(url_for('edit_payment', kind=kind, ref_id=ref_id))
        errors, v = _parse_payment_edit(request.form)
        if errors:
            for e_ in errors:
                flash(e_, 'error')
            return redirect(url_for('edit_payment', kind=kind, ref_id=ref_id, student=request.form.get('student'), m=request.form.get('m')))
        now_iso = datetime.utcnow().isoformat()
        if kind == 'submission':
            sub = db.execute('SELECT * FROM payment_submissions WHERE id=%s', (ref_id,)).fetchone()
            db.execute('''UPDATE payment_submissions SET amount=%s, payment_date=%s, payment_mode=%s, cash_amount=%s,
                          paid_to=%s, income_month=%s WHERE id=%s''',
                       (v['amount'], v['date'].isoformat(), v['mode'], v['cash'], v['paid_to'], v['income_month'], ref_id))
            if sub['status'] == 'CONFIRMED' and sub['student_id']:
                delta = v['amount'] - (sub['amount'] or 0)
                if abs(delta) > 0.004:
                    _shift_student_paid(db, sub['student_id'], delta)
        elif kind == 'record':
            st = db.execute('SELECT * FROM students WHERE id=%s', (ref_id,)).fetchone()
            new_paid = round((ev.get('base_amount') or 0) + v['amount'], 2)
            db.execute('''UPDATE students SET amount_paid=%s, balance=%s, payment_date=%s, payment_mode=%s, cash_amount=%s,
                          paid_to=%s, income_month=%s, updated_at=%s WHERE id=%s''',
                       (new_paid, max(round((st['total_rent'] or 0) - new_paid, 2), 0), v['date'].isoformat(), v['mode'], v['cash'],
                        v['paid_to'], v['income_month'], now_iso, ref_id))
        else:
            db.execute('''UPDATE payment_history SET amount_paid=%s, balance=GREATEST(COALESCE(total_rent,0) - %s, 0), payment_date=%s,
                          payment_mode=%s, cash_amount=%s, paid_to=%s, income_month=%s WHERE id=%s''',
                       (v['amount'], v['amount'], v['date'].isoformat(), v['mode'], v['cash'], v['paid_to'], v['income_month'], ref_id))
        db.commit()
        flash(f"Payment of {ev['name']} updated.", 'success')
        return _after_payment_redirect(request.form)
    counted = 'auto'
    if ev.get('income_month') == this_key:
        counted = 'this'
    elif ev.get('income_month') == last_key:
        counted = 'last'
    return render_template('edit_payment.html', ev=ev, counted=counted, this_label=cycle_label(this_key),
                           last_label=cycle_label(last_key), today_iso=today_ist().isoformat(),
                           back_student=request.args.get('student', type=int), back_m=request.args.get('m') or 'this')


@app.route('/admin/payments/delete/<kind>/<int:ref_id>', methods=['POST'])
@login_required
def delete_payment_event(kind, ref_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return _after_payment_redirect(request.form)
    if kind not in ('submission', 'record', 'history'):
        flash('Payments of deleted students can only be changed after you restore the student.', 'error')
        return _after_payment_redirect(request.form)
    db = get_db()
    ev = _find_payment_event(db, kind, ref_id)
    if not ev:
        flash('That payment was not found.', 'error')
        return _after_payment_redirect(request.form)
    detail = f"One payment deleted from All Payments (mode: {ev.get('mode') or '-'}, paid to: {ev.get('paid_to') or '-'}, date: {ev['date'].isoformat()})"
    if kind == 'submission':
        sub = db.execute('SELECT * FROM payment_submissions WHERE id=%s', (ref_id,)).fetchone()
        if sub['status'] == 'CONFIRMED' and sub['student_id']:
            new_paid = _shift_student_paid(db, sub['student_id'], -(sub['amount'] or 0))
            if new_paid is not None and new_paid <= 0.005:
                _clear_student_payment_fields(db, sub['student_id'])
        db.execute('DELETE FROM payment_submissions WHERE id=%s', (ref_id,))
    elif kind == 'record':
        st = db.execute('SELECT * FROM students WHERE id=%s', (ref_id,)).fetchone()
        keep = round(ev.get('base_amount') or 0, 2)
        if keep <= 0.005:
            _clear_student_payment_fields(db, ref_id)
        else:
            db.execute('UPDATE students SET amount_paid=%s, balance=%s, income_month=NULL, updated_at=%s WHERE id=%s',
                       (keep, max(round((st['total_rent'] or 0) - keep, 2), 0), datetime.utcnow().isoformat(), ref_id))
    else:
        db.execute('DELETE FROM payment_history WHERE id=%s', (ref_id,))
    _log_payment_delete(db, ev['student_id'], ev['name'], ev['hostel'], ev['room_number'], ev['amount'], detail)
    db.commit()
    flash(f"Payment of ₹{ev['amount']:,.0f} from {ev['name']} deleted.", 'success')
    return _after_payment_redirect(request.form)


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
    month_start = today_ist().replace(day=1).isoformat()
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
    return redirect(url_for('month_payments', i=idx))


@app.route('/admin/month-payments/<int:student_id>/restore', methods=['POST'])
@login_required
def month_payment_restore(student_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('month_payments', view='deleted'))
    db = get_db()
    month_start = today_ist().replace(day=1).isoformat()
    db.execute('DELETE FROM month_payment_dismissals WHERE student_id=%s AND month_start=%s',
               (student_id, month_start))
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
            db.execute('''
                INSERT INTO students
                (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance, photo_filename, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)
            ''', (hostel, room_number, sharing, name, clean_contact, total_rent, amount_paid, balance, now, now))
            imported += 1
        except Exception:
            skipped += 1
            continue

    db.commit()
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
    """Individual payments confirmed by the admin during the current month for one hostel."""
    month_start = today_ist().replace(day=1).isoformat()
    rows = db.execute(
        "SELECT * FROM payment_submissions WHERE status='CONFIRMED' AND hostel=%s AND confirmed_at >= %s "
        "ORDER BY confirmed_at", (hostel, month_start)).fetchall()
    if not rows:
        return Paragraph('No confirmed payments recorded this month.', styles['EmptyRoom'])

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
            Paragraph('PAYMENTS RECORDED THIS MONTH', styles['RoomHeading']),
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