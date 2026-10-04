"""Business logic: fee ledger, late fine, installments, reminders, e-mail, receipts, risk scoring."""
import io
import os
import json
import smtplib
import secrets
from email.message import EmailMessage
from datetime import datetime, date, timedelta

import pandas as pd

from db import q, one, execute, conn, log, get_setting, now_iso

CHANNELS = ['In-app', 'Email', 'SMS (demo)', 'WhatsApp (demo)']

# reminder stage -> minimum days before the same stage is sent again to the same student
STAGE_COOLDOWN = {'PRE_7': 7, 'PRE_3': 3, 'PRE_1': 1, 'DUE_TODAY': 1, 'OVERDUE': 3}
STAGE_LABEL = {
    'PRE_7': 'Upcoming (4-7 days)', 'PRE_3': 'Due soon (2-3 days)', 'PRE_1': 'Due tomorrow',
    'DUE_TODAY': 'Due today', 'OVERDUE': 'Overdue',
}


def money(v):
    return f'₹{float(v):,.0f}'


def get_secret(name, default=None):
    """Environment variable first, then Streamlit secrets (works outside Streamlit too)."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        import streamlit as st
        return st.secrets.get(name, default)
    except Exception:
        return default


# ---------------------------------------------------------------- ledger
def _load_plan(uid, paid, today):
    p = one("SELECT * FROM plans WHERE user_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1", (uid,))
    if not p:
        return None
    items = []
    for r in q('SELECT * FROM installments WHERE plan_id=? ORDER BY seq', (p['id'],)):
        due = date.fromisoformat(r['due_date'])
        if paid >= r['target_paid'] - 0.5:
            st = 'PAID'
        elif due < today:
            st = 'OVERDUE'
        else:
            st = 'UPCOMING'
        items.append({'seq': r['seq'], 'amount': float(r['amount']), 'due_date': due, 'status': st})
    return {'id': p['id'], 'created_at': p['created_at'], 'items': items}


def student_record(uid, today=None):
    """Everything the UI, reminders and chatbot need to know about one student's fee account."""
    today = today or date.today()
    u = one('SELECT * FROM users WHERE id=?', (uid,))
    f = one('SELECT * FROM fees WHERE user_id=? ORDER BY id DESC LIMIT 1', (uid,))
    paid = float(one("SELECT COALESCE(SUM(amount),0) p FROM payments WHERE user_id=? AND status='SUCCESS'", (uid,))['p'])
    charges = float(one('SELECT COALESCE(SUM(amount),0) s FROM charges WHERE user_id=?', (uid,))['s'])
    fine_charged = float(one("SELECT COALESCE(SUM(amount),0) s FROM charges WHERE user_id=? AND kind='LATE_FINE'", (uid,))['s'])
    total_fee = float(f['total_fee']) if f else 0.0
    owed = total_fee + charges
    pending = max(owed - paid, 0.0)
    fee_due = date.fromisoformat(f['due_date']) if f else None

    plan = _load_plan(uid, paid, today)
    due, next_inst = fee_due, None
    if plan and pending > 0:
        unpaid = [i for i in plan['items'] if i['status'] != 'PAID']
        if unpaid:
            next_inst = unpaid[0]
            due = next_inst['due_date']
    days = (due - today).days if due else None

    if owed <= 0 and paid <= 0:
        status = 'NOT ASSIGNED'
    elif pending <= 0:
        status = 'PAID'
    elif days is not None and days < 0:
        status = 'OVERDUE'
    elif days is not None and days <= 7:
        status = 'DUE SOON'
    else:
        status = 'PENDING'

    fine_est = 0.0
    if pending > 0 and days is not None and days < 0 and fine_charged <= 0:
        rate = float(get_setting('late_fee_per_day', 0) or 0)
        cap = float(get_setting('late_fee_cap', 0) or 0)
        fine_est = float(round(min(-days * rate, cap)))

    return {
        'user': u, 'fee': f, 'total_fee': total_fee, 'charges': charges, 'fine_charged': fine_charged,
        'owed': owed, 'paid': paid, 'pending': pending, 'fee_due': fee_due, 'due': due, 'days': days,
        'status': status, 'late_fine_est': fine_est, 'payable_now': pending + fine_est,
        'plan': plan, 'next_installment': next_inst,
    }


def fee_breakdown(total_fee):
    try:
        parts = json.loads(get_setting('fee_breakdown', '{}'))
        s = sum(float(v) for v in parts.values()) or 100.0
        return {k: round(total_fee * float(v) / s) for k, v in parts.items()}
    except Exception:
        return {'Total fee': round(total_fee)}


