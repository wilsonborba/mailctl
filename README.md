# mailctl

`mailctl` is a lightweight, agent-friendly email CLI written in Python for Gmail-compatible SMTP/IMAP workflows.

## Requirements

- Python 3.11 or newer
- Gmail account with 2-Step Verification enabled
- Gmail App Password

## Installation

Run:

```bash
python3 mailctl.py install
```

This installs the executable to `~/.local/bin/mailctl`.

If `~/.local/bin` is not in your `PATH`, add it manually:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then verify:

```bash
mailctl --help
```

To remove the installed executable later:

```bash
mailctl uninstall
```

## Gmail setup

1. Enable 2-Step Verification in your Google account.
2. Generate an App Password for Mail.
3. Use that App Password with `mailctl`.

`mailctl` does not use Gmail API, OAuth, or web scraping.

## Safe password usage

Recommended:

```bash
read -rsp "Gmail App Password: " MAILCTL_PASSWORD_PERSONAL
export MAILCTL_PASSWORD_PERSONAL
echo
```

Not recommended:

```bash
export MAILCTL_PASSWORD_PERSONAL="senha"
```

Inline export may leave the secret in shell history.

## Account configuration

Add an account:

```bash
mailctl account add \
  --alias personal \
  --email user@gmail.com \
  --name "User Name" \
  --provider gmail
```

List accounts:

```bash
mailctl account list
```

Select the default account:

```bash
mailctl account use personal
```

Test SMTP and IMAP authentication without sending email:

```bash
mailctl account test personal
```

Configuration is stored in:

```text
~/.config/mailctl/config.json
```

Secrets are never stored in that file.

## Sending email

Dry-run first:

```bash
mailctl send \
  --account personal \
  --to hr@company.com \
  --subject "Application - Data Scientist" \
  --body "Please find my resume attached." \
  --attach "$HOME/Documents/CV.pdf" \
  --dry-run
```

Real send:

```bash
mailctl send \
  --account personal \
  --to hr@company.com \
  --cc manager@company.com \
  --subject "Application - ML Engineer" \
  --body-file cover-letter.txt \
  --attach CV.pdf \
  --attach certificates.pdf
```

Notes:

- `--attach` can be repeated
- `--yes` disables the confirmation prompt
- `--dry-run` builds and validates the message without connecting to SMTP
- `--json` returns stable machine-readable output

## Local drafts

Create a local draft:

```bash
mailctl draft create \
  --account personal \
  --to hr@company.com \
  --subject "Follow-up" \
  --body "Hello"
```

Send a saved draft:

```bash
mailctl draft send DRAFT_ID
```

Drafts are local files only. SMTP/IMAP cannot guarantee that these drafts appear in Gmail Drafts automatically.

## Reading and downloading

List folders:

```bash
mailctl folders --account personal
```

Inbox:

```bash
mailctl inbox --account personal --limit 20
mailctl inbox --account personal --unread --json
```

Search:

```bash
mailctl search --account personal --from recruiter@company.com
mailctl search --account personal --subject interview
```

Read:

```bash
mailctl read --account personal 12345
```

Attachment listing and download:

```bash
mailctl attachments --account personal 12345
mailctl download --account personal 12345 --output ~/Downloads/mailctl/
```

Mailbox actions:

```bash
mailctl mark --account personal 12345 --read
mailctl mark --account personal 12345 --unread
mailctl move --account personal 12345 --folder "Applications"
mailctl delete --account personal 12345
```

## Reply

Reply while preserving thread headers when possible:

```bash
mailctl reply --account personal 12345 \
  --body "Thanks for the update." \
  --dry-run
```

## Agent usage

Most operational commands support `--json`:

```bash
mailctl send --account personal --to hr@company.com --subject "Ping" --body "Hello" --dry-run --json
```

Success format:

```json
{
  "ok": true,
  "command": "send",
  "result": {}
}
```

Error format:

```json
{
  "ok": false,
  "error": {
    "type": "authentication_error",
    "message": "..."
  }
}
```

## Security

- Uses verified TLS with `ssl.create_default_context()`
- Does not store App Passwords in config or history
- Does not print secrets in normal output
- Sanitizes downloaded attachment names
- Does not render HTML, execute scripts, open links, or fetch remote images
- Refuses invalid attachment paths and directories

## Local data

- Config: `~/.config/mailctl/config.json`
- History: `~/.local/state/mailctl/history.jsonl`
- Drafts: `~/.local/state/mailctl/drafts/`
- Downloads: `~/Downloads/mailctl/`

History stores safe metadata only: recipients, subject, attachment names, message ID, timestamp, and status.

## Limitations

- Gmail support in this MVP assumes App Password authentication
- SMTP/IMAP cannot guarantee universal server-side draft behavior
- SMTP/IMAP cannot guarantee that a message never lands in spam
- This project does not implement bulk sending, Gmail API features, or OAuth
