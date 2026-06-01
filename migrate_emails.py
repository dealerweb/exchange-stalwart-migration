import imaplib
import ssl
import sys
import email as emaillib
import threading
import sqlite3
import re
import time
import base64
from email.utils import parsedate_tz, formatdate, parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from imapclient.imapclient import encode_utf7
from exchangelib import IMPERSONATION, Account, Credentials, Configuration, NTLM, ItemId
from exchangelib.errors import ErrorServerBusy
from exchangelib.protocol import BaseProtocol
from requests.adapters import HTTPAdapter
from migrate_config import stw_email
import urllib3
urllib3.disable_warnings()

EXCHANGE_SERVER       = 'exchange.domain.com'
EXCHANGE_ADMIN        = 'administrator@domain.com'
EXCHANGE_PASS         = 'CHANGE_ME'
STALWART_HOST         = 'mail.domain.com'
STALWART_PORT         = 993
DEFAULT_STALWART_PASS = 'CHANGE_ME'
MAX_WORKERS           = 8
FETCH_CHUNK           = 50
UPLOAD_BATCH          = 200
MAX_RETRIES           = 5
RETRY_WAIT            = 30

ALWAYS_REIMPORT = {'Entwürfe'}

FOLDER_MAP = {
    'Posteingang':        'INBOX',
    'Gesendete Elemente': 'Gesendet',
    'Gelöschte Elemente': 'Papierkorb',
    'Entwürfe':           'Entwürfe',
    'Junk-E-Mail':        'Spam',
    'Archiv':             'Archiv',
}

ROOT_SYSTEM_FOLDERS = set(FOLDER_MAP.keys())

CATEGORY_MAP = {
    'Rote Kategorie':   '$label:red',
    'Orange Kategorie': '$label:orange',
    'Gelbe Kategorie':  '$label:yellow',
    'Grüne Kategorie':  '$label:green',
    'Blaue Kategorie':  '$label:blue',
    'Lila Kategorie':   '$label:purple',
    'Rosa Kategorie':   '$label:pink',
    'Red Category':     '$label:red',
    'Orange Category':  '$label:orange',
    'Yellow Category':  '$label:yellow',
    'Green Category':   '$label:green',
    'Blue Category':    '$label:blue',
    'Purple Category':  '$label:purple',
    'Pink Category':    '$label:pink',
}

EXCLUDE = {
    'Kalender', 'Kontakte', 'Synchronisierungsprobleme', 'Aufgaben',
    'Journal', 'Notizen', 'Postausgang', 'Mailspring', 'RSS-Feeds',
    'Archives', 'Recipient Cache', 'GAL Contacts', 'Organizational Contacts',
    'PeopleCentricConversation Buddies', 'Companies', 'Verlauf der Unterhaltung',
    'Conversation Action Settings', 'Dateien', 'Einstellungen für QuickSteps',
    'ExternalContacts', 'Yammer Root', 'Working Set',
}

thread_local = threading.local()
print_lock   = threading.Lock()
db_lock      = threading.Lock()

class NoVerifyHTTPAdapter(HTTPAdapter):
    def send(self, *args, **kwargs):
        kwargs['verify'] = False
        return super().send(*args, **kwargs)

BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

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

def get_db_path(stalwart_user):
    safe = stalwart_user.replace('@', '_').replace('.', '_')
    return f'/tmp/migration_cache_{safe}.db'

