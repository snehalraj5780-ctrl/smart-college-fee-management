"""Database layer: schema, migrations, seed data and tiny query helpers (SQLite)."""
import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, date, timedelta

DB_FILE = os.environ.get('COLLEGE_DB', 'college_fee.db')

COURSES = ['B.Sc Computer Science', 'BCA', 'BBA', 'B.Com', 'BA']

DEFAULT_SETTINGS = {
    'college_name': 'Demo College of Arts, Commerce & Science',
    'academic_year': '2026-27',
    'late_fee_per_day': '50',        # DEMO policy - change in Admin > Settings
    'late_fee_cap': '2000',          # DEMO policy - change in Admin > Settings
    'allow_installments': '1',
    'max_installments': '4',
    'office_hours': 'Mon-Sat, 10:00 AM - 4:00 PM (update in Admin > Settings)',
    'office_location': 'Accounts Office, Main Building (update in Admin > Settings)',
    'accounts_email': 'accounts@college.com',
    'accounts_phone': '020-0000-0000',
    'fee_breakdown': '{"Tuition": 70, "Examination": 10, "Laboratory & Library": 12, "Development": 8}',
}

DEFAULT_COURSE_FEES = {
    'B.Sc Computer Science': 80000, 'BCA': 72000, 'BBA': 66000, 'B.Com': 56000, 'BA': 45000,
}


def now_iso():
    return datetime.now().isoformat(timespec='seconds')


def conn():
    c = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def q(sql, params=()):
    db = conn()
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def one(sql, params=()):
    db = conn()
    try:
        return db.execute(sql, params).fetchone()
    finally:
        db.close()


def execute(sql, params=()):
    db = conn()
    try:
        cur = db.execute(sql, params)
        db.commit()
        return cur.lastrowid
    finally:
        db.close()


def log(actor, action, details=''):
    execute('INSERT INTO audit_logs(actor,action,details,created_at) VALUES(?,?,?,?)',
            (actor, action, details, now_iso()))


# ---------- passwords ----------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 120000)
    return f'{salt}${dk.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split('$', 1)
        check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 120000).hex()
        return secrets.compare_digest(check, digest)
    except Exception:
        return False


# ---------- settings ----------
def get_setting(key, default=None):
    r = one('SELECT value FROM settings WHERE key=?', (key,))
    if r:
        return r['value']
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value):
    execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)))


