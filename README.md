# Exchange 2019 → Stalwart Mail Server Migration Toolkit

A complete toolkit for migrating from Microsoft Exchange 2019 to [Stalwart Mail Server](https://stalw.art/) including emails, calendars, contacts, and distribution groups.

## Overview

This toolkit was developed for a real-world, large-scale migration (100+ mailboxes, several hundred GB of mail data). It handles all aspects of the migration including incremental sync, duplicate detection, folder sanitization, and legacy folder cleanup.

## Architecture

```
Exchange 2019 (EWS/NTLM)
        ↓
Migration Host (Python scripts)
        ↓
Stalwart Mail Server (IMAP/CalDAV/CardDAV/JMAP)
```

## Prerequisites

### Migration Host (Linux)
```bash
pip3 install exchangelib imapclient pytz requests --break-system-packages
```

### Exchange 2019 (Exchange Management Shell)
`exchange_settings.ps1`, `user_export.ps1`, and `group_export.ps1` use Exchange cmdlets (`Get-Mailbox`, `New-ThrottlingPolicy`, `Get-DistributionGroup`, ...) and **must be run in the Exchange Management Shell** on the Exchange server - a normal PowerShell window fails with "command not recognized".

Run `exchange_settings.ps1` first to configure EWS Impersonation, throttling policies, and full mailbox access permissions.

> `user_import.ps1` and `group_import.ps1` only talk to Stalwart over JMAP (HTTP) and run in any normal PowerShell with network access to the Stalwart host - no Exchange Management Shell needed.

---

## Scripts

### PowerShell (Windows / Exchange Server)

| Script | Description |
|--------|-------------|
| `exchange_settings.ps1` | Configures Exchange for migration (impersonation, throttling, IIS reset) |
| `user_export.ps1` | Exports all mailboxes from Exchange to CSV |
| `user_import.ps1` | Creates user accounts in Stalwart via JMAP |
| `group_export.ps1` | Exports distribution groups from Exchange to CSV |
| `group_import.ps1` | Creates mailing lists in Stalwart via JMAP |

### Python (Migration Host)

| Script | Description |
|--------|-------------|
| `migrate_config.py` | Central configuration and email address mapping for edge cases |
| `migrate_emails.py` | Email migration via EWS → IMAP |
| `migrate_calendar.py` | Calendar migration via EWS → CalDAV |
| `migrate_contacts.py` | Contact migration via EWS → CardDAV |
| `migrate.py` | Wrapper to run all three migrations sequentially |

---

## Configuration

### migrate_config.py
Central place for email address mappings when Exchange and Stalwart addresses differ:

```python
EMAIL_MAP = {
    # 'exchange-address@domain.com': 'stalwart-address@domain.com',
}
```

### migrate_emails.py
```python
EXCHANGE_SERVER       = 'exchange.domain.com'
EXCHANGE_ADMIN        = 'administrator@domain.com'
EXCHANGE_PASS         = 'CHANGE_ME'
STALWART_HOST         = 'mail.domain.com'
DEFAULT_STALWART_PASS = 'CHANGE_ME'
MAX_WORKERS           = 8    # Parallel IMAP upload threads
FETCH_CHUNK           = 50   # Items fetched per EWS request
UPLOAD_BATCH          = 200  # Emails uploaded per batch
```

### migrate_calendar.py
```python
SYNC_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)  # Start of sync window
SYNC_TO   = datetime(2099, 1, 1, tzinfo=timezone.utc)  # End of sync window
```

Calendars with many events take a long time to migrate (each event is fetched and uploaded individually), so the default window covers the **current year onward** - users get their upcoming appointments quickly. Migrate older history afterwards in separate passes by widening `SYNC_FROM`/`SYNC_TO` (see Step 6).

### Stalwart Domain ID (user_import.ps1 / group_import.ps1)
The PowerShell import scripts reference `$DomainId`, the internal ID Stalwart
assigns to a domain (a short string such as `a` or `b`). The `b` in the scripts
is only an example - **set it to your own domain's ID** before running, otherwise
the user/list creation calls target the wrong (or a non-existent) domain. You can
find the ID in the Stalwart admin interface under your domain, or via the
management/JMAP API.

---

## Usage

### Step 1: Prepare Exchange
```powershell
.\exchange_settings.ps1
```

### Step 2: Export and import users
```powershell
.\user_export.ps1
.\user_import.ps1
```

### Step 3: Export and import distribution groups
```powershell
.\group_export.ps1
.\group_import.ps1
```

### Step 4: Prepare users.csv
```
user1@domain.com;Password123!
user2@domain.com
user3@domain.com;DifferentPassword!
```

Lines without a password use `DEFAULT_STALWART_PASS`. Clean the file before use:
```bash
sed -i 's/"//g' users.csv
sed -i '/^$/d' users.csv
```

### Step 5: Run migration
```bash
# All users (email + calendar + contacts)
python3 migrate.py users.csv

# Single user
python3 migrate_emails.py user@domain.com 'Password123!'
python3 migrate_calendar.py user@domain.com 'Password123!'
python3 migrate_contacts.py user@domain.com 'Password123!'
```

### Step 6: Migrate calendar history (optional)
Adjust `SYNC_FROM`/`SYNC_TO` in `migrate_calendar.py` and comment out email/contacts in `migrate.py`:

```python
SYNC_FROM = datetime(2016, 1, 1, tzinfo=timezone.utc)
SYNC_TO   = datetime(2025, 12, 31, tzinfo=timezone.utc)
```

---

## Key Features

### Email Migration
- **SQLite cache** per user – fast incremental re-runs; already migrated emails are skipped instantly
- **Two-phase import** – loads metadata first, then fetches only missing emails in chunks
- **Parallel upload** – configurable thread pool for IMAP APPEND operations
- **IMAP reconnect** – automatically reconnects on broken pipe or timeout
- **UTF-7 folder names** – correctly handles encoded folder names (ä, ö, ü, ß)
- **Date header repair** – fixes corrupt or missing date headers
- **Folder name sanitization** – replaces `/` in folder names with `+` to prevent IMAP hierarchy issues
- **Deletion sync (1:1 mirror)** – on incremental re-runs, mails deleted in Exchange are removed from Stalwart too, so the copy stays a 1:1 mirror
- **Post-cutover mail protected** – only previously-migrated mail (tracked in the per-user cache) is ever deleted; messages that arrived in Stalwart after the cutover are never touched
- **Exchange throttling retry** – automatically waits and retries on `ErrorServerBusy`

### Calendar Migration
- **UID-based sync** – only imports new events, skips already migrated ones
- **Time window sync** – configurable `SYNC_FROM`/`SYNC_TO` to control which period is synced
- **iCal fixes** – removes own ORGANIZER/ATTENDEE fields to prevent events showing as invitations
- **EndTimeZone error handling** – problematic events are retried individually
- **Rate limit handling** – automatic retry with backoff on HTTP 429

### Contact Migration
- **vCard validation** – skips emails incorrectly stored as contacts in Exchange
- **Version normalization** – converts all vCards to VERSION:3.0
- **Rate limit handling** – automatic retry with backoff on HTTP 429

### User & Group Import
- **Idempotent** – safely re-runnable, skips already existing users and groups
- **UTF-8 safe** – correctly handles special characters in display names
- **SSL bypass** – works with self-signed certificates

---

## Stalwart Configuration

### WebDAV Max Results
Increase the default limit to handle users with large calendars:
```bash
stalwart-cli --url https://mail.domain.com -k --user admin \
  update WebDav singleton --field "maxResults=20000"
systemctl restart stalwart
```

This raises the WebDAV result cap so the calendar dedup query can see *all* existing events on a large calendar (the default limit otherwise truncates it).

---

## Exchange Throttling

If you encounter frequent `ErrorServerBusy` errors, apply throttling policies:

```powershell
New-ThrottlingPolicy -Name "MigrationOrgPolicy" `
  -ThrottlingPolicyScope Organization `
  -EwsMaxConcurrency 100 `
  -EwsMaxBurst 9999999 `
  -EwsRechargeRate 9999999 `
  -EwsCutoffBalance 9999999

iisreset
```

> **Important:** `EwsCutoffBalance` must be set to a high number, not 0. A value of 0 causes immediate throttling on every request.

---

## Known Limitations

- Calendar events are synced within the configured time window only; events outside this window in Stalwart are not touched
- The Drafts folder is always fully re-imported as draft emails often lack Message-IDs
- Very large calendars (10,000+ events) may require multiple migration passes due to Exchange EWS limits per request

---

## License

MIT