# ---------------------------------------------------------------- payments & plans
def make_payment(uid, amount, method, actor, prefix='TXN'):
    """Record a payment. If the student is overdue, the late fine is assessed (once) at this point."""
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError('Amount must be greater than zero.')
    rec = student_record(uid)
    fine = rec['late_fine_est']
    if amount > rec['pending'] + fine + 0.01:
        raise ValueError(f'Amount is more than the payable balance ({money(rec["pending"] + fine)}).')
    tx = prefix + datetime.now().strftime('%Y%m%d%H%M%S') + secrets.token_hex(2).upper()
    db = conn()
    try:
        if fine > 0:
            db.execute('INSERT INTO charges(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)',
                       (uid, 'LATE_FINE', fine, f'Late fine, {-rec["days"]} day(s) overdue', now_iso()))
        db.execute('INSERT INTO payments(user_id,amount,payment_date,transaction_id,method,status) VALUES(?,?,?,?,?,?)',
                   (uid, amount, date.today().isoformat(), tx, method, 'SUCCESS'))
        db.commit()
    finally:
        db.close()
    log(actor, 'PAYMENT', f'{money(amount)} {tx}' + (f' (late fine {money(fine)} added)' if fine else ''))
    return tx, fine


def create_plan(uid, n, actor):
    if get_setting('allow_installments', '1') != '1':
        raise ValueError('Installments are not enabled by the college.')
    n = int(n)
    max_n = int(get_setting('max_installments', 4))
    if not 2 <= n <= max_n:
        raise ValueError(f'Choose between 2 and {max_n} installments.')
    rec = student_record(uid)
    if rec['pending'] <= 0:
        raise ValueError('There is no pending fee to split.')
    if rec['plan']:
        raise ValueError('You already have an active installment plan.')
    today = date.today()
    first = rec['fee_due'] if rec['fee_due'] and rec['fee_due'] >= today else today + timedelta(days=7)
    base = int(rec['pending'] // n)
    db = conn()
    try:
        pid = db.execute('INSERT INTO plans(user_id,created_at,status) VALUES(?,?,?)', (uid, now_iso(), 'ACTIVE')).lastrowid
        running = 0.0
        for i in range(n):
            amt = float(base if i < n - 1 else rec['pending'] - base * (n - 1))
            running += amt
            db.execute('INSERT INTO installments(plan_id,seq,amount,due_date,target_paid) VALUES(?,?,?,?,?)',
                       (pid, i + 1, amt, (first + timedelta(days=30 * i)).isoformat(), rec['paid'] + running))
        db.commit()
    finally:
        db.close()
    log(rec['user']['email'], 'CREATE_PLAN', f'{n} installments for {money(rec["pending"])}')
    return pid


def cancel_plan(uid, actor):
    execute("UPDATE plans SET status='CANCELLED' WHERE user_id=? AND status='ACTIVE'", (uid,))
    log(actor, 'CANCEL_PLAN', f'user {uid}')


def add_charge(uid, kind, amount, note, actor):
    execute('INSERT INTO charges(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)',
            (uid, kind, float(amount), note, now_iso()))
    log(actor, kind, f'user {uid}: {money(amount)} {note}')


# ---------------------------------------------------------------- reminders
def reminder_stage(rec):
    if rec['pending'] <= 0 or rec['days'] is None:
        return None
    d = rec['days']
    if d < 0:
        return 'OVERDUE'
    if d == 0:
        return 'DUE_TODAY'
    if d == 1:
        return 'PRE_1'
    if d <= 3:
        return 'PRE_3'
    if d <= 7:
        return 'PRE_7'
    return None


def reminder_text(rec, stage=None):
    stage = stage or reminder_stage(rec)
    u = rec['user']
    name = u['name']
    pend = money(rec['pending'])
    amt = rec['next_installment']['amount'] if rec['next_installment'] else rec['pending']
    what = f'your installment of {money(amt)}' if rec['next_installment'] else f'your pending fee of {pend}'
    contact = f'Accounts office: {get_setting("accounts_phone")} / {get_setting("accounts_email")}.'
    due = rec['due'].strftime('%d %b %Y') if rec['due'] else ''
    if rec['pending'] <= 0:
        return f'Hello {name}, your college fee is fully paid. Thank you!'
    if stage == 'OVERDUE':
        fine = f' A late fine of about {money(rec["late_fine_est"])} will be added when you pay.' if rec['late_fine_est'] else ''
        return (f'Hello {name}, {what} was due on {due} and is now {-rec["days"]} day(s) overdue.{fine} '
                f'Total pending: {pend}. Please pay at the earliest or open the portal to split it into installments. {contact}')
    if stage == 'DUE_TODAY':
        return f'Hello {name}, {what} is due TODAY ({due}). Total pending: {pend}. Please pay today to avoid a late fine.'
    if stage == 'PRE_1':
        return f'Hello {name}, {what} is due TOMORROW ({due}). Total pending: {pend}. Please pay on time to avoid a late fine.'
    if stage == 'PRE_3':
        return f'Hello {name}, this is an urgent reminder: {what} is due on {due} ({rec["days"]} days left). Total pending: {pend}.'
    return f'Hello {name}, a friendly reminder that {what} is due on {due}. Total pending: {pend}. Please complete the payment on time.'


def send_email(to, subject, body):
    """Send through SMTP if configured. Returns (ok, status_text)."""
    host = get_secret('SMTP_HOST')
    if not host or not to:
        return False, 'LOGGED (SMTP not configured)'
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = get_secret('SMTP_FROM') or get_secret('SMTP_USER') or 'noreply@college.com'
        msg['To'] = to
        msg.set_content(body)
        with smtplib.SMTP(host, int(get_secret('SMTP_PORT', 587)), timeout=15) as s:
            s.starttls()
            if get_secret('SMTP_USER'):
                s.login(get_secret('SMTP_USER'), get_secret('SMTP_PASSWORD', ''))
            s.send_message(msg)
        return True, 'SENT'
    except Exception as e:
        return False, f'FAILED: {str(e)[:80]}'


def _recently_sent(uid, stage):
    cutoff = (datetime.now() - timedelta(days=STAGE_COOLDOWN.get(stage, 1)) + timedelta(minutes=1)).isoformat(timespec='seconds')
    return one('SELECT 1 FROM reminders WHERE user_id=? AND stage=? AND sent_at>=?', (uid, stage, cutoff)) is not None


def queue_reminder(uid, channel, stage, message, status):
    execute('INSERT INTO reminders(user_id,reminder_type,sent_at,channel,message,status,stage) VALUES(?,?,?,?,?,?,?)',
            (uid, STAGE_LABEL.get(stage, stage or 'MANUAL'), now_iso(), channel, message, status, stage))


def deliver_reminder(rec, channel, stage, message):
    """Send/log one reminder on a channel and store it. Returns the status text."""
    u = rec['user']
    if channel == 'Email':
        ok, status = send_email(u['email'], f'Fee reminder - {get_setting("college_name")}', message)
    elif channel == 'In-app':
        status = 'DELIVERED'
    else:
        status = 'LOGGED (demo)'
    queue_reminder(u['id'], channel, stage, message, status)
    return status


def run_auto_reminders(channel='In-app', stages=None, dry_run=False, force=False, actor='SYSTEM'):
    """Escalating reminder engine. channel may be 'Student preference'. Skips students reminded recently."""
    out = []
    for r in q("SELECT id FROM users WHERE role='student' ORDER BY student_id"):
        rec = student_record(r['id'])
        stage = reminder_stage(rec)
        if not stage or (stages and stage not in stages):
            continue
        if not force and _recently_sent(r['id'], stage):
            continue
        ch = rec['user']['pref_channel'] if channel == 'Student preference' else channel
        msg = reminder_text(rec, stage)
        status = 'PREVIEW'
        if not dry_run:
            status = deliver_reminder(rec, ch, stage, msg)
        out.append({'Student ID': rec['user']['student_id'], 'Name': rec['user']['name'], 'Stage': STAGE_LABEL[stage],
                    'Channel': ch, 'Pending': rec['pending'], 'Status': status, 'Message': msg})
    if not dry_run:
        log(actor, 'AUTO_REMINDERS', f'{channel}: {len(out)} reminders')
    return out


# ---------------------------------------------------------------- receipts
def receipt_text(u, p):
    return (f'COLLEGE FEE PAYMENT RECEIPT\n{"=" * 45}\n{get_setting("college_name")}\n\n'
            f'Student ID: {u["student_id"]}\nStudent Name: {u["name"]}\nCourse: {u["course"]}\nSemester: {u["semester"]}\n'
            f'Amount Paid: {money(p["amount"])}\nPayment Date: {p["payment_date"]}\nTransaction ID: {p["transaction_id"]}\n'
            f'Payment Method: {p["method"]}\nStatus: {p["status"]}\n\nThis is a system-generated demo receipt.\n')


def receipt_pdf(u, p):
    """Return PDF bytes, or None if reportlab is not installed."""
    try:
        from reportlab.lib.pagesizes import A5
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except Exception:
        return None
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A5)
    w, h = A5
    c.setFillColor(colors.HexColor('#1f3a8a'))
    c.rect(0, h - 28 * mm, w, 28 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 15)
    c.drawString(12 * mm, h - 14 * mm, get_setting('college_name')[:48])
    c.setFont('Helvetica', 10)
    c.drawString(12 * mm, h - 21 * mm, 'FEE PAYMENT RECEIPT')
    c.setFillColor(colors.black)
    rows = [('Receipt / Transaction ID', p['transaction_id']), ('Payment date', p['payment_date']),
            ('Student ID', u['student_id']), ('Student name', u['name']),
            ('Course / Semester', f'{u["course"]} / Sem {u["semester"]}'),
            ('Payment method', p['method']), ('Status', p['status'])]
    y = h - 42 * mm
    for k, v in rows:
        c.setFont('Helvetica', 9)
        c.setFillColor(colors.HexColor('#6b7280'))
        c.drawString(12 * mm, y, k)
        c.setFillColor(colors.black)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(60 * mm, y, str(v))
        y -= 8 * mm
    c.setFillColor(colors.HexColor('#ecfdf5'))
    c.roundRect(12 * mm, y - 14 * mm, w - 24 * mm, 14 * mm, 3 * mm, fill=1, stroke=0)
    c.setFillColor(colors.HexColor('#065f46'))
    c.setFont('Helvetica-Bold', 14)
    c.drawString(16 * mm, y - 9 * mm, f'Amount paid: Rs. {float(p["amount"]):,.0f}')
    c.setFillColor(colors.HexColor('#6b7280'))
    c.setFont('Helvetica-Oblique', 8)
    c.drawString(12 * mm, 12 * mm, 'System-generated demo receipt. No signature required.')
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- analytics
def risk_score(rec, reminders_30d=0):
    """Transparent rule-based default-risk score (0-100). Not a trained ML model."""
    if rec['pending'] <= 0:
        return 0
    score = 0.0
    d = rec['days']
    if d is not None and d < 0:
        score += 40 + min(-d, 30) / 30 * 20
    elif d is not None and d <= 3:
        score += 15
    elif d is not None and d <= 7:
        score += 8
    ratio = rec['pending'] / rec['owed'] if rec['owed'] else 0
    score += ratio * 25
    score += min(reminders_30d, 5) * 3
    return int(min(round(score), 100))