def init_db(stalwart_user):
    conn = sqlite3.connect(get_db_path(stalwart_user), check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS migrated
                    (mid TEXT, folder TEXT, imap_uid INTEGER,
                     PRIMARY KEY (mid, folder))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_folder ON migrated(folder)')
    conn.commit()
    try:
        conn.execute('ALTER TABLE migrated ADD COLUMN imap_uid INTEGER')
        conn.commit()
    except Exception:
        pass
    return conn

def get_cached_ids(conn, folder):
    rows = conn.execute(
        'SELECT mid FROM migrated WHERE folder=?', (folder,)
    ).fetchall()
    return set(r[0] for r in rows)

def get_cached_uid_map(conn, folder):
    rows = conn.execute(
        'SELECT mid, imap_uid FROM migrated WHERE folder=? AND imap_uid IS NOT NULL',
        (folder,)
    ).fetchall()
    return {r[0]: r[1] for r in rows}

def clear_folder_cache(conn, folder):
    with db_lock:
        conn.execute('DELETE FROM migrated WHERE folder=?', (folder,))
        conn.commit()

def mark_migrated_batch(conn, items, folder):
    if not items:
        return
    with db_lock:
        conn.executemany(
            'INSERT OR IGNORE INTO migrated VALUES (?,?,?)',
            [(mid, folder, uid) for mid, uid in items]
        )
        conn.commit()

def enc(folder):
    return encode_utf7(folder).decode('ascii')

def dec(folder_raw):
    def decode_match(m):
        b64 = m.group(1).replace(',', '/')
        if not b64:
            return '&'
        try:
            return base64.b64decode(b64 + '==').decode('utf-16-be')
        except Exception:
            return m.group(0)
    try:
        return re.sub(r'&([^-]*)-', decode_match, folder_raw)
    except Exception:
        return folder_raw

def get_imap_date(raw):
    try:
        parsed = emaillib.message_from_bytes(raw)
        date_str = parsed.get('Date', '')
        if date_str:
            result = parsedate_tz(date_str)
            if result and 1970 <= result[0] <= 2100:
                dt = parsedate_to_datetime(date_str)
                return imaplib.Time2Internaldate(dt.timestamp())
    except Exception:
        pass
    return None

def fix_date_header(raw):
    try:
        parsed = emaillib.message_from_bytes(raw)
        date = parsed.get('Date', '')
        needs_fix = False
        if not date:
            needs_fix = True
        else:
            result = parsedate_tz(date)
            if result and (result[0] > 2100 or result[0] < 1970):
                needs_fix = True
        if needs_fix:
            raw_str = raw.decode('utf-8', errors='replace')
            new_date = f'Date: {formatdate()}'
            if re.search(r'^Date:', raw_str, re.MULTILINE):
                raw_str = re.sub(r'^Date:.*$', new_date, raw_str, flags=re.MULTILINE)
            else:
                raw_str = raw_str.replace('\r\n\r\n', f'\r\n{new_date}\r\n\r\n', 1)
            return raw_str.encode('utf-8')
    except Exception:
        pass
    return raw

def get_mid_from_raw(raw):
    parsed = emaillib.message_from_bytes(raw)
    mid = parsed.get('Message-ID', '').strip()
    if not mid:
        subject = parsed.get('Subject', '') or ''
        date    = parsed.get('Date', '') or ''
        mid = f"<fallback-{hash(f'{subject}{date}')}@local>"
    return mid

def get_exchange_account(email):
    credentials = Credentials(username=EXCHANGE_ADMIN, password=EXCHANGE_PASS)
    config = Configuration(server=EXCHANGE_SERVER, credentials=credentials, auth_type=NTLM)
    return Account(primary_smtp_address=email, config=config, access_type=IMPERSONATION)

def connect_imap(stalwart_user, stalwart_pass):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    imap = imaplib.IMAP4_SSL(STALWART_HOST, STALWART_PORT, ssl_context=ctx)
    imap.login(stalwart_user, stalwart_pass)
    return imap

def reconnect_imap(imap, stalwart_user, stalwart_pass):
    try:
        imap.noop()
        return imap
    except Exception:
        print("  IMAP-Verbindung unterbrochen, verbinde neu...")
        try:
            imap.logout()
        except Exception:
            pass
        for attempt in range(MAX_RETRIES):
            try:
                return connect_imap(stalwart_user, stalwart_pass)
            except Exception as e:
                print(f"  Reconnect fehlgeschlagen (Versuch {attempt+1}/{MAX_RETRIES}): {e}")
                time.sleep(5)
        raise Exception("IMAP Reconnect nicht möglich")

def get_thread_imap(stalwart_user, stalwart_pass):
    if not hasattr(thread_local, 'imap') or thread_local.imap is None:
        thread_local.imap = connect_imap(stalwart_user, stalwart_pass)
    else:
        try:
            thread_local.imap.noop()
        except Exception:
            try:
                thread_local.imap.logout()
            except Exception:
                pass
            thread_local.imap = connect_imap(stalwart_user, stalwart_pass)
    return thread_local.imap

def ensure_imap_folder(imap, folder, stalwart_user, stalwart_pass):
    imap = reconnect_imap(imap, stalwart_user, stalwart_pass)
    res = imap.select(f'"{enc(folder)}"')
    if res[0] != 'OK':
        imap.create(f'"{enc(folder)}"')
        imap.subscribe(f'"{enc(folder)}"')
    return imap

def delete_all_messages_in_folder(imap, folder, stalwart_user, stalwart_pass):
    imap = reconnect_imap(imap, stalwart_user, stalwart_pass)
    res = imap.select(f'"{enc(folder)}"')
    if res[0] != 'OK':
        return imap
    typ, data = imap.search(None, 'ALL')
    if typ != 'OK' or not data[0]:
        return imap
    uids = data[0].split()
    if not uids:
        return imap
    imap.store(b','.join(uids), '+FLAGS', '\\Deleted')
    imap.expunge()
    return imap

def fetch_message_ids_from_imap(imap, imap_folder):
    existing = {}
    res = imap.select(f'"{enc(imap_folder)}"', readonly=True)
    if res[0] != 'OK' or res[1][0] == b'0':
        return existing
    typ, data = imap.search(None, 'ALL')
    if typ != 'OK' or not data[0]:
        return existing
    uids = data[0].split()
    if not uids:
        return existing
    batch_size = 500
    for i in range(0, len(uids), batch_size):
        batch = b','.join(uids[i:i+batch_size])
        typ, msgs = imap.fetch(batch, '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE)])')
        if typ != 'OK':
            continue
        for msg_data in msgs:
            if isinstance(msg_data, tuple):
                uid_match = re.search(rb'UID (\d+)', msg_data[0])
                imap_uid = int(uid_match.group(1)) if uid_match else None
                parsed = emaillib.message_from_bytes(msg_data[1])
                mid = parsed.get('Message-ID', '').strip()
                if not mid:
                    subject = parsed.get('Subject', '') or ''
                    date    = parsed.get('Date', '') or ''
                    mid = f"<fallback-{hash(f'{subject}{date}')}@local>"
                if mid:
                    existing[mid] = imap_uid
    return existing

def preload_stalwart_ids(main_imap, db_conn, stalwart_user, stalwart_pass):
    cached = db_conn.execute('SELECT COUNT(*) FROM migrated').fetchone()[0]
    if cached > 0:
        print(f"  DB-Cache vorhanden: {cached} Message-IDs")
        return main_imap

    print("  Erstelle DB-Cache von Stalwart...")
    main_imap = reconnect_imap(main_imap, stalwart_user, stalwart_pass)
    typ, folders = main_imap.list()
    if typ != 'OK':
        return main_imap

    folder_list = []
    for folder_data in folders:
        try:
            decoded = folder_data.decode('utf-8', errors='replace')
            match = re.search(r'"/" "?(.+?)"?$', decoded)
            if not match:
                match = re.search(r'"/" (.+)$', decoded)
            if not match:
                continue
            raw_name    = match.group(1).strip().strip('"')
            folder_name = dec(raw_name)
            if folder_name in ALWAYS_REIMPORT:
                continue
            folder_list.append(folder_name)
        except Exception:
            continue

    total_loaded = 0
    for idx, folder_name in enumerate(folder_list, 1):
        try:
            main_imap = reconnect_imap(main_imap, stalwart_user, stalwart_pass)
            id_map = fetch_message_ids_from_imap(main_imap, folder_name)
            if id_map:
                mark_migrated_batch(db_conn, list(id_map.items()), folder_name)
                total_loaded += len(id_map)
            print(f"  Cache [{idx}/{len(folder_list)}] {folder_name}: {len(id_map)} IDs")
        except Exception as e:
            print(f"  Cache Fehler bei {folder_name}: {e}")
            continue

    print(f"  DB-Cache erstellt: {total_loaded} Message-IDs")
    return main_imap

def has_any_messages(folder):
    if folder.total_count > 0:
        return True
    try:
        for subfolder in folder.children:
            if subfolder.name in EXCLUDE:
                continue
            if has_any_messages(subfolder):
                return True
    except Exception:
        pass
    return False

def build_flags(is_read, categories):
    flags = []
    if is_read:
        flags.append('\\Seen')
    for cat in (categories or []):
        label = CATEGORY_MAP.get(cat)
        if label:
            flags.append(label)
    if flags:
        return '(' + ' '.join(flags) + ')'
    return None

def upload_message(args):
    stalwart_user, stalwart_pass, imap_folder, raw, flags, mid, imap_date = args
    try:
        imap = get_thread_imap(stalwart_user, stalwart_pass)
        result = imap.append(f'"{enc(imap_folder)}"', flags, imap_date, raw)
        imap_uid = None
        if result and result[0] == 'OK':
            uid_match = re.search(rb'\[APPENDUID \d+ (\d+)\]', result[1][0] if result[1] else b'')
            if uid_match:
                imap_uid = int(uid_match.group(1))
        return ('ok', mid, imap_uid)
    except Exception as e:
        thread_local.imap = None
        return ('error', str(e), None)

def upload_batch_items(items, db_conn, imap_folder, count_ref, errors_ref, total_missing):
    newly_migrated = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(upload_message, args) for args in items]
        for future in futures:
            result, val, imap_uid = future.result()
            if result == 'ok':
                count_ref[0] += 1
                if val:
                    newly_migrated.append((val, imap_uid))
            else:
                errors_ref[0] += 1
    if newly_migrated:
        mark_migrated_batch(db_conn, newly_migrated, imap_folder)
    with print_lock:
        print(f"    Fortschritt: {count_ref[0]}/{total_missing} migriert, {errors_ref[0]} Fehler...")

