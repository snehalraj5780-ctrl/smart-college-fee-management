"""AI-Powered Smart College Fee Management & Reminder Chatbot  -  Streamlit UI.

Modules:  db.py (schema/seed)  services.py (fees, reminders, receipts, risk)  chatbot.py (assistant)
"""
import re
import json
import secrets
from datetime import datetime, date, timedelta

import pandas as pd
import streamlit as st

from db import (init_db, q, one, execute, log, hash_password, verify_password, get_setting, set_setting, COURSES, now_iso)
from services import (money, get_secret, student_record, fee_breakdown, make_payment, create_plan, cancel_plan, add_charge,
                      reminder_stage, reminder_text, deliver_reminder, run_auto_reminders, receipt_pdf, receipt_text,
                      all_students_df, collection_trend, excel_bytes, send_email, CHANNELS, STAGE_LABEL)
from chatbot import answer_student, answer_admin, log_chat, llm_available

st.set_page_config(page_title='Smart College Fee Portal', page_icon='🎓', layout='wide')
init_db()

ADMIN_EMAIL = (get_secret('ADMIN_EMAIL', 'admin@college.com') or 'admin@college.com').strip().lower()
ADMIN_PASSWORD = get_secret('ADMIN_PASSWORD', 'admin123') or 'admin123'
USING_DEFAULT_ADMIN = get_secret('ADMIN_PASSWORD') is None
MAX_ATTEMPTS, LOCK_MINUTES = 5, 10

st.markdown('''<style>
.block-container{padding-top:1.2rem;max-width:1400px}
.hero{padding:18px 22px;border-radius:16px;background:linear-gradient(120deg,#1e3a8a,#2563eb);color:#fff;margin-bottom:14px}
.hero h2{margin:0;color:#fff}.hero p{margin:4px 0 0;opacity:.9}
[data-testid="stMetric"]{background:#f8fafc;border:1px solid #e5e7eb;padding:10px 14px;border-radius:12px}
</style>''', unsafe_allow_html=True)


# ------------------------------------------------------------------ small UI helpers (work on old & new Streamlit)
def df_show(df, **kw):
    try:
        st.dataframe(df, width='stretch', hide_index=True, **kw)
    except Exception:
        st.dataframe(df, use_container_width=True, hide_index=True, **kw)


def btn(label, wide=False, **kw):
    if wide:
        try:
            return st.button(label, width='stretch', **kw)
        except Exception:
            return st.button(label, use_container_width=True, **kw)
    return st.button(label, **kw)


def dl_btn(label, data, file_name, mime, **kw):
    return st.download_button(label, data, file_name=file_name, mime=mime, **kw)


def next_student_id():
    n = one("SELECT COALESCE(MAX(CAST(substr(student_id,4) AS INTEGER)),1000)+1 n FROM users WHERE student_id LIKE 'STU%'")['n']
    return f'STU{n}'


def course_fee(course):
    r = one('SELECT total_fee FROM course_fees WHERE course=?', (course,))
    return float(r['total_fee']) if r else 0.0


def locked(until):
    try:
        return bool(until) and datetime.fromisoformat(until) > datetime.now()
    except Exception:
        return False


