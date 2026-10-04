"""Advanced fee assistant.

Layers (in order):
  1. Intent engine  - typo-tolerant, English + Hinglish/Marathi keywords, entity extraction (amounts, installment counts)
  2. Multi-turn context - follow-ups like "yes", "3", "pay it"
  3. Action buttons - pay / split into installments / download receipt
  4. FAQ retrieval  - TF-IDF cosine similarity over the admin-managed knowledge base
  5. Optional LLM fallback (Claude) that is grounded ONLY on the student's real account facts + FAQs
All money figures always come from the database, never from the LLM.
"""
import re
import math
import difflib
from dataclasses import dataclass, field
from collections import Counter

import pandas as pd

from db import q, one, execute, get_setting, now_iso
from services import (money, get_secret, student_record, fee_breakdown, all_students_df, STAGE_LABEL)


@dataclass
class Reply:
    text: str
    intent: str = 'unknown'
    confidence: float = 0.0
    source: str = 'rules'            # rules | faq | llm | fallback
    suggestions: list = field(default_factory=list)
    action: dict | None = None       # {'type': 'pay'|'plan'|'receipt', ...}
    table: pd.DataFrame | None = None
    answered: bool = True


# ------------------------------------------------------------------ text helpers
_STOP = set('a an the is are am was were be to of for in on at my me i you your can could would please tell show what '
            'how do does did it this that and or with about any there our us we'.split())


def normalize(t):
    t = t.lower().replace('₹', ' rs ')
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\u0900-\u097f.,\s]', ' ', t)).strip()


def tokens(t):
    return [w for w in re.findall(r'[a-z0-9\u0900-\u097f]+', normalize(t))]


_VOCAB = set()   # filled from the keyword table below


def fix_typos(t):
    out = []
    for w in tokens(t):
        if len(w) >= 4 and w not in _VOCAB and not w.isdigit():
            m = difflib.get_close_matches(w, _VOCAB, n=1, cutoff=0.82)
            out.append(m[0] if m else w)
        else:
            out.append(w)
    return ' '.join(out)


def parse_amount(text):
    """'pay 5000', 'rs 5,000', '10k', '₹2.5k' -> float or None."""
    t = normalize(text).replace(',', '')
    m = re.search(r'(\d+(?:\.\d+)?)\s*(k|thousand|lakh|lac)?\b', t)
    if not m:
        return None
    v = float(m.group(1))
    mult = {'k': 1000, 'thousand': 1000, 'lakh': 100000, 'lac': 100000}.get(m.group(2) or '', 1)
    v *= mult
    return v if v > 0 else None


_NUMWORDS = {'two': 2, 'three': 3, 'four': 4, 'five': 5, '2': 2, '3': 3, '4': 4, '5': 5}


def parse_count(text):
    for w in tokens(text):
        if w in _NUMWORDS:
            return _NUMWORDS[w]
    return None


