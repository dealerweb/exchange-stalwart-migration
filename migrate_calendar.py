import requests
import sys
import re
import urllib3
import warnings
import time
from datetime import datetime, timezone
warnings.filterwarnings('ignore')
from exchangelib import IMPERSONATION, Account, Credentials, Configuration, NTLM, EWSDateTime, EWSTimeZone, ItemId
from exchangelib.errors import ErrorServerBusy
from exchangelib.winzone import MS_TIMEZONE_TO_IANA_MAP
from exchangelib.protocol import BaseProtocol
from requests.adapters import HTTPAdapter
from migrate_config import stw_email
urllib3.disable_warnings()

MS_TIMEZONE_TO_IANA_MAP['Customized Time Zone'] = 'Europe/Berlin'
MS_TIMEZONE_TO_IANA_MAP[''] = 'Europe/Berlin'

EXCHANGE_SERVER       = 'exchange.domain.com'
EXCHANGE_ADMIN        = 'administrator@domain.com'
EXCHANGE_PASS         = 'CHANGE_ME'
STALWART_HOST         = 'https://mail.domain.com'
DEFAULT_STALWART_PASS = 'CHANGE_ME'
MAX_RETRIES           = 5
RETRY_WAIT            = 30
FETCH_CHUNK           = 50

# Calendars with many events are slow to migrate - every event is fetched from
# Exchange and uploaded individually via CalDAV, so a large calendar can take a
# very long time. Recommended strategy: migrate the CURRENT YEAR + FUTURE first
# (the default window below) so users have their upcoming appointments quickly,
# then migrate older history afterwards in separate passes by widening this
# window (e.g. SYNC_FROM=2016, SYNC_TO=2025).
SYNC_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)   # start of sync window
SYNC_TO   = datetime(2099, 1, 1, tzinfo=timezone.utc)   # end of sync window

def with_retry(func, retries=MAX_RETRIES, wait=RETRY_WAIT):
    for attempt in range(retries):
        try:
            return func()
        except ErrorServerBusy:
            print(f"\n  Exchange überlastet, warte {wait}s... (Versuch {attempt+1}/{retries})")
            time.sleep(wait)
        except Exception as e:
            if 'ServerBusy' in str(e) or 'server busy' in str(e).lower():
                print(f"\n  Exchange überlastet, warte {wait}s... (Versuch {attempt+1}/{retries})")
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

def fix_ical(raw, user_email):
    raw_str = raw.decode('utf-8', errors='replace')
    raw_str = raw_str.replace('METHOD:REQUEST', 'METHOD:PUBLISH')
    raw_str = re.sub(
        r'ORGANIZER[^\r\n]*' + re.escape(user_email) + r'[^\r\n]*(?:\r?\n[ \t][^\r\n]*)*\r?\n',
        '', raw_str, flags=re.IGNORECASE
    )
    raw_str = re.sub(
        r'ATTENDEE[^\r\n]*' + re.escape(user_email) + r'[^\r\n]*(?:\r?\n[ \t][^\r\n]*)*\r?\n',
        '', raw_str, flags=re.IGNORECASE
    )
    return raw_str.encode('utf-8')

def get_stalwart_uids_in_range(stalwart_user, stalwart_pass):
    existing = {}
    url = f"{STALWART_HOST}/dav/cal/{stalwart_user}/default/"
    headers = {'Depth': '1', 'Content-Type': 'application/xml'}
    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{SYNC_FROM.strftime('%Y%m%dT%H%M%SZ')}"
                      end="{SYNC_TO.strftime('%Y%m%dT%H%M%SZ')}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>'''
    resp = requests.request('REPORT', url, headers=headers, data=body,
                           auth=(stalwart_user, stalwart_pass), verify=False)
    if resp.status_code == 207:
        matches = re.findall(r'<D:href>([^<]+\.ics)</D:href>', resp.text)
        for href in matches:
            uid = href.split('/')[-1].replace('.ics', '')
            existing[uid] = href
    return existing

def delete_event(stalwart_user, stalwart_pass, href):
    url = f"{STALWART_HOST}{href}"
    for attempt in range(10):
        try:
            resp = requests.delete(url, auth=(stalwart_user, stalwart_pass), verify=False)
            if resp.status_code == 429:
                time.sleep(5)
            else:
                return
        except Exception:
            time.sleep(1)