# ------------------------------------------------------------------ session
for k, v in {'auth': False, 'role': None, 'uid': None, 'chat': [], 'chat_ctx': {}, 'admin_chat': [], 'pending_prompt': None}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ------------------------------------------------------------------ login / registration
def login_page():
    st.markdown('<div class="hero"><h2>🎓 Smart College Fee Portal</h2>'
                '<p>Check dues • pay in installments • smart reminders • AI fee assistant</p></div>', unsafe_allow_html=True)
    t1, t2, t3 = st.tabs(['🔐 Login', '📝 Student Registration', 'ℹ️ Demo Access'])
    with t1:
        role = st.radio('Login as', ['Student', 'Admin'], horizontal=True)
        email = st.text_input('Email')
        password = st.text_input('Password', type='password')
        if btn('Login', wide=True, type='primary'):
            email = email.strip().lower()
            if role == 'Admin':
                until = get_setting('admin_locked_until', '')
                if locked(until):
                    st.error(f'Too many failed attempts. Try again after {datetime.fromisoformat(until).strftime("%H:%M")}.')
                elif email == ADMIN_EMAIL and secrets.compare_digest(password, ADMIN_PASSWORD):
                    set_setting('admin_failed', 0)
                    st.session_state.update(auth=True, role='admin', uid=None)
                    log('ADMIN', 'LOGIN', 'Admin login')
                    st.rerun()
                else:
                    n = int(get_setting('admin_failed', 0) or 0) + 1
                    if n >= MAX_ATTEMPTS:
                        set_setting('admin_locked_until', (datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec='seconds'))
                        n = 0
                    set_setting('admin_failed', n)
                    log('ADMIN', 'LOGIN_FAILED', email)
                    st.error('Invalid admin credentials.')
            else:
                u = one("SELECT * FROM users WHERE lower(email)=? AND role='student'", (email,))
                if u and locked(u['locked_until']):
                    st.error(f'Account temporarily locked. Try again after {datetime.fromisoformat(u["locked_until"]).strftime("%H:%M")}.')
                elif u and verify_password(password, u['password_hash']):
                    execute('UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?', (u['id'],))
                    st.session_state.update(auth=True, role='student', uid=u['id'], chat=[], chat_ctx={})
                    log(u['email'], 'LOGIN', 'Student login')
                    st.rerun()
                else:
                    if u:
                        n = u['failed_attempts'] + 1
                        if n >= MAX_ATTEMPTS:
                            execute('UPDATE users SET failed_attempts=0, locked_until=? WHERE id=?',
                                    ((datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec='seconds'), u['id']))
                        else:
                            execute('UPDATE users SET failed_attempts=? WHERE id=?', (n, u['id']))
                    st.error('Invalid student email or password.')
    with t2:
        with st.form('register'):
            name = st.text_input('Full name')
            email = st.text_input('Email address')
            phone = st.text_input('Mobile number')
            course = st.selectbox('Course', COURSES)
            sem = st.selectbox('Semester', list(range(1, 7)))
            pw = st.text_input('Create password (min 6 characters)', type='password')
            pw2 = st.text_input('Confirm password', type='password')
            if st.form_submit_button('Create Student Account'):
                if not name.strip() or not email.strip() or not pw:
                    st.error('Please fill all required fields.')
                elif len(pw) < 6:
                    st.error('Password must be at least 6 characters.')
                elif pw != pw2:
                    st.error('Passwords do not match.')
                elif not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email.strip()):
                    st.error('Enter a valid email.')
                elif phone.strip() and not re.fullmatch(r'\d{10}', phone.strip()):
                    st.error('Mobile number must be 10 digits.')
                elif one('SELECT id FROM users WHERE lower(email)=lower(?)', (email.strip(),)):
                    st.error('Email already registered.')
                else:
                    sid = next_student_id()
                    uid = execute('INSERT INTO users(student_id,name,email,phone,course,semester,role,password_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                  (sid, name.strip(), email.strip(), phone.strip(), course, sem, 'student', hash_password(pw), now_iso()))
                    execute('INSERT INTO fees(user_id,academic_year,total_fee,due_date) VALUES(?,?,?,?)',
                            (uid, get_setting('academic_year'), course_fee(course), (date.today() + timedelta(days=30)).isoformat()))
                    log(email.strip(), 'REGISTER', f'New student {sid}')
                    st.success(f'Account created. Your Student ID is {sid}. You can now login.')
    with t3:
        st.info('Demo student: ansh@example.com / student123  (all 30 seeded students use student123)')
        if USING_DEFAULT_ADMIN:
            st.info('Demo admin: admin@college.com / admin123')
            st.warning('Default admin password in use. Set ADMIN_EMAIL / ADMIN_PASSWORD in Streamlit secrets before real use.')
        st.caption('Try the assistant: "how much is pending", "split my fee into 3 installments", "pay 5000", "late fine", "kitna baki hai".')


# ------------------------------------------------------------------ student: chatbot panel
def push_assistant(text, **extra):
    st.session_state.chat.append({'role': 'assistant', 'content': text, **extra})


def process_prompt(prompt, uid):
    hist = [(m['role'], m['content']) for m in st.session_state.chat]
    r = answer_student(prompt, uid, st.session_state.chat_ctx, hist)
    log_id = log_chat(uid, prompt, r)
    st.session_state.chat.append({'role': 'user', 'content': prompt})
    push_assistant(r.text, table=r.table, action=r.action, suggestions=r.suggestions, log_id=log_id, source=r.source)


def payment_by_id(pid):
    return one('SELECT * FROM payments WHERE id=?', (pid,))


def render_action(msg, u, rec, idx):
    a = msg.get('action')
    if not a:
        return
    uid = u['id']
    t = a['type']
    if t == 'pay' and rec['pending'] > 0:
        amt = min(float(a['amount']), rec['payable_now'])
        c1, c2 = st.columns([2, 3])
        method = c1.selectbox('Method', ['UPI Demo', 'Card Demo', 'Net Banking Demo'], key=f'cm{idx}', label_visibility='collapsed')
        if c2.button(f'💳 Pay {money(amt)} now (demo)', key=f'cp{idx}', type='primary'):
            try:
                tx, fine = make_payment(uid, amt, method, u['email'])
                p = one('SELECT * FROM payments WHERE transaction_id=?', (tx,))
                st.session_state.chat_ctx['last_intent'] = None
                push_assistant(f'✅ Payment of **{money(amt)}** successful. Transaction ID `{tx}`.'
                               + (f' A late fine of {money(fine)} was added.' if fine else ''), action={'type': 'receipt', 'payment_id': p['id']})
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    elif t == 'plan_choices':
        cols = st.columns(a['max'] - 1)
        for i, n in enumerate(range(2, a['max'] + 1)):
            if cols[i].button(f'{n} installments', key=f'pc{idx}_{n}'):
                process_prompt(f'{n} installments', uid)
                st.rerun()
    elif t == 'plan':
        if st.button(f'✅ Activate {a["n"]}-installment plan', key=f'pl{idx}', type='primary'):
            try:
                create_plan(uid, a['n'], u['email'])
                push_assistant('Your installment plan is active 🎉 Ask me *"when is my due date?"* any time to see it.')
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    elif t == 'receipt':
        p = payment_by_id(a['payment_id'])
        if p:
            pdf = receipt_pdf(u, p)
            if pdf:
                dl_btn('📄 Download receipt (PDF)', pdf, f'{p["transaction_id"]}.pdf', 'application/pdf', key=f'rc{idx}')
            else:
                dl_btn('📄 Download receipt', receipt_text(u, p), f'{p["transaction_id"]}.txt', 'text/plain', key=f'rc{idx}')


def assistant_tab(u, rec):
    top = st.columns([4, 1])
    top[0].subheader('🤖 AI Fee Assistant')
    if top[1].button('🧹 Clear chat'):
        st.session_state.chat, st.session_state.chat_ctx = [], {}
        st.rerun()
    st.caption('Knows your live balance, due date, late fine and payment history. Understands typos and Hinglish. '
               + ('Claude AI fallback is ON.' if llm_available() else 'Rule-based + FAQ mode (add ANTHROPIC_API_KEY to enable Claude fallback).'))
    chat = st.session_state.chat
    if not chat:
        push_assistant(f'Hi {u["name"].split()[0]}! 👋 Ask me about your fee, due date, late fine, installments or payments.',
                       suggestions=['How much fee is pending?', 'When is my due date?', 'Split my fee into installments', 'What is my late fine?'])
        chat = st.session_state.chat
    last = len(chat) - 1
    for i, m in enumerate(chat):
        with st.chat_message(m['role']):
            st.markdown(m['content'])
            if m.get('table') is not None:
                df_show(m['table'])
            if i == last and m['role'] == 'assistant':
                render_action(m, u, rec, i)
                if m.get('log_id') and not m.get('fb'):
                    f1, f2, _ = st.columns([1, 1, 8])
                    for col, val, lab in ((f1, 1, '👍'), (f2, -1, '👎')):
                        if col.button(lab, key=f'fb{i}_{val}'):
                            execute('UPDATE chat_logs SET feedback=? WHERE id=?', (val, m['log_id']))
                            m['fb'] = val
                            st.rerun()
                sug = m.get('suggestions') or []
                if sug:
                    cols = st.columns(min(len(sug), 4))
                    for j, s in enumerate(sug[:4]):
                        if cols[j].button(s, key=f'sg{i}_{j}'):
                            st.session_state.pending_prompt = s
                            st.rerun()
    prompt = st.chat_input('Ask: How much fee is pending?  /  Pay 5000  /  Split my fee')
    prompt = prompt or st.session_state.pop('pending_prompt', None)
    if prompt:
        process_prompt(prompt, u['id'])
        st.rerun()


# ------------------------------------------------------------------ student dashboard
def student_dashboard(uid):
    rec = student_record(uid)
    u = rec['user']
    st.markdown(f'<div class="hero"><h2>👋 Welcome, {u["name"]}</h2><p>{u["student_id"]} • {u["course"]} • Semester {u["semester"]}</p></div>',
                unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Total Fee', money(rec['owed']))
    c2.metric('Paid', money(rec['paid']))
    c3.metric('Pending', money(rec['pending']))
    c4.metric('Status', rec['status'])
    if rec['owed'] > 0:
        st.progress(min(rec['paid'] / rec['owed'], 1.0), text=f'{rec["paid"] / rec["owed"] * 100:.0f}% of your fee paid')
    if rec['pending'] > 0 and rec['due']:
        what = f'installment {rec["next_installment"]["seq"]} ({money(rec["next_installment"]["amount"])})' if rec['next_installment'] else 'fee'
        if rec['status'] == 'OVERDUE':
            st.error(f'⚠️ Your {what} was due on {rec["due"].strftime("%d %B %Y")} ({-rec["days"]} days ago). '
                     f'Estimated late fine if paid today: {money(rec["late_fine_est"])}.')
        elif rec['status'] == 'DUE SOON':
            st.warning(f'🔔 Your {what} is due on {rec["due"].strftime("%d %B %Y")} ({rec["days"]} days left).')
        else:
            st.info(f'📅 Next due date: {rec["due"].strftime("%d %B %Y")}')

    tabs = st.tabs(['🤖 AI Assistant', '💳 Pay Fee', '🧾 Payment History', '🔔 Reminders', '👤 My Profile'])
    with tabs[0]:
        assistant_tab(u, rec)
    with tabs[1]:
        pay_tab(u, rec)
    with tabs[2]:
        history_tab(u)
    with tabs[3]:
        reminders_tab(u, rec)
    with tabs[4]:
        profile_tab(u)


def pay_tab(u, rec):
    uid = u['id']
    st.subheader('Pay Fee (Demo)')
    with st.expander('Fee breakdown'):
        bd = fee_breakdown(rec['total_fee'])
        df_show(pd.DataFrame([{'Component': k, 'Amount': money(v)} for k, v in bd.items()]))
        if rec['charges']:
            st.caption(f'Plus charges / adjustments on your account: {money(rec["charges"])}')
    if rec['pending'] <= 0:
        st.success('Your fee is fully paid. 🎉')
        return
    a, b, c = st.columns(3)
    a.metric('Pending', money(rec['pending']))
    b.metric('Late fine if paid today', money(rec['late_fine_est']))
    c.metric('Payable now', money(rec['payable_now']))

    st.markdown('#### Installment plan')
    plan = rec['plan']
    if plan:
        df_show(pd.DataFrame([{'#': i['seq'], 'Amount': money(i['amount']), 'Due date': i['due_date'].strftime('%d %b %Y'),
                               'Status': i['status'].title()} for i in plan['items']]))
        if btn('Cancel plan'):
            cancel_plan(uid, u['email'])
            st.rerun()
    elif get_setting('allow_installments', '1') == '1':
        mx = int(get_setting('max_installments', 4))
        n = st.radio('Split my pending fee into', list(range(2, mx + 1)), horizontal=True, format_func=lambda x: f'{x} monthly installments')
        st.caption(f'{n} × about {money(rec["pending"] / n)}. A late fine only applies if you miss an installment date.')
        if btn('Activate installment plan'):
            try:
                create_plan(uid, n, u['email'])
                st.success('Plan activated.')
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    st.markdown('#### Make a payment')
    default = rec['next_installment']['amount'] if rec['next_installment'] else rec['pending']
    amount = st.number_input('Payment amount (₹)', min_value=1.0, max_value=float(rec['payable_now']),
                             value=float(min(default, rec['payable_now'])), step=500.0)
    method = st.selectbox('Payment method', ['UPI Demo', 'Card Demo', 'Net Banking Demo'])
    if rec['late_fine_est']:
        st.warning(f'A late fine of {money(rec["late_fine_est"])} will be added to your account with this payment.')
    st.caption('Demonstration payment flow. Do not enter real card/UPI credentials.')
    if btn('Pay Now (Demo)', type='primary'):
        try:
            tx, fine = make_payment(uid, amount, method, u['email'])
            st.success(f'Payment successful. Transaction ID: {tx}')
            st.rerun()
        except ValueError as e:
            st.error(str(e))


def history_tab(u):
    st.subheader('Payment History')
    ps = q('SELECT * FROM payments WHERE user_id=? ORDER BY payment_date DESC,id DESC', (u['id'],))
    if not ps:
        st.info('No payments recorded.')
    for p in ps:
        c1, c2, c3, c4 = st.columns([2, 2, 1, 2])
        c1.write(f'**{p["payment_date"]}**\n\n{money(p["amount"])}')
        c2.write(f'{p["method"]}\n\n`{p["transaction_id"]}`')
        c3.success(p['status'])
        pdf = receipt_pdf(u, p)
        if pdf:
            with c4:
                dl_btn('Receipt (PDF)', pdf, f'{p["transaction_id"]}.pdf', 'application/pdf', key=f'h{p["id"]}')
        else:
            with c4:
                dl_btn('Receipt', receipt_text(u, p), f'{p["transaction_id"]}.txt', 'text/plain', key=f'h{p["id"]}')
    ch = q('SELECT created_at,kind,amount,note FROM charges WHERE user_id=? ORDER BY id DESC', (u['id'],))
    if ch:
        st.markdown('#### Charges & adjustments')
        df_show(pd.DataFrame([{'Date': r['created_at'][:10], 'Type': r['kind'].replace('_', ' ').title(), 'Amount': money(r['amount']), 'Note': r['note']} for r in ch]))


def reminders_tab(u, rec):
    st.subheader('Smart Reminder Center')
    msg = reminder_text(rec)
    (st.success if rec['pending'] <= 0 else st.warning)(msg)
    cur = u['pref_channel'] if u['pref_channel'] in CHANNELS else 'In-app'
    ch = st.selectbox('How should the college remind you?', CHANNELS, index=CHANNELS.index(cur))
    c1, c2 = st.columns(2)
    if c1.button('Save my preference'):
        execute('UPDATE users SET pref_channel=? WHERE id=?', (ch, u['id']))
        st.success('Preference saved. Automatic reminders will use this channel.')
    if rec['pending'] > 0 and c2.button('Send me a reminder now'):
        status = deliver_reminder(rec, ch, reminder_stage(rec) or 'MANUAL', msg)
        log(u['email'], 'REMINDER', f'{ch}: {status}')
        st.success(f'Reminder {status.lower()}.')
    rs = q('SELECT sent_at,reminder_type,channel,status,message FROM reminders WHERE user_id=? ORDER BY sent_at DESC LIMIT 15', (u['id'],))
    if rs:
        df_show(pd.DataFrame([dict(x) for x in rs]))


def profile_tab(u):
    st.subheader('My Profile')
    for k, v in (('Student ID', u['student_id']), ('Name', u['name']), ('Email', u['email']), ('Phone', u['phone'] or 'Not provided'),
                 ('Course', u['course']), ('Semester', u['semester'])):
        st.write(f'**{k}:** {v}')
    with st.expander('Change password'):
        with st.form('chpw'):
            old = st.text_input('Current password', type='password')
            new = st.text_input('New password (min 6)', type='password')
            new2 = st.text_input('Confirm new password', type='password')
            if st.form_submit_button('Update password'):
                if not verify_password(old, u['password_hash']):
                    st.error('Current password is incorrect.')
                elif len(new) < 6 or new != new2:
                    st.error('New password must be at least 6 characters and match the confirmation.')
                else:
                    execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(new), u['id']))
                    log(u['email'], 'CHANGE_PASSWORD', '')
                    st.success('Password updated.')