# ------------------------------------------------------------------ intent table
# (phrase, weight). Phrases match as substrings of the typo-corrected text; weight 3 = strong signal.
INTENTS = {
    'greeting': [('hello', 3), ('hi', 3), ('hey', 3), ('good morning', 3), ('good afternoon', 3), ('good evening', 3), ('namaste', 3), ('namaskar', 3)],
    'thanks': [('thanks', 3), ('thank you', 3), ('thx', 3), ('dhanyavad', 3), ('shukriya', 3), ('great', 1)],
    'bye': [('bye', 3), ('goodbye', 3), ('see you', 3)],
    'help': [('help', 2), ('who are you', 3), ('what are you', 3), ('what can you do', 3), ('options', 2), ('menu', 2), ('features', 2)],
    'summary': [('summary', 3), ('my fee status', 3), ('fee status', 3), ('account status', 3), ('overview', 2), ('status', 1)],
    'balance': [('pending', 3), ('balance', 3), ('remaining', 3), ('outstanding', 3), ('due amount', 3), ('how much', 1),
                ('owe', 3), ('have to pay', 3), ('need to pay', 3), ('should i pay', 3), ('kitna baki', 3), ('baki', 2), ('baaki', 2), ('kiti baki', 3), ('shilak', 2), ('bharaych', 2), ('bharna', 2)],
    'total_fee': [('total fee', 3), ('total fees', 3), ('full fee', 3), ('annual fee', 3), ('fee amount', 2), ('how much is my fee', 3), ('kitni fees', 3)],
    'paid_amount': [('how much paid', 3), ('amount paid', 3), ('paid so far', 3), ('already paid', 3), ('total paid', 3), ('kitna bhara', 3), ('kiti bharle', 3)],
    'due_date': [('due date', 3), ('last date', 3), ('deadline', 3), ('when to pay', 3), ('when is', 1), ('kab tak', 3), ('kadhi paryant', 3),
                 ('next installment', 2), ('by when', 3), ('till when', 3)],
    'late_fee': [('late fee', 3), ('late fine', 3), ('fine', 2), ('penalty', 3), ('late charge', 3), ('late payment', 2), ('overdue charge', 3), ('dand', 2)],
    'payment_history': [('payment history', 3), ('my payments', 3), ('past payments', 3), ('transactions', 3), ('transaction', 2), ('previous payment', 3)],
    'last_payment': [('last payment', 3), ('latest payment', 3), ('recent payment', 3), ('last paid', 3)],
    'receipt': [('receipt', 3), ('invoice', 2), ('payment proof', 3), ('download', 1), ('pavti', 3)],
    'how_to_pay': [('how to pay', 3), ('how can i pay', 3), ('how do i pay', 3), ('payment method', 3), ('payment process', 3), ('ways to pay', 3), ('upi', 1), ('kaise bharu', 3)],
    'pay_now': [('pay now', 3), ('pay', 2), ('make payment', 3), ('pay fee', 3), ('pay my fee', 3), ('i want to pay', 3), ('bhar do', 2), ('pay it', 3)],
    'installment': [('installment', 3), ('instalment', 3), ('emi', 3), ('split', 3), ('part payment', 3), ('partial', 2), ('in parts', 3), ('hafta', 2), ('divide my fee', 3)],
    'fee_breakdown': [('breakdown', 3), ('break down', 3), ('components', 3), ('what is included', 3), ('tuition', 2), ('fee structure', 3), ('fee details', 2)],
    'profile': [('who am i', 3), ('my profile', 3), ('student id', 3), ('my details', 3), ('my course', 2), ('my semester', 2), ('my name', 2)],
    'contact': [('contact', 3), ('phone number', 3), ('email', 2), ('call', 1), ('talk to', 2), ('speak to', 2), ('human', 2), ('staff', 2), ('accounts office', 2), ('complaint', 3)],
    'office_hours': [('office hours', 3), ('office timing', 3), ('office time', 3), ('timings', 3), ('open', 1), ('working hours', 3), ('where is', 2), ('location', 2), ('accounts department', 3)],
    'scholarship': [('scholarship', 3), ('concession', 3), ('waiver', 3), ('discount', 2), ('fee relief', 3), ('financial aid', 3)],
}
SUGGESTIONS = {
    'default': ['How much fee is pending?', 'When is my due date?', 'Split my fee into installments', 'Show my payment history'],
}
for _kw in INTENTS.values():
    for _p, _w in _kw:
        _VOCAB.update(w for w in _p.split() if len(w) >= 4)
_VOCAB.update(['pending', 'balance', 'payment', 'receipt', 'deadline', 'installment', 'scholarship', 'history', 'remaining',
               'outstanding', 'transaction', 'concession', 'breakdown'])


def classify(text):
    """Return (intent, confidence 0-1) using weighted phrase matching on typo-corrected text."""
    fixed = ' ' + fix_typos(text) + ' '
    words = set(fixed.split())
    scores = {}
    for intent, phrases in INTENTS.items():
        s = 0
        for p, w in phrases:
            hit = (f' {p} ' in fixed) if ' ' in p else (p in words)
            if hit:
                s += w + (0.5 if ' ' in p else 0)   # multi-word phrases are more specific
        if s:
            scores[intent] = s
    if not scores:
        return None, 0.0
    # specific beats generic: late_fee/installment/receipt/etc. outrank balance and pay_now
    best = max(scores, key=lambda k: scores[k])
    # "how can I pay my fee?" is a question about the process, not a request to pay
    if best == 'pay_now' and 'how_to_pay' in scores and (' how ' in fixed or ' kaise ' in fixed):
        best = 'how_to_pay'
    conf = min(scores[best] / 4.0, 1.0)
    return best, conf


# ------------------------------------------------------------------ FAQ retrieval (TF-IDF)
def _toks(t):
    return [w for w in tokens(t) if w not in _STOP and len(w) > 1]


