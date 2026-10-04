"""Run the escalating reminder engine from the command line (for cron / Task Scheduler / a server).

    python send_reminders.py                      # student's preferred channel
    python send_reminders.py --channel Email      # force e-mail
    python send_reminders.py --dry-run            # preview only

Needs the same database and secrets (env vars SMTP_HOST, SMTP_USER, ... ) as the app.
NOTE: on Streamlit Community Cloud the local SQLite file is not shared with other machines, so use this
script on a server, or after moving the database to PostgreSQL/Supabase.
"""
import argparse
from db import init_db
from services import run_auto_reminders, CHANNELS

ap = argparse.ArgumentParser()
ap.add_argument('--channel', default='Student preference', choices=['Student preference'] + CHANNELS)
ap.add_argument('--dry-run', action='store_true')
ap.add_argument('--force', action='store_true', help='ignore the per-stage cooldown')
a = ap.parse_args()

init_db()
rows = run_auto_reminders(a.channel, dry_run=a.dry_run, force=a.force, actor='CRON')
for r in rows:
    print(f"{r['Student ID']:8} {r['Name']:20} {r['Stage']:22} {r['Channel']:16} {r['Status']}")
print(f"{len(rows)} reminder(s) {'previewed' if a.dry_run else 'processed'}.")