# ------------------------------------------------------------------ admin dashboard
def admin_dashboard():
    st.markdown('<div class="hero"><h2>🛠️ College Admin Dashboard</h2><p>Collections • risk • reminders • chatbot insights • settings</p></div>', unsafe_allow_html=True)
    df = all_students_df()
    if df.empty:
        st.info('No students yet.')
        return
    total, paid, pending = df['Total Fee'].sum(), df['Paid'].sum(), df['Pending'].sum()
    overdue = int((df['Status'] == 'OVERDUE').sum())
    a, b, c, d, e = st.columns(5)
    a.metric('Students', len(df))
    b.metric('Total Fees', money(total))
    c.metric('Collected', money(paid), f'{paid / total * 100 if total else 0:.0f}%')
    d.metric('Pending', money(pending))
    e.metric('Overdue students', overdue)
    if overdue:
        st.error(f'⚠️ {overdue} students have overdue fees.')

    tabs = st.tabs(['📊 Overview', '👥 Students', '💰 Fee Management', '📧 Reminders', '🎯 Risk & Insights',
                    '🤖 Assistant & Chat Insights', '❓ FAQ', '📑 Reports', '⚙️ Settings', '📝 Audit Log'])
    with tabs[0]:
        admin_overview(df)
    with tabs[1]:
        admin_students(df)
    with tabs[2]:
        admin_fees(df)
    with tabs[3]:
        admin_reminders()
    with tabs[4]:
        admin_risk(df)
    with tabs[5]:
        admin_chat_tab()
    with tabs[6]:
        admin_faq()
    with tabs[7]:
        admin_reports(df)
    with tabs[8]:
        admin_settings()
    with tabs[9]:
        logs = q('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 300')
        df_show(pd.DataFrame([dict(x) for x in logs]))