def delete_removed_messages(imap, imap_folder, exchange_mids, db_conn, stalwart_user, stalwart_pass):
    uid_map = get_cached_uid_map(db_conn, imap_folder)
    to_delete_mids = set(uid_map.keys()) - exchange_mids
    if not to_delete_mids:
        return imap, 0

    to_delete_uids = [str(uid_map[mid]) for mid in to_delete_mids if uid_map.get(mid)]
    if not to_delete_uids:
        return imap, 0

    print(f"    {len(to_delete_uids)} in Exchange gelöschte Mails werden entfernt...")
    imap = reconnect_imap(imap, stalwart_user, stalwart_pass)
    imap.select(f'"{enc(imap_folder)}"')
    imap.uid('STORE', ','.join(to_delete_uids), '+FLAGS', '\\Deleted')
    imap.expunge()

    with db_lock:
        db_conn.executemany(
            'DELETE FROM migrated WHERE mid=? AND folder=?',
            [(mid, imap_folder) for mid in to_delete_mids]
        )
        db_conn.commit()

    return imap, len(to_delete_uids)

def migrate_folder(exchange_folder, main_imap, imap_folder, exchange_email, stalwart_user, stalwart_pass, db_conn, account):
    total = exchange_folder.total_count
    if total == 0:
        return main_imap, 0

    main_imap = ensure_imap_folder(main_imap, imap_folder, stalwart_user, stalwart_pass)

    if imap_folder in ALWAYS_REIMPORT:
        print(f"  → {imap_folder}: immer neu importieren – lösche vorhandene Mails...")
        main_imap = delete_all_messages_in_folder(main_imap, imap_folder, stalwart_user, stalwart_pass)
        clear_folder_cache(db_conn, imap_folder)
        existing_ids = set()
    else:
        existing_ids = get_cached_ids(db_conn, imap_folder)

    print(f"  → {imap_folder}: {total} Nachrichten ({len(existing_ids)} bereits vorhanden)")

    print(f"    Lade Metadaten...")
    missing_items = []
    exchange_mids = set()
    meta_count = 0
    for item in with_retry(lambda: list(exchange_folder.all().only('message_id', 'is_read', 'categories'))):
        try:
            mid = (getattr(item, 'message_id', None) or '').strip()
            if not mid or mid not in existing_ids:
                missing_items.append((
                    item.id,
                    item.changekey,
                    mid,
                    getattr(item, 'is_read', False),
                    getattr(item, 'categories', None)
                ))
            if mid:
                exchange_mids.add(mid)
            meta_count += 1
            if meta_count % 500 == 0:
                print(f"    Metadaten: {meta_count}/{total} gelesen...")
        except Exception:
            pass

    if imap_folder not in ALWAYS_REIMPORT:
        main_imap, deleted = delete_removed_messages(main_imap, imap_folder, exchange_mids, db_conn, stalwart_user, stalwart_pass)
        if deleted:
            print(f"    {deleted} gelöschte Mails entfernt")

    if not missing_items:
        print(f"    ✓ alles bereits vorhanden")
        return main_imap, 0

    total_missing = len(missing_items)
    print(f"    {total_missing} neue Mails werden geladen und hochgeladen...")

    item_ids = [ItemId(id=i[0], changekey=i[1]) for i in missing_items]
    meta_map = {i[0]: i for i in missing_items}

    main_imap = reconnect_imap(main_imap, stalwart_user, stalwart_pass)
    main_imap.select(f'"{enc(imap_folder)}"')

    count_ref  = [0]
    errors_ref = [0]
    batch      = []

    for i in range(0, len(item_ids), FETCH_CHUNK):
        chunk = item_ids[i:i+FETCH_CHUNK]
        try:
            for full_item in with_retry(lambda: list(account.fetch(ids=chunk))):
                try:
                    if not hasattr(full_item, 'mime_content') or full_item.mime_content is None:
                        continue
                    raw = full_item.mime_content
                    if isinstance(raw, str):
                        raw = raw.encode('utf-8')

                    raw       = fix_date_header(raw)
                    imap_date = get_imap_date(raw)
                    mid       = get_mid_from_raw(raw)

                    if mid in existing_ids:
                        continue

                    meta  = meta_map.get(full_item.id)
                    flags = build_flags(
                        meta[3] if meta else False,
                        meta[4] if meta else None
                    )
                    batch.append((stalwart_user, stalwart_pass, imap_folder, raw, flags, mid, imap_date))
                    existing_ids.add(mid)

                    if len(batch) >= UPLOAD_BATCH:
                        upload_batch_items(batch, db_conn, imap_folder, count_ref, errors_ref, total_missing)
                        batch = []
                        main_imap = reconnect_imap(main_imap, stalwart_user, stalwart_pass)
                        main_imap.select(f'"{enc(imap_folder)}"')
                except Exception:
                    pass
        except Exception as e:
            print(f"    Chunk-Fehler: {e}")

    if batch:
        upload_batch_items(batch, db_conn, imap_folder, count_ref, errors_ref, total_missing)

    print(f"    ✓ {count_ref[0]} migriert, {errors_ref[0]} Fehler")
    return main_imap, count_ref[0]