def faq_search(question, top=3):
    faqs = q('SELECT * FROM faqs')
    if not faqs:
        return []
    docs = [_toks(f['question'] + ' ' + f['question'] + ' ' + f['answer'] + ' ' + f['category']) for f in faqs]
    n = len(docs)
    df = Counter(w for d in docs for w in set(d))
    idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}

    def vec(ws):
        tf = Counter(ws)
        return {w: (1 + math.log(c)) * idf.get(w, 1.0) for w, c in tf.items()}

    def cos(a, b):
        num = sum(a[w] * b.get(w, 0) for w in a)
        den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
        return num / den if den else 0.0

    qv = vec([w for w in fix_typos(question).split() if w not in _STOP])
    scored = sorted(((cos(qv, vec(d)), f) for d, f in zip(docs, faqs)), key=lambda x: -x[0])
    return [(s, f) for s, f in scored[:top] if s > 0]


# ------------------------------------------------------------------ intent handlers (student)
def _plan_text(rec):
    p = rec['plan']
    if not p:
        return ''
    lines = [f'• #{i["seq"]}: {money(i["amount"])} by {i["due_date"].strftime("%d %b %Y")} — {i["status"].title()}' for i in p['items']]
    return '\n'.join(lines)


def _due_phrase(rec):
    if not rec['due']:
        return 'not configured yet'
    d = rec['days']
    when = 'today' if d == 0 else (f'in {d} day(s)' if d > 0 else f'{-d} day(s) ago')
    return f'**{rec["due"].strftime("%d %B %Y")}** ({when})'