# ---------- schema ----------
SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id TEXT UNIQUE,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL,
  phone TEXT,
  course TEXT,
  semester INTEGER,
  role TEXT NOT NULL DEFAULT 'student',
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  pref_channel TEXT NOT NULL DEFAULT 'In-app'
);
CREATE TABLE IF NOT EXISTS fees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  academic_year TEXT NOT NULL,
  total_fee REAL NOT NULL,
  due_date TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  payment_date TEXT NOT NULL,
  transaction_id TEXT UNIQUE NOT NULL,
  method TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'SUCCESS',
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS charges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,             -- LATE_FINE | CONCESSION | ADJUSTMENT
  amount REAL NOT NULL,           -- negative for concessions / waivers
  note TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS installments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  amount REAL NOT NULL,
  due_date TEXT NOT NULL,
  target_paid REAL NOT NULL,      -- cumulative amount that must be paid for this one to count as paid
  FOREIGN KEY(plan_id) REFERENCES plans(id)
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  reminder_type TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  channel TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'GENERATED',
  stage TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS faqs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'General'
);
CREATE TABLE IF NOT EXISTS chat_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  question TEXT NOT NULL,
  intent TEXT,
  confidence REAL,
  answered INTEGER NOT NULL DEFAULT 1,
  source TEXT,
  feedback INTEGER,               -- 1 = helpful, -1 = not helpful
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS course_fees (
  course TEXT PRIMARY KEY,
  total_fee REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  details TEXT,
  created_at TEXT NOT NULL
);
'''


def _ensure_column(db, table, column, ddl):
    cols = [r['name'] for r in db.execute(f'PRAGMA table_info({table})').fetchall()]
    if column not in cols:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')


def init_db():
    db = conn()
    try:
        db.execute('PRAGMA journal_mode=WAL')
    except Exception:
        pass
    db.executescript(SCHEMA)
    # migrations for databases created by the older version of the project
    _ensure_column(db, 'users', 'failed_attempts', 'INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'users', 'locked_until', 'TEXT')
    _ensure_column(db, 'users', 'pref_channel', "TEXT NOT NULL DEFAULT 'In-app'")
    _ensure_column(db, 'reminders', 'stage', 'TEXT')
    db.commit()
    seed_data(db)
    db.close()


# due_offset = days from TODAY (negative = already overdue). Dates are relative so the demo always
# shows a realistic mix of paid / due soon / pending / overdue students.
_STUDENTS = [
    ('STU1001', 'Ansh Patil', 'ansh@example.com', '9876501001', 'B.Sc Computer Science', 5, 80000, 60000, 0),
    ('STU1002', 'Rahul Sharma', 'rahul@example.com', '9876501002', 'BCA', 4, 72000, 52000, -7),
    ('STU1003', 'Sneha Kulkarni', 'sneha@example.com', '9876501003', 'BBA', 6, 68000, 68000, 5),
    ('STU1004', 'Priya Joshi', 'priya@example.com', '9876501004', 'B.Com', 3, 55000, 30000, 10),
    ('STU1005', 'Amit Deshmukh', 'amit@example.com', '9876501005', 'B.Sc Computer Science', 4, 82000, 40000, -3),
    ('STU1006', 'Neha Pawar', 'neha@example.com', '9876501006', 'BCA', 5, 75000, 25000, 3),
    ('STU1007', 'Rohan Jadhav', 'rohan@example.com', '9876501007', 'BBA', 2, 62000, 62000, 12),
    ('STU1008', 'Pooja More', 'pooja@example.com', '9876501008', 'B.Com', 6, 58000, 42000, 1),
    ('STU1009', 'Siddharth Shinde', 'siddharth@example.com', '9876501009', 'B.Sc Computer Science', 6, 85000, 70000, 7),
    ('STU1010', 'Kavya Pawar', 'kavya@example.com', '9876501010', 'BCA', 3, 70000, 50000, -12),
    ('STU1011', 'Omkar Gaikwad', 'omkar@example.com', '9876501011', 'BBA', 5, 66000, 16000, -20),
    ('STU1012', 'Isha Chavan', 'isha@example.com', '9876501012', 'B.Com', 4, 56000, 56000, 14),
    ('STU1013', 'Vivek Shirole', 'vivek@example.com', '9876501013', 'B.Sc Computer Science', 2, 78000, 30000, 6),
    ('STU1014', 'Riya Bhosale', 'riya@example.com', '9876501014', 'BCA', 6, 76000, 61000, 2),
    ('STU1015', 'Akash Wagh', 'akash@example.com', '9876501015', 'BBA', 3, 64000, 20000, -30),
    ('STU1016', 'Sakshi Salunkhe', 'sakshi@example.com', '9876501016', 'B.Com', 2, 54000, 10000, 9),
    ('STU1017', 'Nikhil Korde', 'nikhil@example.com', '9876501017', 'B.Sc Computer Science', 5, 81000, 81000, 11),
    ('STU1018', 'Aditi Mane', 'aditi@example.com', '9876501018', 'BCA', 4, 73000, 35000, -1),
    ('STU1019', 'Yash Pawar', 'yash@example.com', '9876501019', 'BBA', 6, 69000, 45000, 4),
    ('STU1020', 'Mrunal Patil', 'mrunal@example.com', '9876501020', 'B.Com', 5, 57000, 30000, 8),
    ('STU1021', 'Kunal More', 'kunal@example.com', '9876501021', 'B.Sc Computer Science', 3, 79000, 50000, -5),
    ('STU1022', 'Tanvi Joshi', 'tanvi@example.com', '9876501022', 'BCA', 2, 71000, 71000, 15),
    ('STU1023', 'Saurabh Kale', 'saurabh@example.com', '9876501023', 'BBA', 4, 65000, 30000, 20),
    ('STU1024', 'Shreya Pawar', 'shreya@example.com', '9876501024', 'B.Com', 3, 59000, 59000, 18),
    ('STU1025', 'Harshad Jagtap', 'harshad@example.com', '9876501025', 'B.Sc Computer Science', 6, 83000, 58000, 13),
    ('STU1026', 'Manasi Kadam', 'manasi@example.com', '9876501026', 'BCA', 5, 74000, 20000, -9),
    ('STU1027', 'Tejas Bansode', 'tejas@example.com', '9876501027', 'BBA', 2, 63000, 5000, -45),
    ('STU1028', 'Simran Shaikh', 'simran@example.com', '9876501028', 'B.Com', 6, 61000, 41000, 22),
    ('STU1029', 'Atharva Desai', 'atharva@example.com', '9876501029', 'B.Sc Computer Science', 4, 80000, 80000, 25),
    ('STU1030', 'Gauri Kharat', 'gauri@example.com', '9876501030', 'BCA', 3, 70000, 45000, 30),
]

_FAQS = [
    ('How can I pay my college fee?',
     'Login to the student portal, open the Pay Fee tab and choose an amount and method. In this demo the payment is simulated; a real deployment connects an approved payment gateway.', 'Payments'),
    ('How do I download my receipt?',
     'Open Payment History after a successful payment and click Receipt (PDF).', 'Payments'),
    ('Can I pay my fee in installments?',
     'Yes, if your college allows it. Open the Pay Fee tab (or ask the assistant to "split my fee") and choose 2 to 4 installments.', 'Fees'),
    ('What is the late fee policy?',
     'A late fine per overdue day (up to a maximum) is added when you pay after the due date. The exact rate is set by the accounts office; ask the assistant "what is my late fine?" to see your estimate.', 'Fees'),
    ('Where is the accounts department?',
     'Please update this answer in Admin > FAQ with your college building and room number. Office location is also available in Admin > Settings.', 'College'),
    ('What documents are required for a fee concession?',
     'Please contact the accounts/scholarship office. The admin should upload the college-approved document list here.', 'Scholarship'),
    ('What are the office timings?',
     'Office timings are configured by the admin in Settings. Ask the assistant "office hours" for the current timings.', 'College'),
    ('What happens if my payment fails but money is deducted?',
     'Keep the transaction reference/screenshot and contact the accounts office. Failed transactions are normally reversed by the bank within a few working days.', 'Payments'),
]


def seed_data(db):
    for k, v in DEFAULT_SETTINGS.items():
        db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))
    for c, f in DEFAULT_COURSE_FEES.items():
        db.execute('INSERT OR IGNORE INTO course_fees(course,total_fee) VALUES(?,?)', (c, f))
    if db.execute('SELECT COUNT(*) c FROM faqs').fetchone()['c'] == 0:
        for qn, an, cat in _FAQS:
            db.execute('INSERT INTO faqs(question,answer,category) VALUES(?,?,?)', (qn, an, cat))
    if db.execute("SELECT COUNT(*) c FROM users WHERE role='student'").fetchone()['c'] > 0:
        db.commit()
        return
    now = now_iso()
    today = date.today()
    pw = hash_password('student123')   # hash once; all demo students share the same demo password
    for sid, name, email, phone, course, sem, total, paid, off in _STUDENTS:
        cur = db.execute(
            'INSERT INTO users(student_id,name,email,phone,course,semester,role,password_hash,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)', (sid, name, email, phone, course, sem, 'student', pw, now))
        uid = cur.lastrowid
        db.execute('INSERT INTO fees(user_id,academic_year,total_fee,due_date) VALUES(?,?,?,?)',
                   (uid, '2026-27', total, (today + timedelta(days=off)).isoformat()))
        if paid:
            db.execute('INSERT INTO payments(user_id,amount,payment_date,transaction_id,method,status) '
                       'VALUES(?,?,?,?,?,?)',
                       (uid, paid, (today - timedelta(days=45)).isoformat(), f'TXN{sid[3:]}001', 'Online Demo', 'SUCCESS'))
    db.commit()