def admin_overview(df):
    c1, c2 = st.columns(2)
    with c1:
        st.subheader('Collected vs Pending by Course')
        g = df.groupby('Course')[['Paid', 'Pending']].sum()
        st.bar_chart(g)
    with c2:
        st.subheader('Fee Status Distribution')
        st.bar_chart(df['Status'].value_counts())
    tr = collection_trend(90)
    st.subheader('Daily Collection (last 90 days)')
    if tr.empty:
        st.info('No payments in this period.')
    else:
        st.line_chart(tr.set_index('d')['a'])
    st.subheader('Upcoming dues (next 7 days)')
    up = df[df['Status'] == 'DUE SOON'].sort_values('Days to Due')
    df_show(up[['Student ID', 'Name', 'Course', 'Pending', 'Due Date', 'Days to Due']] if not up.empty else pd.DataFrame({'Info': ['None']}))


def admin_students(df):
    c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
    search = c1.text_input('🔎 Search by ID, name, email or course')
    sf = c2.multiselect('Status', sorted(df['Status'].unique()))
    cf = c3.multiselect('Course', sorted(df['Course'].unique()))
    rf = c4.multiselect('Risk', ['High', 'Medium', 'Low', 'None'])
    f = df
    if search:
        f = f[f.astype(str).apply(lambda x: x.str.contains(search, case=False, na=False)).any(axis=1)]
    if sf:
        f = f[f['Status'].isin(sf)]
    if cf:
        f = f[f['Course'].isin(cf)]
    if rf:
        f = f[f['Risk'].isin(rf)]
    st.caption(f'{len(f)} students')
    df_show(f.drop(columns=['User ID']))

    st.subheader('Add New Student')
    with st.form('add_student'):
        n1, n2, n3 = st.columns(3)
        name, email, phone = n1.text_input('Name'), n2.text_input('Email'), n3.text_input('Phone')
        course = n1.selectbox('Course', COURSES, key='acourse')
        sem = n2.selectbox('Semester', list(range(1, 7)), key='asem')
        pw = n3.text_input('Temporary password', value='student123', type='password')
        totalfee = n1.number_input('Total fee (0 = use course default)', min_value=0.0, step=1000.0)
        due = n2.date_input('Due date', date.today() + timedelta(days=30))
        if st.form_submit_button('Add Student'):
            if not name.strip() or not email.strip():
                st.error('Name and email are required.')
            elif one('SELECT id FROM users WHERE lower(email)=lower(?)', (email.strip(),)):
                st.error('Email already exists.')
            else:
                sid = next_student_id()
                uid = execute('INSERT INTO users(student_id,name,email,phone,course,semester,role,password_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                              (sid, name.strip(), email.strip(), phone.strip(), course, sem, 'student', hash_password(pw), now_iso()))
                execute('INSERT INTO fees(user_id,academic_year,total_fee,due_date) VALUES(?,?,?,?)',
                        (uid, get_setting('academic_year'), totalfee or course_fee(course), due.isoformat()))
                log('ADMIN', 'ADD_STUDENT', sid)
                st.success(f'Added {sid}')
                st.rerun()

    st.subheader('Reset a student password')
    with st.form('reset_pw'):
        sid = st.selectbox('Student', df['Student ID'].tolist(), key='rp_sid')
        newpw = st.text_input('New temporary password', type='password')
        if st.form_submit_button('Reset password'):
            if len(newpw) < 6:
                st.error('Minimum 6 characters.')
            else:
                execute('UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE student_id=?', (hash_password(newpw), sid))
                log('ADMIN', 'RESET_PASSWORD', sid)
                st.success('Password reset and account unlocked.')


def admin_fees(df):
    sid = st.selectbox('Select student to manage', df['Student ID'].tolist(), key='fm_sid')
    user = one('SELECT * FROM users WHERE student_id=?', (sid,))
    rec = student_record(user['id'])
    f = rec['fee']
    st.write(f'**{user["name"]}** • Total {money(rec["owed"])} • Paid {money(rec["paid"])} • Pending {money(rec["pending"])} • {rec["status"]}'
             + (f' • Late fine estimate {money(rec["late_fine_est"])}' if rec['late_fine_est'] else ''))
    c1, c2 = st.columns(2)
    with c1:
        st.markdown('##### Fee settings')
        with st.form('fee_edit'):
            totalfee = st.number_input('Base fee', min_value=0.0, value=float(f['total_fee']) if f else 0.0, step=1000.0)
            due2 = st.date_input('Due date', date.fromisoformat(f['due_date']) if f else date.today())
            if st.form_submit_button('Save Fee Settings'):
                if f:
                    execute('UPDATE fees SET total_fee=?,due_date=? WHERE id=?', (totalfee, due2.isoformat(), f['id']))
                else:
                    execute('INSERT INTO fees(user_id,academic_year,total_fee,due_date) VALUES(?,?,?,?)', (user['id'], get_setting('academic_year'), totalfee, due2.isoformat()))
                log('ADMIN', 'UPDATE_FEE', sid)
                st.success('Fee settings updated.')
                st.rerun()
    with c2:
        st.markdown('##### Record manual payment')
        if rec['payable_now'] <= 0:
            st.success('Nothing pending.')
        else:
            with st.form('manual_payment'):
                amt = st.number_input('Amount', min_value=1.0, max_value=float(rec['payable_now']), value=float(min(rec['payable_now'], 5000)), step=500.0)
                method = st.selectbox('Method', ['Cash', 'Bank Transfer', 'UPI', 'Cheque', 'Online'])
                if st.form_submit_button('Record Payment'):
                    try:
                        tx, fine = make_payment(user['id'], amt, method, 'ADMIN', prefix='ADM')
                        st.success(f'Recorded {tx}')
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
    st.markdown('##### Concession / waiver / adjustment')
    with st.form('charge_form'):
        k1, k2, k3 = st.columns(3)
        kind = k1.selectbox('Type', ['Concession (reduce fee)', 'Waive late fine', 'Extra charge'])
        amt = k2.number_input('Amount (₹)', min_value=0.0, step=100.0)
        note = k3.text_input('Reason / note')
        if st.form_submit_button('Apply'):
            if kind.startswith('Waive'):
                if rec['fine_charged'] > 0:
                    add_charge(user['id'], 'CONCESSION', -rec['fine_charged'], note or 'Late fine waived', 'ADMIN')
                    st.success('Late fine waived.')
                    st.rerun()
                else:
                    st.info('No late fine has been charged to this student.')
            elif amt <= 0:
                st.error('Enter an amount.')
            else:
                add_charge(user['id'], 'CONCESSION' if kind.startswith('Concession') else 'ADJUSTMENT',
                           -amt if kind.startswith('Concession') else amt, note or kind, 'ADMIN')
                st.success('Applied.')
                st.rerun()
    ch = q('SELECT created_at,kind,amount,note FROM charges WHERE user_id=? ORDER BY id DESC', (user['id'],))
    if ch:
        df_show(pd.DataFrame([dict(x) for x in ch]))
    if rec['plan'] and btn('Cancel this student\'s installment plan'):
        cancel_plan(user['id'], 'ADMIN')
        st.rerun()


def admin_reminders():
    st.subheader('Automatic reminder engine')
    st.caption('Escalating reminders (7 days → 3 days → 1 day → due today → overdue every 3 days). Students who were already reminded '
               'for the same stage are skipped so nobody is spammed.')
    smtp = bool(get_secret('SMTP_HOST'))
    st.info('📧 Email: ' + ('SMTP configured — emails are really sent.' if smtp else 'SMTP not configured — emails are logged only (see README to enable).'))
    c1, c2 = st.columns(2)
    channel = c1.selectbox('Channel', ['Student preference'] + CHANNELS)
    stages = c2.multiselect('Stages', list(STAGE_LABEL), default=list(STAGE_LABEL), format_func=lambda s: STAGE_LABEL[s])
    force = st.checkbox('Ignore cooldown (send even if already reminded)')
    p1, p2 = st.columns(2)
    if p1.button('👁️ Preview recipients'):
        rows = run_auto_reminders(channel, stages, dry_run=True, force=force)
        st.session_state.rem_preview = rows
    if p2.button('🚀 Send now', type='primary'):
        rows = run_auto_reminders(channel, stages, dry_run=False, force=force, actor='ADMIN')
        st.session_state.rem_preview = None
        st.success(f'{len(rows)} reminders processed.')
        if rows:
            df_show(pd.DataFrame(rows).drop(columns=['Message']))
    prev = st.session_state.get('rem_preview')
    if prev is not None:
        st.write(f'**{len(prev)}** students would be reminded:')
        if prev:
            df_show(pd.DataFrame(prev))
    st.subheader('Reminder log')
    rs = q('SELECT r.sent_at,u.student_id,u.name,r.reminder_type,r.channel,r.status,r.message FROM reminders r JOIN users u ON u.id=r.user_id ORDER BY r.id DESC LIMIT 150')
    if rs:
        df_show(pd.DataFrame([dict(x) for x in rs]))
    with st.expander('Send a test email'):
        to = st.text_input('Send to')
        if st.button('Send test'):
            ok, status = send_email(to, 'Test - College Fee Portal', 'This is a test email from the fee portal.')
            (st.success if ok else st.warning)(status)


def admin_risk(df):
    st.subheader('Default-risk ranking')
    st.caption('Rule-based score (0-100) from days overdue, share of fee still unpaid, and how many recent reminders were sent without payment. '
               'Use it to decide who to call first. It is a transparent heuristic, not a trained model.')
    r = df[df['Risk Score'] > 0].sort_values('Risk Score', ascending=False)
    a, b, c = st.columns(3)
    a.metric('High risk', int((df['Risk'] == 'High').sum()))
    b.metric('Medium risk', int((df['Risk'] == 'Medium').sum()))
    c.metric('At-risk amount (High+Medium)', money(df[df['Risk'].isin(['High', 'Medium'])]['Pending'].sum()))
    show = r[['Student ID', 'Name', 'Course', 'Pending', 'Days to Due', 'Reminders (30d)', 'On Plan', 'Risk Score', 'Risk']].head(25)
    df_show(show)
    st.subheader('Payments by method')
    pm = q("SELECT method, SUM(amount) a FROM payments WHERE status='SUCCESS' GROUP BY method")
    if pm:
        st.bar_chart(pd.DataFrame([dict(x) for x in pm]).set_index('method')['a'])
    lf = one("SELECT COALESCE(SUM(amount),0) s FROM charges WHERE kind='LATE_FINE'")['s']
    st.metric('Late fines assessed so far', money(lf))


def admin_chat_tab():
    st.subheader('Ask the admin assistant')
    st.caption('Examples: "overdue students", "top 5 pending", "who is at risk?", "BCA pending", "collection summary", "due this week", "STU1005".')
    for m in st.session_state.admin_chat:
        with st.chat_message(m['role']):
            st.markdown(m['content'])
            if m.get('table') is not None:
                df_show(m['table'])
    p = st.chat_input('Ask about collections, defaulters, risk…', key='admin_input')
    if p:
        r = answer_admin(p)
        st.session_state.admin_chat += [{'role': 'user', 'content': p}, {'role': 'assistant', 'content': r.text, 'table': r.table}]
        st.rerun()
    st.divider()
    st.subheader('Student chatbot insights')
    tot = one('SELECT COUNT(*) c FROM chat_logs')['c']
    un = one('SELECT COUNT(*) c FROM chat_logs WHERE answered=0')['c']
    up = one('SELECT COUNT(*) c FROM chat_logs WHERE feedback=1')['c']
    down = one('SELECT COUNT(*) c FROM chat_logs WHERE feedback=-1')['c']
    a, b, c, d = st.columns(4)
    a.metric('Questions asked', tot)
    b.metric('Unanswered', un)
    c.metric('👍', up)
    d.metric('👎', down)
    it = q("SELECT COALESCE(intent,'unknown') i, COUNT(*) n FROM chat_logs GROUP BY intent ORDER BY n DESC LIMIT 10")
    if it:
        st.markdown('**Most common topics**')
        st.bar_chart(pd.DataFrame([dict(x) for x in it]).set_index('i')['n'])
    un_rows = q('SELECT id,question,created_at FROM chat_logs WHERE answered=0 ORDER BY id DESC LIMIT 20')
    if un_rows:
        st.markdown('**Unanswered questions → turn into FAQs**')
        sel = st.selectbox('Question', [f'{r["id"]}: {r["question"]}' for r in un_rows])
        qid = int(sel.split(':', 1)[0])
        qtext = sel.split(':', 1)[1].strip()
        with st.form('promote_faq'):
            ans = st.text_area('Answer to publish for students')
            cat = st.selectbox('Category', ['Fees', 'Payments', 'College', 'Scholarship', 'Exams', 'Hostel', 'General'])
            if st.form_submit_button('Add as FAQ'):
                if ans.strip():
                    execute('INSERT INTO faqs(question,answer,category) VALUES(?,?,?)', (qtext, ans.strip(), cat))
                    execute('UPDATE chat_logs SET answered=1 WHERE id=?', (qid,))
                    log('ADMIN', 'FAQ_FROM_CHAT', qtext)
                    st.success('FAQ added — the chatbot can now answer this.')
                    st.rerun()
                else:
                    st.error('Please write an answer.')


def admin_faq():
    st.subheader('College Knowledge Base')
    faqs = q('SELECT * FROM faqs ORDER BY category,question')
    df_show(pd.DataFrame([dict(x) for x in faqs]))
    cats = ['Fees', 'Payments', 'College', 'Scholarship', 'Exams', 'Hostel', 'General']
    with st.form('faq_add'):
        st.markdown('**Add FAQ**')
        question, answer = st.text_input('Question'), st.text_area('Answer')
        category = st.selectbox('Category', cats)
        if st.form_submit_button('Add FAQ') and question.strip() and answer.strip():
            execute('INSERT INTO faqs(question,answer,category) VALUES(?,?,?)', (question.strip(), answer.strip(), category))
            log('ADMIN', 'ADD_FAQ', question)
            st.rerun()
    if faqs:
        sel = st.selectbox('Edit / delete FAQ', [f'{x["id"]}: {x["question"]}' for x in faqs])
        row = one('SELECT * FROM faqs WHERE id=?', (int(sel.split(':', 1)[0]),))
        with st.form(f'faq_edit_{row["id"]}'):
            nq, na = st.text_input('Question', row['question']), st.text_area('Answer', row['answer'])
            nc = st.selectbox('Category', cats, index=cats.index(row['category']) if row['category'] in cats else len(cats) - 1)
            s1, s2 = st.columns(2)
            if s1.form_submit_button('Save changes'):
                execute('UPDATE faqs SET question=?,answer=?,category=? WHERE id=?', (nq, na, nc, row['id']))
                log('ADMIN', 'EDIT_FAQ', nq)
                st.rerun()
            if s2.form_submit_button('Delete FAQ'):
                execute('DELETE FROM faqs WHERE id=?', (row['id'],))
                log('ADMIN', 'DELETE_FAQ', row['question'])
                st.rerun()


def admin_reports(df):
    st.subheader('Download Reports')
    clean = df.drop(columns=['User ID'])
    pay = pd.DataFrame([dict(x) for x in q('SELECT u.student_id,u.name,p.amount,p.payment_date,p.transaction_id,p.method,p.status FROM payments p JOIN users u ON u.id=p.user_id ORDER BY p.id DESC')])
    rem = pd.DataFrame([dict(x) for x in q('SELECT u.student_id,u.name,r.sent_at,r.channel,r.stage,r.status,r.message FROM reminders r JOIN users u ON u.id=r.user_id ORDER BY r.id DESC')])
    chg = pd.DataFrame([dict(x) for x in q('SELECT u.student_id,u.name,c.kind,c.amount,c.note,c.created_at FROM charges c JOIN users u ON u.id=c.user_id ORDER BY c.id DESC')])
    c1, c2, c3 = st.columns(3)
    with c1:
        dl_btn('⬇️ Student fee CSV', clean.to_csv(index=False).encode(), 'student_fee_report.csv', 'text/csv')
    with c2:
        dl_btn('⬇️ Payments CSV', pay.to_csv(index=False).encode(), 'payments.csv', 'text/csv')
    with c3:
        dl_btn('⬇️ Full Excel workbook', excel_bytes({'Students': clean, 'Payments': pay, 'Reminders': rem, 'Charges': chg}),
               'fee_report.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def admin_settings():
    st.subheader('College settings & fee policy')
    with st.form('settings'):
        a, b = st.columns(2)
        name = a.text_input('College name', get_setting('college_name'))
        ay = b.text_input('Academic year', get_setting('academic_year'))
        c, d = st.columns(2)
        per_day = c.number_input('Late fine per overdue day (₹)', min_value=0.0, value=float(get_setting('late_fee_per_day')), step=10.0)
        cap = d.number_input('Maximum late fine (₹)', min_value=0.0, value=float(get_setting('late_fee_cap')), step=100.0)
        e, f = st.columns(2)
        allow = e.checkbox('Allow installment plans', value=get_setting('allow_installments') == '1')
        mx = f.number_input('Max installments', min_value=2, max_value=6, value=int(get_setting('max_installments')))
        hours = st.text_input('Office hours', get_setting('office_hours'))
        loc = st.text_input('Office location', get_setting('office_location'))
        g, h = st.columns(2)
        mail = g.text_input('Accounts email', get_setting('accounts_email'))
        phone = h.text_input('Accounts phone', get_setting('accounts_phone'))
        bd = st.text_area('Fee breakdown (JSON, percentages)', get_setting('fee_breakdown'))
        if st.form_submit_button('Save settings'):
            try:
                parsed = json.loads(bd)
                assert isinstance(parsed, dict) and all(isinstance(v, (int, float)) for v in parsed.values())
            except Exception:
                st.error('Fee breakdown must be JSON like {"Tuition": 70, "Exam": 30}.')
            else:
                for k, v in {'college_name': name, 'academic_year': ay, 'late_fee_per_day': per_day, 'late_fee_cap': cap,
                             'allow_installments': '1' if allow else '0', 'max_installments': int(mx), 'office_hours': hours,
                             'office_location': loc, 'accounts_email': mail, 'accounts_phone': phone, 'fee_breakdown': bd}.items():
                    set_setting(k, v)
                log('ADMIN', 'UPDATE_SETTINGS', '')
                st.success('Settings saved. The chatbot and reminders use them immediately.')
    st.subheader('Default fee per course (for new registrations)')
    with st.form('course_fees'):
        vals = {c: st.number_input(c, min_value=0.0, value=course_fee(c), step=1000.0, key=f'cf_{c}') for c in COURSES}
        if st.form_submit_button('Save course fees'):
            for c, v in vals.items():
                execute('INSERT INTO course_fees(course,total_fee) VALUES(?,?) ON CONFLICT(course) DO UPDATE SET total_fee=excluded.total_fee', (c, v))
            log('ADMIN', 'UPDATE_COURSE_FEES', '')
            st.success('Saved.')
    st.caption('Admin login, SMTP e-mail and the Claude API key are configured in Streamlit secrets (see README), never in the database.')


# ------------------------------------------------------------------ main
if not st.session_state.auth:
    login_page()
else:
    with st.sidebar:
        st.title('🎓 College Portal')
        st.write(f'**Role:** {st.session_state.role.title()}')
        if st.session_state.role == 'student':
            su = one('SELECT name FROM users WHERE id=?', (st.session_state.uid,))
            st.write(f'**{su["name"]}**' if su else '')
        st.caption('Claude AI: ' + ('connected ✅' if llm_available() else 'not configured'))
        st.divider()
        if btn('🚪 Logout', wide=True):
            actor = 'ADMIN' if st.session_state.role == 'admin' else (one('SELECT email FROM users WHERE id=?', (st.session_state.uid,)) or {'email': '?'})['email']
            log(actor, 'LOGOUT', 'User logout')
            st.session_state.clear()
            st.rerun()
    if st.session_state.role == 'student':
        if one('SELECT id FROM users WHERE id=?', (st.session_state.uid,)):
            student_dashboard(st.session_state.uid)
        else:
            st.session_state.clear()
            st.rerun()
    else:
        admin_dashboard()

st.divider()
st.caption('Prototype for college deployment • Demo data only • Replace demo credentials, policies and payment integration before real use.')