def handle_student(intent, text, rec, ctx):
    u = rec['user']
    sug = SUGGESTIONS['default']
    pending, fine = rec['pending'], rec['late_fine_est']

    if intent == 'greeting':
        return Reply(f'Hi {u["name"].split()[0]}! 👋 I can check your balance, due date and late fine, split your fee into installments, '
                     f'help you pay, and answer college fee questions.', suggestions=sug)
    if intent == 'thanks':
        return Reply("You're welcome! Anything else I can help with?", suggestions=sug)
    if intent == 'bye':
        return Reply('Bye! Pay on time and avoid late fines 😊')
    if intent == 'help':
        return Reply('Try things like:\n• *How much fee is pending?*\n• *What is my late fine?*\n• *Split my fee into 3 installments*\n'
                     '• *Pay 5000*\n• *Show my last receipt*\n• *Fee breakdown*\n• *Office hours*\n\nHinglish works too: *"kitna baki hai?"*', suggestions=sug)

    if intent == 'summary' or intent == 'balance':
        if rec['status'] == 'NOT ASSIGNED':
            return Reply('No fee is assigned to your account yet. Please contact the accounts office.', suggestions=['Contact accounts office'])
        if pending <= 0:
            return Reply(f'🎉 Your fee is **fully paid** ({money(rec["paid"])} of {money(rec["owed"])}). No dues!')
        txt = (f'Total fee: **{money(rec["owed"])}** · Paid: **{money(rec["paid"])}** · Pending: **{money(pending)}**\n\n'
               f'Next due date: {_due_phrase(rec)}.')
        if rec['next_installment']:
            txt += f'\n\nYou are on an installment plan — next installment **{money(rec["next_installment"]["amount"])}**.'
        if fine:
            txt += f'\n\n⚠️ You are overdue. A late fine of about **{money(fine)}** will be added when you pay.'
        return Reply(txt, action={'type': 'pay', 'amount': rec['next_installment']['amount'] if rec['next_installment'] else pending},
                     suggestions=['Split my fee into installments', 'What is my late fine?'])

    if intent == 'total_fee':
        return Reply(f'Your total fee for {rec["fee"]["academic_year"] if rec["fee"] else "this year"} is **{money(rec["owed"])}**'
                     f'{" (includes " + money(rec["charges"]) + " in charges/adjustments)" if rec["charges"] else ""}.', suggestions=['Fee breakdown'])
    if intent == 'paid_amount':
        return Reply(f'You have paid **{money(rec["paid"])}** so far. Pending: **{money(pending)}**.', suggestions=['Show my payment history'])

    if intent == 'due_date':
        if pending <= 0:
            return Reply('You have no pending fee, so nothing is due. ✅')
        txt = f'Your next due date is {_due_phrase(rec)}.'
        if rec['plan']:
            txt += '\n\nYour installment plan:\n' + _plan_text(rec)
        return Reply(txt, suggestions=['How much fee is pending?'])

    if intent == 'late_fee':
        rate, cap = get_setting('late_fee_per_day'), get_setting('late_fee_cap')
        base = f'Policy: **₹{rate} per overdue day**, up to a maximum of **₹{cap}**, added when you pay after the due date.'
        if rec['fine_charged']:
            return Reply(f'{base}\n\nA late fine of **{money(rec["fine_charged"])}** has already been added to your account.')
        if fine:
            return Reply(f'{base}\n\nYou are {-rec["days"]} day(s) overdue, so your estimated late fine is **{money(fine)}**. '
                         f'Paying the full amount ({money(rec["payable_now"])}) clears everything.',
                         action={'type': 'pay', 'amount': rec['payable_now']}, suggestions=['Split my fee into installments'])
        return Reply(f'{base}\n\nYou currently have **no late fine** — pay before the due date to keep it that way.')

    if intent == 'payment_history' or intent == 'last_payment':
        ps = q('SELECT * FROM payments WHERE user_id=? ORDER BY payment_date DESC,id DESC LIMIT 5', (u['id'],))
        if not ps:
            return Reply('No payments are recorded yet.', suggestions=['How can I pay?'])
        df = pd.DataFrame([{'Date': p['payment_date'], 'Amount': money(p['amount']), 'Method': p['method'],
                            'Transaction ID': p['transaction_id']} for p in ps])
        head = (f'Your last payment was **{money(ps[0]["amount"])}** on {ps[0]["payment_date"]} (`{ps[0]["transaction_id"]}`).'
                if intent == 'last_payment' else f'Here are your {len(ps)} most recent payments:')
        return Reply(head, table=df, action={'type': 'receipt', 'payment_id': ps[0]['id']})
    if intent == 'receipt':
        p = one('SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1', (u['id'],))
        if not p:
            return Reply('You have no payments yet, so there is no receipt to download.')
        return Reply('Here is your latest receipt. Older receipts are in the **Payment History** tab.',
                     action={'type': 'receipt', 'payment_id': p['id']})

    if intent == 'how_to_pay':
        return Reply('Open the **Pay Fee** tab, choose the amount and method (UPI / Card / Net banking) and confirm. '
                     'You can also just tell me *"pay 5000"* and I will prepare the payment. '
                     '*(Demo mode — no real money moves.)*', suggestions=['Pay now', 'Split my fee into installments'])

    if intent == 'pay_now':
        if pending <= 0:
            return Reply('Your fee is fully paid — nothing to pay. ✅')
        amt = parse_amount(text)
        due_amt = rec['next_installment']['amount'] if rec['next_installment'] else pending
        amt = amt if amt else due_amt
        if amt < 100 and parse_amount(text):   # e.g. "pay 2 installments"
            amt = due_amt
        if amt > rec['payable_now']:
            return Reply(f'The most you can pay right now is **{money(rec["payable_now"])}**.',
                         action={'type': 'pay', 'amount': rec['payable_now']})
        extra = f' (a late fine of {money(fine)} will be added)' if fine else ''
        ctx['awaiting'] = None
        return Reply(f'Ready to pay **{money(amt)}**{extra}. Confirm below 👇', action={'type': 'pay', 'amount': amt})

    if intent == 'installment':
        if pending <= 0:
            return Reply('You have no pending fee to split. ✅')
        if rec['plan']:
            return Reply('You already have an active installment plan:\n' + _plan_text(rec), suggestions=['When is my due date?'])
        if get_setting('allow_installments', '1') != '1':
            return Reply('Installments are not enabled by the college right now. Please contact the accounts office.')
        n = parse_count(text)
        mx = int(get_setting('max_installments', 4))
        if n and 2 <= n <= mx:
            return _plan_reply(rec, n)
        ctx['awaiting'] = 'installment_n'
        per = {k: money(pending / k) for k in range(2, mx + 1)}
        opts = ' · '.join(f'{k} × {v}' for k, v in per.items())
        return Reply(f'Your pending fee is **{money(pending)}**. I can split it into 2–{mx} monthly installments: {opts}. '
                     f'How many would you like?', action={'type': 'plan_choices', 'max': mx}, suggestions=[f'{k} installments' for k in range(2, mx + 1)])

    if intent == 'fee_breakdown':
        bd = fee_breakdown(rec['total_fee'])
        df = pd.DataFrame([{'Component': k, 'Amount': money(v)} for k, v in bd.items()])
        return Reply(f'Here is how your base fee of **{money(rec["total_fee"])}** is divided (as configured by the college):', table=df)

    if intent == 'profile':
        return Reply(f'You are **{u["name"]}** — Student ID **{u["student_id"]}**, {u["course"]}, Semester {u["semester"]}.')
    if intent == 'contact':
        return Reply(f'📞 {get_setting("accounts_phone")}\n\n✉️ {get_setting("accounts_email")}\n\n🕘 {get_setting("office_hours")}\n\n📍 {get_setting("office_location")}')
    if intent == 'office_hours':
        return Reply(f'🕘 {get_setting("office_hours")}\n\n📍 {get_setting("office_location")}')
    if intent == 'scholarship':
        best = faq_search('fee concession documents scholarship', 1)
        ans = best[0][1]['answer'] if best else 'Please contact the accounts/scholarship office for concession rules.'
        return Reply(ans, source='faq', suggestions=['Contact accounts office'])
    return None


