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

    # The billing cycle the students' payment fields currently belong to. Starts as the month
    # before launch, so the very first request on/after LAUNCH_DATE archives + resets them.
    cur.execute('''
        INSERT INTO app_settings (key, value) VALUES ('billing_cycle', %s)
        ON CONFLICT (key) DO NOTHING
    ''', (previous_month_label(LAUNCH_DATE),))
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
        'add_student.html',
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


def get_due_students(db):
    """Students whose due day has arrived this month and who still have a balance.
    Nothing is due before LAUNCH_DATE."""
    today = today_ist()
    if today < LAUNCH_DATE:
        return []
    rows = db.execute('SELECT * FROM students WHERE balance > 0').fetchall()
    due = []
    for row in rows:
        s = dict(row)
        due_day = effective_due_day(s, today.year, today.month)
        if due_day is None or due_day > today.day:
            continue
        joined = parse_iso_date(s.get('date_of_join'))
        if joined and joined > today:
            continue
        s['due_day'] = due_day
        s['due_on'] = date(today.year, today.month, due_day).isoformat()
        s['days_overdue'] = today.day - due_day
        due.append(s)
    due.sort(key=lambda x: (x['due_day'], (x['name'] or '').lower()))
    return due


_rollover_checked_cycle = None


def ensure_monthly_rollover():
    """On the first request of each new month (IST), copy every student's payment details into
    payment_history and then blank them (amount paid, payment date, mode, receipt, cash amount,
    paid to, note). Everything else stays. Safe with several workers: the billing_cycle row is
    locked, so only one of them performs the reset."""
    global _rollover_checked_cycle
    today = today_ist()
    if today < LAUNCH_DATE:
        return
    cycle = today.strftime('%Y-%m')
    if _rollover_checked_cycle == cycle:
        return

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'billing_cycle' FOR UPDATE")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO app_settings (key, value) VALUES ('billing_cycle', %s) ON CONFLICT (key) DO NOTHING",
                        (previous_month_label(LAUNCH_DATE),))
            cur.execute("SELECT value FROM app_settings WHERE key = 'billing_cycle' FOR UPDATE")
            row = cur.fetchone()
        closing_cycle = row[0]

        if closing_cycle < cycle:
            now_iso = datetime.utcnow().isoformat()
            cur.execute('''
                INSERT INTO payment_history
                (student_id, hostel, room_number, name, contact, billing_month, total_rent, amount_paid, balance,
                 payment_date, payment_mode, cash_amount, paid_to, receipt_filename, note, archived_at)
                SELECT id, hostel, room_number, name, contact, %s, total_rent, amount_paid, balance,
                       payment_date, payment_mode, cash_amount, paid_to, receipt_filename, note, %s
                FROM students
            ''', (closing_cycle, now_iso))
            cur.execute('''
                UPDATE students
                SET amount_paid = 0, balance = total_rent, payment_date = NULL, payment_mode = NULL,
                    cash_amount = NULL, paid_to = NULL, receipt_filename = NULL, note = '', updated_at = %s
            ''', (now_iso,))
            cur.execute("UPDATE app_settings SET value = %s WHERE key = 'billing_cycle'", (cycle,))
        conn.commit()
        _rollover_checked_cycle = cycle
    except Exception:
        conn.rollback()
        app.logger.exception('Monthly payment rollover failed')
    finally:
        conn.close()


# --------------------------------------------------------------------------
# INITIALIZE DATABASE ON STARTUP
# --------------------------------------------------------------------------

init_db()


@app.before_request
def _monthly_rollover_hook():
    if request.endpoint == 'static':
        return
    try:
        ensure_monthly_rollover()
    except Exception:
        app.logger.exception('Monthly payment rollover hook failed')



# --------------------------------------------------------------------------
# PUBLIC ROUTES
# --------------------------------------------------------------------------

