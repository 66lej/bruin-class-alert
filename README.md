# Bruin Class Alert

A Python 3 UCLA class availability monitor. It periodically checks the current UCLA Schedule of Classes pages and sends notifications when a watched section has an open seat.

## Why not use the old repo?

The older UCLA class alert scripts had the right idea, but they were built around an older UCLA site structure and an outdated Python stack:

- They scraped older Registrar HTML pages.
- They depended on Python 2, `execfile`, and BeautifulSoup 3.
- They used legacy SMTP patterns such as plain Gmail password login.

This version targets the current UCLA Schedule of Classes search results pages and parses the current section status layout.

## Features

- Python 3
- Monitor multiple classes at once
- Filter by section, such as `Lec 1` or `Dis 1A`
- Automatically normalize common UCLA catalog formats, such as turning `31` into `0031`
- macOS desktop notifications
- Discord webhook notifications
- SMTP email notifications
- Optional local MyUCLA auto-enroll flow through Google Chrome
- State tracking to avoid spamming the same alert every polling cycle

## Installation

```bash
python3 -m pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
```

## Configuration

Edit `config.json`.

### Top-level fields

- `poll_interval_seconds`: polling interval in seconds; `60` or slower is recommended
- `request_timeout_seconds`: timeout for each UCLA request
- `request_retries`: automatic retries when UCLA or the network is flaky
- `retry_backoff_seconds`: backoff delay before each retry
- `watchlist`: the classes you want to monitor

### Watchlist entries

- `term`: UCLA term code such as `26S`, or a readable form such as `Spring 2026`
- `subject`: UCLA subject code such as `COM SCI`, or a readable subject name
- `catalog`: course number such as `31`, `100A`, or `M146`
- `section`: optional; if omitted, any open section for that course can trigger an alert
- `session_group`: optional; useful for summer sessions such as `A%` or `C6`
- `notify_on_waitlist`: optional, default `false`; if `true`, the script also alerts when the waitlist still has room

### Local auto-enroll

`auto_enroll` is optional. The current local auto-enroll flow only supports:

- `macOS`
- `Google Chrome`
- an existing manual MyUCLA login in your local Chrome profile

A conservative starting configuration looks like this:

```json
"auto_enroll": {
  "enabled": true,
  "allow_waitlist_auto_enroll": false
}
```

Default behavior:

- Auto-enroll only runs when a real seat is open
- It does not join the waitlist unless you explicitly enable `allow_waitlist_auto_enroll`
- It reuses your local Chrome login session and does not store your UCLA password

Before using it for the first time, open the MyUCLA login flow:

```bash
python3 myucla_auto_enroll.py --setup-login
```

Complete UCLA Logon and Duo in Chrome. After that, the monitor can attempt local auto-enroll when it detects an opening.

You also need this Chrome setting enabled:

- `View > Developer > Allow JavaScript from Apple Events`

Without that setting, the script can open Chrome but cannot drive the page interactions.

To verify local browser automation before relying on auto-enroll, run:

```bash
python3 myucla_auto_enroll.py --self-test
```

Notes:

- For classes like `GEOG 7` that require `Lecture + Laboratory`, the current strategy is to select the first available secondary section if you do not specify one
- If MyUCLA shows a `PTE`, warning, restriction, expired login, or another blocking state, the script stops the automated action and includes the outcome in the notification text
- This is a best-effort local browser automation flow, so if UCLA changes the MyUCLA DOM it may need updates

### Notification methods

#### macOS

```json
"notifiers": {
  "macos": true
}
```

#### Discord webhook

```json
"notifiers": {
  "macos": true,
  "discord_webhook_env": "BRUIN_ALERT_DISCORD_WEBHOOK_URL"
}
```

#### Email

Using a Gmail App Password is recommended instead of your main account password.

```json
"email": {
  "enabled": true,
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "use_tls": true,
  "username_env": "BRUIN_ALERT_EMAIL_USERNAME",
  "password_env": "BRUIN_ALERT_SMTP_PASSWORD",
  "from_email_env": "BRUIN_ALERT_EMAIL_FROM",
  "to_email_env": "BRUIN_ALERT_EMAIL_TO"
}
```

Then fill in `.env`:

```bash
BRUIN_ALERT_DISCORD_WEBHOOK_URL='your Discord webhook'
BRUIN_ALERT_EMAIL_USERNAME='your email account'
BRUIN_ALERT_EMAIL_FROM='sender email'
BRUIN_ALERT_EMAIL_TO='recipient email'
BRUIN_ALERT_SMTP_PASSWORD='your Gmail app password'
```

## Running

Start with a one-time check:

```bash
python3 bruin_alert.py --config config.json --once --debug
```

If the output looks correct, run it continuously:

```bash
python3 bruin_alert.py --config config.json
```

The script automatically loads `.env` from the current directory. If Discord or email variables are missing, those channels are skipped, while terminal output and macOS notifications can still keep working.

## Helper commands

List the current UCLA terms:

```bash
python3 bruin_alert.py --list-terms
```

List the current UCLA subjects:

```bash
python3 bruin_alert.py --list-subjects
```

## Practical tips

- Do not poll too aggressively; `60` seconds is usually enough
- Use `--once --debug` first so you can confirm the exact section labels UCLA is returning before deciding how to fill the `section` field
- If your laptop sleeps or shuts down, local monitoring and local desktop notifications stop too; for 24/7 monitoring, run it on an always-on machine and use Discord or email alerts