def _plan_reply(rec, n):
    pending = rec['pending']
    return Reply(f'Splitting **{money(pending)}** into **{n} monthly installments** of about {money(pending / n)} each. Confirm to activate the plan 👇',
                 intent='installment', confidence=1.0, action={'type': 'plan', 'n': n})


# ------------------------------------------------------------------ LLM fallback (optional)
def llm_available():
    return bool(get_secret('ANTHROPIC_API_KEY'))


def llm_answer(question, rec, history):
    """Grounded fallback. Returns text or None. Never raises."""
    key = get_secret('ANTHROPIC_API_KEY')
    if not key:
        return None
    try:
        import anthropic
        u = rec['user']
        faqs = faq_search(question, 3)
        facts = (f'Student: {u["name"]} ({u["student_id"]}), {u["course"]}, semester {u["semester"]}.\n'
                 f'Total fee {money(rec["owed"])}, paid {money(rec["paid"])}, pending {money(rec["pending"])}, status {rec["status"]}.\n'
                 f'Next due date: {rec["due"]}. Estimated late fine if paid today: {money(rec["late_fine_est"])}.\n'
                 f'Late fine policy: Rs {get_setting("late_fee_per_day")}/day up to Rs {get_setting("late_fee_cap")}.\n'
                 f'Office: {get_setting("office_hours")}; {get_setting("office_location")}; {get_setting("accounts_phone")}; {get_setting("accounts_email")}.\n'
                 + '\n'.join(f'FAQ: {f["question"]} -> {f["answer"]}' for _, f in faqs))
        system = ('You are the fee assistant of an Indian college. Answer ONLY from the FACTS below. Never invent amounts, dates, '
                  'policies or phone numbers; if the facts do not contain the answer, say you are not sure and suggest contacting the '
                  'accounts office. Be brief (max 4 sentences), friendly, and reply in the language the student used (English, Hindi, '
                  'Marathi or Hinglish). You cannot make payments yourself; tell the student to use the Pay Fee tab.\n\nFACTS:\n' + facts)
        msgs = [{'role': r, 'content': m} for r, m in history[-6:] if m]
        msgs.append({'role': 'user', 'content': question})
        # keep the conversation strictly alternating user/assistant starting with user
        clean = []
        for m in msgs:
            if clean and clean[-1]['role'] == m['role']:
                clean[-1]['content'] += '\n' + m['content']
            else:
                clean.append(m)
        while clean and clean[0]['role'] != 'user':
            clean.pop(0)
        client = anthropic.Anthropic(api_key=key, timeout=20)
        resp = client.messages.create(model=get_secret('ANTHROPIC_MODEL', 'claude-sonnet-4-6'), max_tokens=400, system=system, messages=clean)
        return ''.join(b.text for b in resp.content if getattr(b, 'text', None)).strip() or None
    except Exception:
        return None


# ------------------------------------------------------------------ public entry points
def log_chat(uid, question, reply: Reply):
    return execute('INSERT INTO chat_logs(user_id,question,intent,confidence,answered,source,created_at) VALUES(?,?,?,?,?,?,?)',
                   (uid, question[:500], reply.intent, reply.confidence, 1 if reply.answered else 0, reply.source, now_iso()))