@app.route('/', methods=['GET', 'POST'], endpoint='index')
@app.route('/add-student', methods=['GET', 'POST'], endpoint='add_student')
def add_student():
    db = get_db()

    # "RECORD PAYMENT" opens this same page with the identified student's details pre-filled.
    # It only works for a student identified by mobile number on the payments page.
    if request.method == 'GET' and request.args.get('record_payment') == '1':
        student = current_payment_student(db)
        if not student:
            flash('Please enter your mobile number first.', 'error')
            return redirect(url_for('payments_lookup'))
        return render_record_payment(student)

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
        amount_paid = request.form.get('amount_paid', '').strip()
        note = request.form.get('note', '').strip()[:300]
        due_date = request.form.get('due_date', '').strip()

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

        if due_date == '':
            due_date = None
        else:
            try:
                due_date = int(due_date)
                if due_date < 1 or due_date > 31:
                    raise ValueError()
            except (ValueError, TypeError):
                errors.append('Due date must be a day of the month between 1 and 31.')
                due_date = None

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
        try:
            amount_paid = float(amount_paid)
            if amount_paid < 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append('Amount paid must be a valid non-negative number.')
            amount_paid = None

        if total_rent is not None and amount_paid is not None and amount_paid > total_rent:
            errors.append('Amount paid cannot be greater than total rent.')

        pay = parse_payment_fields(request.form, amount_paid, errors)

        photo_file = request.files.get('photo')
        photo_filename = None
        receipt_file = request.files.get('receipt')
        receipt_filename = None

        if not errors and room_number and sharing:
            ok, msg = check_capacity(db, hostel, room_number, sharing)
            if not ok:
                errors.append(msg)

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('add_student.html', form=request.form)

        try:
            if photo_file and photo_file.filename:
                photo_filename = save_photo(photo_file)
            if receipt_file and receipt_file.filename:
                receipt_filename = save_receipt(receipt_file)
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('add_student.html', form=request.form)

        balance = round(total_rent - amount_paid, 2)
        now = datetime.utcnow().isoformat()

        db.execute('''
            INSERT INTO students
            (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance, photo_filename, note, due_date,
             date_of_join, payment_date, payment_mode, cash_amount, paid_to, receipt_filename, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (hostel, room_number, sharing, name, clean_contact, total_rent, amount_paid, balance, photo_filename, note, due_date,
              pay['date_of_join'], pay['payment_date'], pay['payment_mode'], pay['cash_amount'], pay['paid_to'],
              receipt_filename, now, now))
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
    stats = get_stats(db)
    rooms_structure = get_rooms_structure(db)
    all_students = db.execute('SELECT * FROM students ORDER BY LOWER(name)').fetchall()
    all_students = [row_to_student(r) for r in all_students]

    due_count = len(get_due_students(db))
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
    )


# --------------------------------------------------------------------------
# VACANCY CHECK
# --------------------------------------------------------------------------

@app.route('/admin/vacancy')
@login_required
def check_vacancy():
    db = get_db()
    students = db.execute('SELECT * FROM students ORDER BY hostel, room_number, LOWER(name)').fetchall()
    note_rows = db.execute('SELECT * FROM room_notes').fetchall()
    notes_map = {(n['hostel'], n['room_number']): n['note'] for n in note_rows}

    occupied_rooms = {}
    for row in students:
        s = dict(row)
        key = (s['hostel'], s['room_number'])
        occupied_rooms.setdefault(key, {'sharing': s['sharing'], 'occupants': []})
        occupied_rooms[key]['occupants'].append(s)

    hostel_summary = {h: {'capacity': 0, 'occupied': 0, 'vacant': 0} for h in HOSTELS}
    room_cards = []

    for hostel in HOSTELS:
        for room_number in rooms_for_hostel(hostel):
            key = (hostel, room_number)
            note = notes_map.get(key, '')
            if key in occupied_rooms:
                info = occupied_rooms[key]
                sharing = info['sharing']
                occupied = len(info['occupants'])
                vacant = max(sharing - occupied, 0)
                hostel_summary[hostel]['capacity'] += sharing
                hostel_summary[hostel]['occupied'] += occupied
                hostel_summary[hostel]['vacant'] += vacant
                room_cards.append({
                    'hostel': hostel, 'room_number': room_number, 'sharing': sharing,
                    'occupied': occupied, 'vacant': vacant, 'occupants': info['occupants'],
                    'note': note, 'has_students': True,
                })
            else:
                room_cards.append({
                    'hostel': hostel, 'room_number': room_number, 'sharing': None,
                    'occupied': 0, 'vacant': None, 'occupants': [],
                    'note': note, 'has_students': False,
                })

    total_capacity = sum(h['capacity'] for h in hostel_summary.values())
    total_occupied = sum(h['occupied'] for h in hostel_summary.values())
    total_vacant = sum(h['vacant'] for h in hostel_summary.values())

    return render_template(
        'vacancy.html',
        room_cards=room_cards,
        hostel_summary=hostel_summary,
        total_capacity=total_capacity,
        total_occupied=total_occupied,
        total_vacant=total_vacant,
        hostels=HOSTELS,
    )


@app.route('/admin/vacancy/note', methods=['POST'])
@login_required
def save_room_note():
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('check_vacancy'))

    hostel = request.form.get('hostel', '').strip()
    room_number = request.form.get('room_number', '').strip()
    note = request.form.get('note', '').strip()[:300]

    if hostel not in HOSTELS or not room_number.isdigit() or int(room_number) not in rooms_for_hostel(hostel):
        flash('Invalid room.', 'error')
        return redirect(url_for('check_vacancy'))
    room_number = int(room_number)

    db = get_db()
    now = datetime.utcnow().isoformat()
    if note:
        # ON CONFLICT relies on the UNIQUE(hostel, room_number) constraint, so a room
        # can never end up with two separate notes — the existing one is just updated.
        db.execute('''
            INSERT INTO room_notes (hostel, room_number, note, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (hostel, room_number)
            DO UPDATE SET note = EXCLUDED.note, updated_at = EXCLUDED.updated_at
        ''', (hostel, room_number, note, now))
        flash(f'Note saved for {hostel} - Room {room_number}.', 'success')
    else:
        db.execute('DELETE FROM room_notes WHERE hostel=%s AND room_number=%s', (hostel, room_number))
        flash(f'Note cleared for {hostel} - Room {room_number}.', 'success')
    db.commit()

    return redirect(url_for('check_vacancy'))


# --------------------------------------------------------------------------
# DUE LIST
# --------------------------------------------------------------------------

@app.route('/admin/due-list')
@login_required
def due_list():
    db = get_db()
    today = today_ist()
    students = get_due_students(db)
    return render_template(
        'due_list.html',
        students=students,
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

    return render_template(
        'payments_dashboard.html',
        student=student,
        due_amount=due_amount,
        pay_info=pay_info,
        for_month=next_month_label(),
        recent=[dict(r) for r in recent],
    )


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
    rows = db.execute('SELECT * FROM payment_submissions ORDER BY id DESC LIMIT 200').fetchall()
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
        amount_paid = request.form.get('amount_paid', '').strip()
        note = request.form.get('note', '').strip()[:300]
        due_date = request.form.get('due_date', '').strip()
        remove_photo = request.form.get('remove_photo') == '1'
        remove_receipt = request.form.get('remove_receipt') == '1'

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

        if due_date == '':
            due_date = None
        else:
            try:
                due_date = int(due_date)
                if due_date < 1 or due_date > 31:
                    raise ValueError()
            except (ValueError, TypeError):
                errors.append('Due date must be a day of the month between 1 and 31.')
                due_date = None

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
        try:
            amount_paid = float(amount_paid)
            if amount_paid < 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append('Amount paid must be a valid non-negative number.')
            amount_paid = None

        if total_rent is not None and amount_paid is not None and amount_paid > total_rent:
            errors.append('Amount paid cannot be greater than total rent.')

        pay = parse_payment_fields(request.form, amount_paid, errors)

        if not errors and room_number and sharing:
            room_changed = (hostel != student['hostel'] or room_number != student['room_number'])
            ok, msg = check_capacity(db, hostel, room_number, sharing, exclude_id=student_id)
            if not ok:
                errors.append(msg)

        photo_file = request.files.get('photo')
        new_photo_filename = student['photo_filename']
        receipt_file = request.files.get('receipt')
        new_receipt_filename = student.get('receipt_filename')

        if errors:
            for e in errors:
                flash(e, 'error')
            merged = dict(student)
            merged.update(request.form)
            return render_template('edit_student.html', student=merged)

        try:
            if photo_file and photo_file.filename:
                saved = save_photo(photo_file)
                if saved:
                    delete_photo(student['photo_filename'])
                    new_photo_filename = saved
            elif remove_photo:
                delete_photo(student['photo_filename'])
                new_photo_filename = None
            # Receipts are never deleted from storage (payment history may still point to them).
            if receipt_file and receipt_file.filename:
                saved_receipt = save_receipt(receipt_file)
                if saved_receipt:
                    new_receipt_filename = saved_receipt
            elif remove_receipt:
                new_receipt_filename = None
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('edit_student.html', student=student)

        balance = round(total_rent - amount_paid, 2)
        now = datetime.utcnow().isoformat()

        db.execute('''
            UPDATE students
            SET hostel=%s, room_number=%s, sharing=%s, name=%s, contact=%s, total_rent=%s, amount_paid=%s, balance=%s, photo_filename=%s, note=%s, due_date=%s,
                date_of_join=%s, payment_date=%s, payment_mode=%s, cash_amount=%s, paid_to=%s, receipt_filename=%s, updated_at=%s
            WHERE id=%s
        ''', (hostel, room_number, sharing, name, clean_contact, total_rent, amount_paid, balance, new_photo_filename, note, due_date,
              pay['date_of_join'], pay['payment_date'], pay['payment_mode'], pay['cash_amount'], pay['paid_to'],
              new_receipt_filename, now, student_id))
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
    delete_photo(student['photo_filename'])
    db.execute('DELETE FROM students WHERE id = %s', (student_id,))
    db.commit()
    flash(f"{student['name']} was deleted.", 'success')
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


@app.route('/admin/load-register', methods=['POST'])
@login_required
def load_register_data():
    """One-time tool: deletes ALL students and loads the hostel register from seed_data.py."""
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Security check failed. Please try again.', 'error')
        return redirect(url_for('admin_dashboard'))
    if request.form.get('confirm', '').strip() != 'YES':
        flash('Nothing was changed. Type YES (capital letters) to confirm.', 'error')
        return redirect(url_for('admin_dashboard'))

    from seed_data import build_rows
    rows = build_rows()
    db = get_db()
    try:
        # make sure up-to-9 sharing is allowed (same rule init_db applies)
        db.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_sharing_check")
        db.execute("ALTER TABLE students ADD CONSTRAINT students_sharing_check CHECK (sharing BETWEEN 1 AND 9)")
        db.execute('DELETE FROM students')
        now = datetime.utcnow().isoformat()
        for r in rows:
            db.execute('''
                INSERT INTO students
                (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance,
                 note, date_of_join, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', tuple(r) + (now, now))
        db.commit()
    except Exception as exc:
        db.conn.rollback()
        flash(f'Could not load the register data: {exc}', 'error')
        return redirect(url_for('admin_dashboard'))

    flash(f'Done! Old students removed and {len(rows)} students loaded into their hostel, room and sharing.', 'success')
    return redirect(url_for('admin_dashboard'))


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