def upload_item(full_item, uid_map, exchange_email, stalwart_user, stalwart_pass, stalwart_uids, count, errors, missing):
    uid = uid_map.get(full_item.id)
    if not uid:
        return count, errors + 1

    if not hasattr(full_item, 'mime_content') or not full_item.mime_content:
        return count, errors + 1

    raw = full_item.mime_content
    if isinstance(raw, str):
        raw = raw.encode('utf-8')

    raw = fix_ical(raw, exchange_email)
    url = f"{STALWART_HOST}/dav/cal/{stalwart_user}/default/{uid}.ics"

    for attempt in range(10):
        resp = requests.put(url, data=raw,
                          headers={'Content-Type': 'text/calendar; charset=utf-8'},
                          auth=(stalwart_user, stalwart_pass),
                          verify=False)
        if resp.status_code == 429:
            time.sleep(5)
        else:
            break

    time.sleep(0.05)

    if resp.status_code in (200, 201, 204):
        count += 1
        stalwart_uids[uid] = f"/dav/cal/{stalwart_user}/default/{uid}.ics"
    else:
        errors += 1
        if errors <= 3:
            print(f"\n  Fehler {resp.status_code}: {resp.text[:200]}")

    print(f"  {count}/{missing} importiert, {errors} Fehler...          ", end='\r', flush=True)
    return count, errors

def migrate_calendar(exchange_email, stalwart_pass):
    stalwart_user = stw_email(exchange_email)

    print(f"\n{'='*50}")
    print(f"Migriere Kalender: {exchange_email}")
    if stalwart_user != exchange_email:
        print(f"Stalwart: {stalwart_user}")
    print(f"Zeitraum: {SYNC_FROM.strftime('%d.%m.%Y')} - {SYNC_TO.strftime('%d.%m.%Y')}")
    print('='*50)

    account = with_retry(lambda: get_exchange_account(exchange_email))

    print(f"  Lade vorhandene Termine von Stalwart (im Zeitraum)...")
    stalwart_uids = get_stalwart_uids_in_range(stalwart_user, stalwart_pass)
    print(f"  Stalwart: {len(stalwart_uids)} Termine im Zeitraum vorhanden")

    ews_cutoff = EWSDateTime.from_datetime(SYNC_FROM)
    ews_end    = EWSDateTime.from_datetime(SYNC_TO)

    print(f"  Lade UIDs von Exchange...")
    uid_items = []
    for item in account.calendar.all().filter(start__gte=ews_cutoff, start__lte=ews_end).only('uid'):
        uid_items.append(item)
        if len(uid_items) % 500 == 0:
            print(f"  {len(uid_items)} UIDs geladen...")

    exchange_uids = set()
    missing_items = []
    seen_uids     = set()

    for item in uid_items:
        try:
            if not item.uid:
                continue
            uid = item.uid.replace('/', '_').replace('@', '_')
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            exchange_uids.add(uid)
            if uid not in stalwart_uids:
                missing_items.append((item.id, item.changekey, uid))
        except Exception:
            pass

    total   = len(exchange_uids)
    missing = len(missing_items)
    print(f"Exchange: {total} Termine | {missing} neu zu importieren")

    count   = 0
    errors  = 0
    deleted = 0

    if missing_items:
        item_ids = [ItemId(id=i[0], changekey=i[1]) for i in missing_items]
        uid_map  = {i[0]: i[2] for i in missing_items}

        for i in range(0, len(item_ids), FETCH_CHUNK):
            chunk = item_ids[i:i+FETCH_CHUNK]
            try:
                for full_item in with_retry(lambda: list(account.fetch(ids=chunk))):
                    try:
                        count, errors = upload_item(full_item, uid_map, exchange_email, stalwart_user, stalwart_pass, stalwart_uids, count, errors, missing)
                    except Exception as e:
                        errors += 1
                        if errors <= 3:
                            print(f"\n  Fehler: {e}")
            except Exception as e:
                print(f"\n  Chunk-Fehler, versuche einzeln...")
                for single_id in chunk:
                    try:
                        for full_item in account.fetch(ids=[single_id]):
                            try:
                                count, errors = upload_item(full_item, uid_map, exchange_email, stalwart_user, stalwart_pass, stalwart_uids, count, errors, missing)
                            except Exception:
                                errors += 1
                    except Exception:
                        errors += 1

    print()

    to_delete = set(stalwart_uids.keys()) - exchange_uids
    if to_delete:
        print(f"  Lösche {len(to_delete)} im Zeitraum nicht mehr vorhandene Termine...")
        for uid in to_delete:
            delete_event(stalwart_user, stalwart_pass, stalwart_uids[uid])
            deleted += 1
            time.sleep(0.05)

    skipped = total - missing
    print(f"\n✓ {count} neu, {skipped} bereits vorhanden, {deleted} gelöscht, {errors} Fehler")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Verwendung: python3 migrate_calendar.py user@domain.com[passwort]")
        sys.exit(1)

    exchange_email = sys.argv[1]
    stalwart_pass  = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STALWART_PASS

    migrate_calendar(exchange_email, stalwart_pass)