def answer_student(question, uid, ctx=None, history=None):
    """Main entry. ctx is a mutable dict kept in session_state (multi-turn memory)."""
    ctx = ctx if ctx is not None else {}
    history = history or []
    rec = student_record(uid)
    qn = question.strip()
    low = normalize(qn)

    # ---- multi-turn follow-ups
    if ctx.get('awaiting') == 'installment_n':
        n = parse_count(qn)
        if n and 2 <= n <= int(get_setting('max_installments', 4)):
            ctx['awaiting'] = None
            return _plan_reply(rec, n)
        if low in ('no', 'nope', 'cancel', 'nahi', 'nako'):
            ctx['awaiting'] = None
            return Reply('No problem, I have not changed anything.', intent='installment')
    if low in ('yes', 'y', 'ok', 'okay', 'haan', 'ha', 'ho', 'sure') and ctx.get('last_intent') in ('balance', 'summary', 'late_fee') and rec['pending'] > 0:
        amt = rec['next_installment']['amount'] if rec['next_installment'] else rec['payable_now']
        return Reply(f'Great — ready to pay **{money(amt)}**. Confirm below 👇', intent='pay_now', confidence=1.0, action={'type': 'pay', 'amount': amt})

    # ---- intent engine
    intent, conf = classify(qn)
    # amount + number-only messages ("5000") right after a pay prompt
    if intent is None and re.fullmatch(r'(rs\.? ?)?\d[\d,]*(\.\d+)?k?', low) and ctx.get('last_intent') == 'pay_now':
        intent, conf = 'pay_now', 0.9
    if intent == 'pay_now' and conf < 1 and any(k in low for k in ('installment', 'split', 'emi')):
        intent = 'installment'
    if intent and conf >= 0.5:
        r = handle_student(intent, qn, rec, ctx)
        if r:
            r.intent, r.confidence = intent, conf
            ctx['last_intent'] = intent
            return r

    # ---- FAQ retrieval
    hits = faq_search(qn, 3)
    if hits and hits[0][0] >= 0.30:
        ctx['last_intent'] = 'faq'
        rel = [f['question'] for s, f in hits[1:] if s >= 0.2]
        return Reply(hits[0][1]['answer'], intent='faq', confidence=round(hits[0][0], 2), source='faq', suggestions=rel)

    # ---- weak intent match (e.g. single generic word)
    if intent:
        r = handle_student(intent, qn, rec, ctx)
        if r:
            r.intent, r.confidence = intent, conf
            ctx['last_intent'] = intent
            return r

    # ---- LLM (grounded) fallback
    txt = llm_answer(qn, rec, history)
    if txt:
        return Reply(txt, intent='llm', confidence=0.5, source='llm', suggestions=SUGGESTIONS['default'][:3])

    # ---- honest fallback: logged as unanswered so admin can add an FAQ
    return Reply("I'm not sure about that one yet — I've noted your question for the accounts office. "
                 f"You can reach them at {get_setting('accounts_phone')} / {get_setting('accounts_email')}.\n\n"
                 'Meanwhile I can help with balance, due date, late fine, installments, receipts and payments.',
                 intent='unknown', source='fallback', suggestions=SUGGESTIONS['default'], answered=False)


# ------------------------------------------------------------------ admin assistant
def _fmt_df(df, cols):
    out = df[cols].copy()
    for c in ('Pending', 'Paid', 'Total Fee', 'Late Fine Est.'):
        if c in out:
            out[c] = out[c].map(money)
    return out