def process_folder(exchange_folder, main_imap, imap_path, exchange_email, stalwart_user, stalwart_pass, db_conn, account, is_root=False):
    name = exchange_folder.name

    if name in EXCLUDE:
        return main_imap, 0

    if not has_any_messages(exchange_folder):
        return main_imap, 0

    # A "/" inside an Exchange folder name would be read as an IMAP hierarchy
    # separator and split the folder in two. Replace it with "+" for the path.
    safe_name = name.replace('/', '+')

    if is_root and name not in ROOT_SYSTEM_FOLDERS:
        current_imap_path = f'INBOX/{safe_name}'
    else:
        mapped_name = FOLDER_MAP.get(name, safe_name)
        current_imap_path = mapped_name if imap_path == '' else f'{imap_path}/{mapped_name}'

    main_imap, count = migrate_folder(exchange_folder, main_imap, current_imap_path, exchange_email, stalwart_user, stalwart_pass, db_conn, account)
    total = count

    try:
        for subfolder in exchange_folder.children:
            main_imap, count = process_folder(subfolder, main_imap, current_imap_path, exchange_email, stalwart_user, stalwart_pass, db_conn, account)
            total += count
    except Exception:
        pass

    return main_imap, total

def migrate_user(exchange_email, stalwart_pass):
    stalwart_user = stw_email(exchange_email)

    print(f"\n{'='*50}")
    print(f"Migriere: {exchange_email}")
    if stalwart_user != exchange_email:
        print(f"Stalwart: {stalwart_user}")
    print('='*50)

    account   = with_retry(lambda: get_exchange_account(exchange_email))
    main_imap = connect_imap(stalwart_user, stalwart_pass)
    db_conn   = init_db(stalwart_user)

    main_imap = preload_stalwart_ids(main_imap, db_conn, stalwart_user, stalwart_pass)

    root  = account.root / 'Oberste Ebene des Informationsspeichers'
    total = 0

    for folder in root.children:
        main_imap, count = process_folder(folder, main_imap, '', exchange_email, stalwart_user, stalwart_pass, db_conn, account, is_root=True)
        total += count

    try:
        main_imap.logout()
    except Exception:
        pass
    db_conn.close()
    print(f"\nGesamt: {total} neue Nachrichten migriert")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Verwendung: python3 migrate_emails.py user@domain.com [passwort]")
        sys.exit(1)

    exchange_email = sys.argv[1]
    stalwart_pass  = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STALWART_PASS

    migrate_user(exchange_email, stalwart_pass)
