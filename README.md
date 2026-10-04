# 🎓 AI-Powered Smart College Fee Management & Reminder Chatbot

Streamlit project: student fee portal + admin console + an advanced fee assistant.

## What's new compared with the first version
**Advanced chatbot**
- Understands typos and Hinglish/Marathi words ("pendng balanse", "kitna baki hai", "kadhi paryant")
- Pulls amounts out of messages: *"pay 5000"*, *"I want to pay 10k"*
- Remembers the conversation: "Can I split my fee?" → "3" → plan prepared; "balance?" → "yes" → payment prepared
- **Action buttons inside the chat**: Pay now (demo), Activate installment plan, Download receipt (PDF)
- Student-specific answers from live data: balance, due date, **late fine estimate**, installments, last payment, fee breakdown
- FAQ search with TF-IDF similarity over the admin-managed knowledge base
- Optional **Claude fallback** (set `ANTHROPIC_API_KEY`): answers only from the student's real facts + FAQs, never invents amounts or policies
- 👍/👎 feedback; unanswered questions are logged and admins can turn them into FAQs with one click
- **Admin assistant**: ask "overdue students", "top 5 pending", "who is at risk?", "BCA pending", "collection summary", "STU1005"

**Fee reminders**
- Escalating schedule: 7 days → 3 days → 1 day → due today → overdue (every 3 days), with duplicate protection
- Preview recipients before sending; per-student preferred channel (In-app / Email / SMS / WhatsApp)
- **Real e-mail** through SMTP when configured (otherwise logged safely)
- `send_reminders.py` for cron / scheduled runs

**Fees & payments**
- Late fine policy (₹/day with a cap, configurable) assessed once when paying after the due date
- Self-service **installment plans** (2–4 monthly installments, tracked automatically)
- Concessions, fine waivers and adjustments by admin; full charges ledger
- Professional **PDF receipts**

**Admin**
- KPIs, course-wise and trend charts, status filters, risk ranking (transparent rule-based score)
- Settings page (college info, fine policy, installments, fee breakdown, course fees)
- Multi-sheet Excel export, CSV exports, FAQ edit/delete, audit log

**Security & quality**
- Admin credentials come from secrets (no longer hard-coded); login lockout after 5 failed attempts (10 min)
- Students can change their password; admin can reset/unlock accounts
- Modular code (`db.py`, `services.py`, `chatbot.py`, `app.py`) + automated tests
- Demo due dates are relative to today, so the demo always shows paid / due-soon / overdue students
- Fixed: leftover `if False else` code in registration; new students now get their course fee instead of ₹0

## Project structure
```
app.py             Streamlit UI (login, student portal, admin console)
chatbot.py         Intent engine, FAQ retrieval, Claude fallback, admin assistant
services.py        Fee ledger, late fine, installments, reminders, e-mail, receipts, risk score
db.py              SQLite schema, migrations, demo data
send_reminders.py  Command-line reminder runner
tests/test_core.py Automated tests (python tests/test_core.py)
.streamlit/secrets.toml.example
```

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
python tests/test_core.py        # optional: run the tests
```

## Deploy on Streamlit Community Cloud (updates your existing link)
1. Replace the old files in your GitHub repo with these (keep `requirements.txt`; add the new `.py` files, `.gitignore`).
2. Commit and push. Streamlit Cloud redeploys automatically; your existing app URL shows the new version.
3. In the app's **Settings → Secrets**, paste the values from `.streamlit/secrets.toml.example` (at least `ADMIN_EMAIL` and `ADMIN_PASSWORD`).

## Demo accounts
Student: `ansh@example.com` / `student123` (all 30 seeded students use `student123`)
Admin: `admin@college.com` / `admin123` **only while `ADMIN_PASSWORD` is not set in secrets**.

## Try these in the chatbot
`how much fee is pending` · `what is my late fine` · `split my fee into 3 installments` · `pay 5000` · `show my last receipt` · `fee breakdown` · `kitna baki hai` · `office hours`

## Before real use
- **Database:** SQLite on Streamlit Cloud is temporary (data can reset when the app restarts/redeploys). Use PostgreSQL/Supabase for real data.
- **Payments:** the payment flow is a simulation. Real payments need a gateway (Razorpay / PayU / Cashfree) with webhooks.
- **SMS/WhatsApp:** only logged in this version; real sending needs an approved provider (e.g. Twilio, MSG91, WhatsApp Business API).
- **Policies:** the late-fine rate (₹50/day, max ₹2000), office hours and contact details are placeholders; set the real ones in Admin → Settings.
- **Privacy:** get the college's approval before storing real student data.
