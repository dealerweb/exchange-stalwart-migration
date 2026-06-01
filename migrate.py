import subprocess
import sys
import csv
import os

# Resolve the per-type scripts relative to this wrapper, so the toolkit works
# from any directory (not just /tmp). The individual scripts remain standalone.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CSV_FILE = sys.argv[1] if len(sys.argv) > 1 else 'users.csv'
DEFAULT_PASS = 'CHANGE_ME'

MIGRATE_MAIL     = os.path.join(SCRIPT_DIR, 'migrate_emails.py')
MIGRATE_CALENDAR = os.path.join(SCRIPT_DIR, 'migrate_calendar.py')
MIGRATE_CONTACTS = os.path.join(SCRIPT_DIR, 'migrate_contacts.py')

def run_script(script, user_email, stalwart_pass):
    cmd = ['python3', script, user_email, stalwart_pass]
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0

def migrate_user(user_email, stalwart_pass):
    print(f"\n{'#'*60}")
    print(f"# Starte komplette Migration: {user_email}")
    print(f"{'#'*60}")

    print("\n[1/3] E-Mails...")
    run_script(MIGRATE_MAIL, user_email, stalwart_pass)

    print("\n[2/3] Kalender...")
    run_script(MIGRATE_CALENDAR, user_email, stalwart_pass)

    print("\n[3/3] Kontakte...")
    run_script(MIGRATE_CONTACTS, user_email, stalwart_pass)

    print(f"\n✓ Migration abgeschlossen: {user_email}")

if __name__ == '__main__':
    if not os.path.exists(CSV_FILE):
        print(f"CSV nicht gefunden: {CSV_FILE}")
        print("Verwendung: python3 migrate.py /pfad/zur/users.csv")
        sys.exit(1)

    print(f"Lese CSV: {CSV_FILE}")

    with open(CSV_FILE, newline='', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter=';')
        users = list(reader)

    print(f"Gefunden: {len(users)} User")

    for row in users:
        if not row or not row[0].strip():
            continue

        user_email    = row[0].strip()
        stalwart_pass = row[1].strip() if len(row) > 1 and row[1].strip() else DEFAULT_PASS

        migrate_user(user_email, stalwart_pass)

    print(f"\n{'#'*60}")
    print("# Alle Migrationen abgeschlossen!")
    print(f"{'#'*60}")