def answer_admin(question):
    """Natural-language analytics over the live data (no LLM needed)."""
    df = all_students_df()
    t = fix_typos(question)
    low = normalize(question)
    if df.empty:
        return Reply('There are no students yet.')

    m = re.search(r'stu\d{3,}', low)
    if m:
        row = df[df['Student ID'].str.lower() == m.group(0)]
        if row.empty:
            return Reply(f'No student with ID {m.group(0).upper()}.')
        r = row.iloc[0]
        return Reply(f'**{r["Name"]}** ({r["Student ID"]}) — {r["Course"]}, Sem {r["Semester"]}\n\nTotal {money(r["Total Fee"])} · Paid {money(r["Paid"])} · '
                     f'Pending **{money(r["Pending"])}** · Status **{r["Status"]}** · Risk **{r["Risk"]}** ({r["Risk Score"]}/100)')
    for course in df['Course'].unique():
        if normalize(course) in low and any(k in low for k in ('pending', 'overdue', 'show', 'list', 'defaulter')):
            sub = df[(df['Course'] == course) & (df['Pending'] > 0)].sort_values('Pending', ascending=False)
            return Reply(f'{course}: **{len(sub)}** students with dues, total pending **{money(sub["Pending"].sum())}**.',
                         table=_fmt_df(sub, ['Student ID', 'Name', 'Pending', 'Due Date', 'Status']))
    if any(k in t for k in ('risk', 'likely', 'default')):
        sub = df[df['Risk Score'] >= 40].sort_values('Risk Score', ascending=False).head(10)
        return Reply(f'**{len(sub)}** students are at medium/high risk of not paying on time (rule-based score).',
                     table=sub[['Student ID', 'Name', 'Risk Score', 'Risk', 'Status']].assign(Pending=sub['Pending'].map(money)))
    if re.search(r'\btop\b|highest|largest|biggest', t):
        mn = re.search(r'\b(\d{1,2})\b', t)
        n = int(mn.group(1)) if mn else 5
        sub = df[df['Pending'] > 0].sort_values('Pending', ascending=False).head(n)
        return Reply(f'Top {len(sub)} students by pending amount (total **{money(sub["Pending"].sum())}**):',
                     table=_fmt_df(sub, ['Student ID', 'Name', 'Course', 'Pending', 'Due Date', 'Status']))
    if any(k in t for k in ('overdue', 'late', 'defaulter')):
        sub = df[df['Status'] == 'OVERDUE'].sort_values('Pending', ascending=False)
        return Reply(f'**{len(sub)}** students listed — total pending **{money(sub["Pending"].sum())}**.',
                     table=_fmt_df(sub, ['Student ID', 'Name', 'Course', 'Pending', 'Due Date', 'Status']))
    if any(k in t for k in ('due soon', 'this week', 'next 7', 'upcoming')):
        sub = df[df['Status'] == 'DUE SOON'].sort_values('Days to Due')
        return Reply(f'**{len(sub)}** students have a due date within 7 days.', table=_fmt_df(sub, ['Student ID', 'Name', 'Pending', 'Due Date']))
    if any(k in t for k in ('course', 'department', 'program')):
        g = df.groupby('Course', as_index=False)[['Total Fee', 'Paid', 'Pending']].sum()
        for c in ('Total Fee', 'Paid', 'Pending'):
            g[c] = g[c].map(money)
        return Reply('Course-wise collection:', table=g)
    if any(k in t for k in ('collection', 'collected', 'revenue', 'summary', 'total', 'overall')):
        total, paid, pend = df['Total Fee'].sum(), df['Paid'].sum(), df['Pending'].sum()
        today_amt = one("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE payment_date=date('now','localtime')")['s']
        return Reply(f'Collected **{money(paid)}** of **{money(total)}** ({paid / total * 100 if total else 0:.0f}%). Pending **{money(pend)}**. '
                     f'Collected today: **{money(today_amt)}**. Overdue students: **{int((df["Status"] == "OVERDUE").sum())}**.')
    if any(k in t for k in ('paid', 'cleared', 'full')):
        n = int((df['Status'] == 'PAID').sum())
        return Reply(f'**{n}** of {len(df)} students have fully paid.')
    if any(k in t for k in ('unanswered', 'question', 'chat', 'bot')):
        n = one('SELECT COUNT(*) c FROM chat_logs WHERE answered=0')['c']
        tot = one('SELECT COUNT(*) c FROM chat_logs')['c']
        return Reply(f'Students asked the chatbot **{tot}** questions; **{n}** could not be answered. See *Chatbot Insights* to turn them into FAQs.')
    if any(k in t for k in ('reminder', 'reminded')):
        n = one("SELECT COUNT(*) c FROM reminders WHERE sent_at>=date('now','localtime')")['c']
        return Reply(f'**{n}** reminders were generated today.')
    return Reply('Ask me things like: *"overdue students"*, *"top 5 pending"*, *"who is at risk?"*, *"BCA pending"*, *"collection summary"*, '
                 '*"due this week"*, *"course wise"* or a student ID like *STU1005*.', answered=False)