def risk_label(score):
    return 'High' if score >= 70 else ('Medium' if score >= 40 else ('Low' if score > 0 else 'None'))


def all_students_df():
    cutoff = (datetime.now() - timedelta(days=30)).isoformat(timespec='seconds')
    rows = []
    for r in q("SELECT id FROM users WHERE role='student' ORDER BY student_id"):
        rec = student_record(r['id'])
        u = rec['user']
        n_rem = one('SELECT COUNT(*) c FROM reminders WHERE user_id=? AND sent_at>=?', (u['id'], cutoff))['c']
        rs = risk_score(rec, n_rem)
        rows.append({
            'Student ID': u['student_id'], 'Name': u['name'], 'Email': u['email'], 'Phone': u['phone'],
            'Course': u['course'], 'Semester': u['semester'], 'Total Fee': rec['owed'], 'Paid': rec['paid'],
            'Pending': rec['pending'], 'Due Date': rec['due'].isoformat() if rec['due'] else '',
            'Days to Due': rec['days'], 'Status': rec['status'], 'Late Fine Est.': rec['late_fine_est'],
            'On Plan': 'Yes' if rec['plan'] else 'No', 'Reminders (30d)': n_rem,
            'Risk Score': rs, 'Risk': risk_label(rs), 'User ID': u['id'],
        })
    return pd.DataFrame(rows)


def collection_trend(days=60):
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = q("SELECT payment_date d, SUM(amount) a, COUNT(*) n FROM payments WHERE status='SUCCESS' AND payment_date>=? "
             'GROUP BY payment_date ORDER BY payment_date', (since,))
    return pd.DataFrame([dict(r) for r in rows], columns=['d', 'a', 'n'])


def excel_bytes(sheets):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine='openpyxl') as w:
        for name, df in sheets.items():
            df.to_excel(w, index=False, sheet_name=name[:31])
    return bio.getvalue()
