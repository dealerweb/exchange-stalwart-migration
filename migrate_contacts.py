import requests
import sys
import re
import urllib3
import warnings
import time
warnings.filterwarnings('ignore')
from exchangelib import IMPERSONATION, Account, Credentials, Configuration, NTLM
from exchangelib.errors import ErrorServerBusy
from exchangelib.protocol import BaseProtocol
from requests.adapters import HTTPAdapter
from migrate_config import stw_email
urllib3.disable_warnings()

EXCHANGE_SERVER       = 'exchange.domain.com'
EXCHANGE_ADMIN        = 'administrator@domain.com'
EXCHANGE_PASS         = 'CHANGE_ME'
STALWART_HOST         = 'https://mail.domain.com'
DEFAULT_STALWART_PASS = 'CHANGE_ME'
MAX_RETRIES           = 5
RETRY_WAIT            = 30

def with_retry(func, retries=MAX_RETRIES, wait=RETRY_WAIT):
    for attempt in range(retries):
        try:
            return func()
        except ErrorServerBusy:
            print(f"  Exchange überlastet, warte {wait}s... (Versuch {attempt+1}/{retries})")
            time.sleep(wait)
        except Exception as e:
            if 'ServerBusy' in str(e) or 'server busy' in str(e).lower():
                print(f"  Exchange überlastet, warte {wait}s... (Versuch {attempt+1}/{retries})")
                time.sleep(wait)
            else:
                raise
    raise Exception(f"Max Retries ({retries}) erreicht")

class NoVerifyHTTPAdapter(HTTPAdapter):
    def send(self, *args, **kwargs):
        kwargs['verify'] = False
        return super().send(*args, **kwargs)

BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

def get_exchange_account(email):
    credentials = Credentials(username=EXCHANGE_ADMIN, password=EXCHANGE_PASS)
    config = Configuration(server=EXCHANGE_SERVER, credentials=credentials, auth_type=NTLM)
    return Account(primary_smtp_address=email, config=config, access_type=IMPERSONATION)

def fix_vcard(raw):
    raw_str = raw.decode('utf-8', errors='replace')
    if 'BEGIN:VCARD' not in raw_str:
        return None
    raw_str = re.sub(r'VERSION:\d+\.\d+', 'VERSION:3.0', raw_str)
    return raw_str.encode('utf-8')

def get_existing_uids(stalwart_user, stalwart_pass):
    existing = {}
    url = f"{STALWART_HOST}/dav/card/{stalwart_user}/default/"
    headers = {'Depth': '1', 'Content-Type': 'application/xml'}
    body = '''<?xml version="1.0" encoding="UTF-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop><D:getetag/></D:prop>
</D:propfind>'''
    resp = requests.request('PROPFIND', url, headers=headers, data=body,
                           auth=(stalwart_user, stalwart_pass), verify=False)
    if resp.status_code == 207:
        matches = re.findall(r'<D:href>([^<]+\.vcf)</D:href>', resp.text)
        for href in matches:
            uid = href.split('/')[-1].replace('.vcf', '')
            existing[uid] = href
    return existing

def delete_all_existing(stalwart_user, stalwart_pass, existing):
    print(f"  Lösche {len(existing)} vorhandene Kontakte...")
    for i, (uid, href) in enumerate(existing.items()):
        url = f"{STALWART_HOST}{href}"
        for attempt in range(10):
            try:
                resp = requests.delete(url, auth=(stalwart_user, stalwart_pass), verify=False)
                if resp.status_code == 429:
                    time.sleep(5)
                else:
                    break
            except Exception:
                time.sleep(1)
        if i > 0 and i % 50 == 0:
            time.sleep(1)
            print(f"  {i}/{len(existing)} gelöscht...")

def get_uid_from_vcard(raw_str):
    match = re.search(r'^UID:(.+)$', raw_str, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None

def ensure_uid_in_vcard(raw_str, fallback_uid):
    if not re.search(r'^UID:', raw_str, re.MULTILINE):
        raw_str = raw_str.replace('END:VCARD', f'UID:{fallback_uid}\r\nEND:VCARD')
    return raw_str

def migrate_contacts(exchange_email, stalwart_pass):
    stalwart_user = stw_email(exchange_email)

    print(f"\n{'='*50}")
    print(f"Migriere Kontakte: {exchange_email}")
    if stalwart_user != exchange_email:
        print(f"Stalwart: {stalwart_user}")
    print('='*50)

    account  = with_retry(lambda: get_exchange_account(exchange_email))
    existing = get_existing_uids(stalwart_user, stalwart_pass)
    total    = with_retry(lambda: account.contacts.all().count())
    print(f"Exchange: {total} Kontakte | Stalwart: {len(existing)} vorhanden")

    if existing:
        delete_all_existing(stalwart_user, stalwart_pass, existing)

    count     = 0
    skipped   = 0
    errors    = 0
    seen_uids = set()

    contacts = with_retry(lambda: list(account.contacts.all()))

    for i, contact in enumerate(contacts):
        try:
            if not hasattr(contact, 'mime_content') or not contact.mime_content:
                skipped += 1
                continue

            raw = contact.mime_content
            if isinstance(raw, str):
                raw = raw.encode('utf-8')

            raw = fix_vcard(raw)
            if raw is None:
                skipped += 1
                continue

            raw_str = raw.decode('utf-8', errors='replace')
            uid = get_uid_from_vcard(raw_str)
            if not uid:
                uid = f"contact-{i}-{stalwart_user.replace('@', '_')}"
                raw_str = ensure_uid_in_vcard(raw_str, uid)
                raw = raw_str.encode('utf-8')

            uid_safe = re.sub(r'[^a-zA-Z0-9_-]', '_', uid)

            if uid_safe in seen_uids:
                skipped += 1
                continue
            seen_uids.add(uid_safe)

            url = f"{STALWART_HOST}/dav/card/{stalwart_user}/default/{uid_safe}.vcf"

            for attempt in range(10):
                resp = requests.put(url, data=raw,
                                  headers={'Content-Type': 'text/vcard; charset=utf-8'},
                                  auth=(stalwart_user, stalwart_pass),
                                  verify=False)
                if resp.status_code == 429:
                    time.sleep(5)
                else:
                    break

            time.sleep(0.05)

            if resp.status_code in (200, 201, 204):
                count += 1
                if count % 50 == 0:
                    print(f"  {count}/{total} migriert...")
            else:
                errors += 1
                if errors <= 3:
                    print(f"  Fehler {resp.status_code}: {resp.text[:200]}")

        except ErrorServerBusy:
            print(f"  Exchange überlastet, warte {RETRY_WAIT}s...")
            time.sleep(RETRY_WAIT)
            errors += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  Fehler: {e}")

    print(f"\n✓ {count} migriert, {skipped} übersprungen, {errors} Fehler")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Verwendung: python3 migrate_contacts.py user@domain.com [passwort]")
        sys.exit(1)

    exchange_email = sys.argv[1]
    stalwart_pass  = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STALWART_PASS

    migrate_contacts(exchange_email, stalwart_pass)
