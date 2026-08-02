#!/usr/bin/env python3
"""mailctl: lightweight, agent-friendly email CLI."""

from __future__ import annotations

import argparse
import email.utils
import html
import imaplib
import json
import mimetypes
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import uuid
import venv
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from socket import timeout as SocketTimeout
from typing import Any
import getpass

try:
    import keyring
    from keyring.errors import KeyringError, NoKeyringError
except ImportError:
    keyring = None

    class KeyringError(Exception):
        pass

    class NoKeyringError(KeyringError):
        pass


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_NETWORK = 5
EXIT_SERVER = 6
EXIT_CANCELED = 7
EXIT_INVALID_FILE = 8
EXIT_CONFLICT = 9

TIMEOUT_SECONDS = 20
GMAIL_WARNING_SIZE = 20 * 1024 * 1024
KEYRING_SERVICE = "mailctl"


class MailCtlError(Exception):
    """Handled application error."""

    def __init__(self, message: str, *, exit_code: int, error_type: str) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.error_type = error_type


@dataclass
class AttachmentSpec:
    path: Path
    filename: str
    size: int
    mime_type: str


@dataclass
class MessageBuildResult:
    message: EmailMessage
    attachments: list[AttachmentSpec]
    recipients: list[str]
    account_alias: str
    account_email: str
    subject: str
    total_size: int


def config_root() -> Path:
    return Path.home() / ".config" / "mailctl"


def state_root() -> Path:
    return Path.home() / ".local" / "state" / "mailctl"


def config_path() -> Path:
    return config_root() / "config.json"


def history_path() -> Path:
    return state_root() / "history.jsonl"


def drafts_root() -> Path:
    return state_root() / "drafts"


def install_path() -> Path:
    return Path.home() / ".local" / "bin" / "mailctl"


def runtime_root() -> Path:
    return Path.home() / ".local" / "share" / "mailctl"


def runtime_venv_path() -> Path:
    return runtime_root() / ".venv"


def runtime_python_path() -> Path:
    return runtime_venv_path() / "bin" / "python"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "mailctl"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)


def output_result(args: argparse.Namespace, command: str, result: Any) -> int:
    if getattr(args, "json", False):
        print(json_dump({"ok": True, "command": command, "result": result}))
    elif result is not None:
        if isinstance(result, str):
            print(result)
        elif isinstance(result, list):
            for item in result:
                if isinstance(item, str):
                    print(item)
                else:
                    print(json_dump(item))
        else:
            print(json_dump(result))
    return EXIT_OK


def output_error(args: argparse.Namespace | None, exc: MailCtlError) -> int:
    if args is not None and getattr(args, "json", False):
        print(
            json_dump(
                {
                    "ok": False,
                    "error": {"type": exc.error_type, "message": exc.message},
                }
            )
        )
    else:
        print(f"error: {exc.message}", file=sys.stderr)
    return exc.exit_code


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def ensure_parent(path: Path, mode: int = 0o700) -> None:
    ensure_dir(path.parent, mode)


def write_private_json(path: Path, data: Any) -> None:
    ensure_parent(path)
    path.write_text(json_dump(data) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def load_config(required: bool = False) -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        if required:
            raise MailCtlError(
                "config not found, add an account first",
                exit_code=EXIT_CONFIG,
                error_type="configuration_error",
            )
        return {"default_account": None, "accounts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailCtlError(
            f"invalid config file: {exc}",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        ) from exc
    if "accounts" not in data or not isinstance(data["accounts"], dict):
        raise MailCtlError(
            "config is missing accounts mapping",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )
    return data


def save_config(data: dict[str, Any]) -> None:
    ensure_dir(config_root())
    write_private_json(config_path(), data)


def normalize_alias(alias: str) -> str:
    alias = alias.strip()
    if not alias:
        raise MailCtlError(
            "account alias cannot be empty",
            exit_code=EXIT_USAGE,
            error_type="usage_error",
        )
    return alias


def password_env_name(alias: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "_", alias).upper()
    return f"MAILCTL_PASSWORD_{clean}"


def keyring_username(alias: str, email_address: str) -> str:
    return f"{alias}:{email_address.lower()}"


def has_keyring_library() -> bool:
    return keyring is not None


def keyring_backend_name() -> str | None:
    if not has_keyring_library():
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    return backend.__class__.__name__


def ensure_keyring_ready() -> None:
    if not has_keyring_library():
        raise MailCtlError(
            "secure credential support is not installed. reinstall mailctl with pyproject dependencies or run install.sh",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )
    backend = keyring.get_keyring()
    if backend.__class__.__name__.lower().startswith("fail"):
        raise MailCtlError(
            "no usable system keyring backend is available. on Debian Linux, install and unlock a Secret Service backend such as gnome-keyring",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )


def save_password(alias: str, email_address: str, password: str) -> None:
    ensure_keyring_ready()
    if not password:
        raise MailCtlError(
            "empty password not allowed",
            exit_code=EXIT_AUTH,
            error_type="authentication_error",
        )
    try:
        keyring.set_password(KEYRING_SERVICE, keyring_username(alias, email_address), password)
    except (KeyringError, RuntimeError) as exc:
        raise MailCtlError(
            f"failed to store password securely: {exc}",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        ) from exc


def delete_password(alias: str, email_address: str) -> bool:
    if not has_keyring_library():
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, keyring_username(alias, email_address))
        return True
    except Exception:
        return False


def get_stored_password(alias: str, email_address: str) -> str | None:
    if not has_keyring_library():
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, keyring_username(alias, email_address))
    except Exception:
        return None


def validate_email(address: str) -> str:
    address = address.strip()
    _, parsed = email.utils.parseaddr(address)
    if not parsed or "@" not in parsed:
        raise MailCtlError(
            f"invalid email address: {address}",
            exit_code=EXIT_USAGE,
            error_type="usage_error",
        )
    local, _, domain = parsed.partition("@")
    if not local or not domain or "." not in domain:
        raise MailCtlError(
            f"invalid email address: {address}",
            exit_code=EXIT_USAGE,
            error_type="usage_error",
        )
    return parsed


def split_addresses(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for item in value.split(","):
            item = item.strip()
            if item:
                result.append(validate_email(item))
    return result


def get_account(config: dict[str, Any], alias: str | None) -> tuple[str, dict[str, Any]]:
    use_alias = alias or config.get("default_account")
    if not use_alias:
        raise MailCtlError(
            "no account specified and no default account configured",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )
    account = config["accounts"].get(use_alias)
    if not account:
        raise MailCtlError(
            f"account not found: {use_alias}",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )
    return use_alias, account


def get_password(alias: str, email_address: str, *, interactive: bool = True, persist_prompt: bool = False) -> str:
    stored = get_stored_password(alias, email_address)
    if stored:
        return stored
    env_name = password_env_name(alias)
    value = os.environ.get(env_name)
    if value:
        return value
    if not interactive:
        raise MailCtlError(
            f"password not available in keyring or environment variable {env_name}",
            exit_code=EXIT_AUTH,
            error_type="authentication_error",
        )
    value = getpass.getpass(f"App Password for {alias}: ")
    if not value:
        raise MailCtlError(
            "empty password not allowed",
            exit_code=EXIT_AUTH,
            error_type="authentication_error",
        )
    if persist_prompt and has_keyring_library():
        save_password(alias, email_address, value)
    return value


def prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    if base in {"", ".", ".."}:
        raise MailCtlError(
            f"invalid attachment filename: {name}",
            exit_code=EXIT_INVALID_FILE,
            error_type="invalid_file_error",
        )
    base = re.sub(r"[^\w.\-+@]", "_", base)
    if not base:
        raise MailCtlError(
            f"invalid attachment filename: {name}",
            exit_code=EXIT_INVALID_FILE,
            error_type="invalid_file_error",
        )
    return base


def resolve_file(path_value: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise MailCtlError(
            f"file not found: {path}",
            exit_code=EXIT_INVALID_FILE,
            error_type="invalid_file_error",
        )
    if path.is_dir():
        raise MailCtlError(
            f"directories are not valid attachments: {path}",
            exit_code=EXIT_INVALID_FILE,
            error_type="invalid_file_error",
        )
    if not os.access(path, os.R_OK):
        raise MailCtlError(
            f"file is not readable: {path}",
            exit_code=EXIT_INVALID_FILE,
            error_type="invalid_file_error",
        )
    return path


def read_text_option(inline: str | None, file_value: str | None) -> str | None:
    if inline is not None and file_value is not None:
        raise MailCtlError(
            "use either inline text or file input, not both",
            exit_code=EXIT_USAGE,
            error_type="usage_error",
        )
    if file_value is not None:
        return resolve_file(file_value).read_text(encoding="utf-8")
    return inline


def collect_attachment_specs(values: list[str] | None) -> list[AttachmentSpec]:
    specs: list[AttachmentSpec] = []
    for value in values or []:
        path = resolve_file(value)
        mime_type, _ = mimetypes.guess_type(path.name)
        specs.append(
            AttachmentSpec(
                path=path,
                filename=path.name,
                size=path.stat().st_size,
                mime_type=mime_type or "application/octet-stream",
            )
        )
    return specs


def html_to_text(value: str) -> str:
    class _Stripper(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

        def get_text(self) -> str:
            text = "".join(self.parts)
            text = html.unescape(text)
            text = re.sub(r"\r\n?", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()

    parser = _Stripper()
    parser.feed(value)
    return parser.get_text()


def extract_message_text(message: EmailMessage) -> str:
    if message.is_multipart():
        html_fallback: str | None = None
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            try:
                payload = part.get_content()
            except Exception:
                continue
            if content_type == "text/plain" and isinstance(payload, str):
                return payload.strip()
            if content_type == "text/html" and isinstance(payload, str):
                html_fallback = payload
        return html_to_text(html_fallback or "")
    try:
        payload = message.get_content()
    except Exception:
        payload = ""
    if message.get_content_type() == "text/html":
        return html_to_text(str(payload))
    return str(payload).strip()


def draft_path(draft_id: str) -> Path:
    return drafts_root() / f"{draft_id}.json"


def append_history(entry: dict[str, Any]) -> None:
    ensure_dir(state_root())
    path = history_path()
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
    if not path.exists():
        os.chmod(path, 0o600)
    else:
        os.chmod(path, 0o600)


def load_history() -> list[dict[str, Any]]:
    path = history_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def account_connectivity_check(account: dict[str, Any], alias: str) -> dict[str, str]:
    password = get_password(alias, account["email"])
    email_addr = account["email"]
    try:
        with smtplib.SMTP_SSL(
            account["smtp_host"],
            int(account["smtp_port"]),
            context=create_ssl_context(),
            timeout=TIMEOUT_SECONDS,
        ) as smtp:
            smtp.login(email_addr, password)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailCtlError(
            f"SMTP authentication failed for {alias}",
            exit_code=EXIT_AUTH,
            error_type="authentication_error",
        ) from exc
    except (OSError, SocketTimeout) as exc:
        raise MailCtlError(
            f"SMTP connection failed: {exc}",
            exit_code=EXIT_NETWORK,
            error_type="network_error",
        ) from exc
    try:
        imap = imaplib.IMAP4_SSL(
            account["imap_host"],
            int(account["imap_port"]),
            ssl_context=create_ssl_context(),
            timeout=TIMEOUT_SECONDS,
        )
        try:
            imap.login(email_addr, password)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as exc:
        raise MailCtlError(
            f"IMAP authentication failed for {alias}",
            exit_code=EXIT_AUTH,
            error_type="authentication_error",
        ) from exc
    except (OSError, SocketTimeout) as exc:
        raise MailCtlError(
            f"IMAP connection failed: {exc}",
            exit_code=EXIT_NETWORK,
            error_type="network_error",
        ) from exc
    return {"smtp": "ok", "imap": "ok"}


def compose_message(
    args: argparse.Namespace,
    *,
    config: dict[str, Any],
    reply_context: dict[str, Any] | None = None,
) -> MessageBuildResult:
    alias, account = get_account(config, getattr(args, "account", None))
    sender = validate_email(account["email"])
    requested_from = getattr(args, "from_address", None)
    if requested_from and validate_email(requested_from) != sender:
        raise MailCtlError(
            "custom --from is not allowed unless it matches the authenticated account",
            exit_code=EXIT_USAGE,
            error_type="usage_error",
        )
    to_list = split_addresses(getattr(args, "to", None))
    cc_list = split_addresses(getattr(args, "cc", None))
    bcc_list = split_addresses(getattr(args, "bcc", None))
    if reply_context and not to_list:
        to_list = reply_context["to"]
        cc_list = reply_context["cc"] if getattr(args, "reply_all", False) else []
    recipients = to_list + cc_list + bcc_list
    if not recipients:
        raise MailCtlError(
            "at least one recipient is required",
            exit_code=EXIT_USAGE,
            error_type="usage_error",
        )
    if len(recipients) > 50 and not getattr(args, "allow_many", False):
        raise MailCtlError(
            "more than 50 recipients requires --allow-many",
            exit_code=EXIT_USAGE,
            error_type="usage_error",
        )
    body = read_text_option(getattr(args, "body", None), getattr(args, "body_file", None))
    html_body = read_text_option(getattr(args, "html", None), getattr(args, "html_file", None))
    if reply_context and getattr(args, "quote", False):
        quoted = "\n".join(f"> {line}" for line in reply_context["body"].splitlines())
        body = ((body or "").rstrip() + ("\n\n" if body else "") + quoted).strip()
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((account.get("display_name") or "", sender))
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    reply_to = split_addresses(getattr(args, "reply_to", None))
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to)
    subject = getattr(args, "subject", None) or ""
    if reply_context:
        subject = subject or reply_context["subject"]
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        if reply_context.get("message_id"):
            msg["In-Reply-To"] = reply_context["message_id"]
        references = reply_context.get("references", [])
        if reply_context.get("message_id"):
            references = references + [reply_context["message_id"]]
        if references:
            msg["References"] = " ".join(references)
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(datetime.now().astimezone())
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.split("@", 1)[1])
    if body is not None and html_body is not None:
        msg.set_content(body)
        msg.add_alternative(html_body, subtype="html")
    elif html_body is not None:
        msg.set_content(html_to_text(html_body) or "")
        msg.add_alternative(html_body, subtype="html")
    else:
        msg.set_content(body or "")
    attachments = collect_attachment_specs(getattr(args, "attach", None))
    for spec in attachments:
        maintype, subtype = spec.mime_type.split("/", 1)
        msg.add_attachment(
            spec.path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=spec.filename,
        )
    total_size = len(msg.as_bytes(policy=policy.SMTP))
    return MessageBuildResult(
        message=msg,
        attachments=attachments,
        recipients=recipients,
        account_alias=alias,
        account_email=sender,
        subject=subject,
        total_size=total_size,
    )


def summarize_message(build: MessageBuildResult) -> dict[str, Any]:
    return {
        "account": build.account_alias,
        "from": build.account_email,
        "to": build.message.get_all("To", []),
        "cc": build.message.get_all("Cc", []),
        "bcc_count": len(build.recipients) - len(split_addresses(build.message.get_all("To", []))) - len(split_addresses(build.message.get_all("Cc", []))),
        "subject": build.subject,
        "total_size": build.total_size,
        "attachments": [
            {"filename": item.filename, "size": item.size, "mime_type": item.mime_type}
            for item in build.attachments
        ],
        "recipient_count": len(build.recipients),
        "gmail_size_warning": build.total_size >= GMAIL_WARNING_SIZE,
    }


def send_via_smtp(build: MessageBuildResult, account: dict[str, Any], alias: str) -> None:
    password = get_password(alias, account["email"])
    try:
        with smtplib.SMTP_SSL(
            account["smtp_host"],
            int(account["smtp_port"]),
            context=create_ssl_context(),
            timeout=TIMEOUT_SECONDS,
        ) as smtp:
            smtp.login(account["email"], password)
            smtp.send_message(build.message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailCtlError(
            "SMTP authentication failed",
            exit_code=EXIT_AUTH,
            error_type="authentication_error",
        ) from exc
    except smtplib.SMTPException as exc:
        raise MailCtlError(
            f"SMTP server error: {exc}",
            exit_code=EXIT_SERVER,
            error_type="server_error",
        ) from exc
    except (OSError, SocketTimeout) as exc:
        raise MailCtlError(
            f"SMTP network error: {exc}",
            exit_code=EXIT_NETWORK,
            error_type="network_error",
        ) from exc


def connect_imap(account: dict[str, Any], alias: str) -> imaplib.IMAP4_SSL:
    password = get_password(alias, account["email"])
    try:
        client = imaplib.IMAP4_SSL(
            account["imap_host"],
            int(account["imap_port"]),
            ssl_context=create_ssl_context(),
            timeout=TIMEOUT_SECONDS,
        )
        client.login(account["email"], password)
        return client
    except imaplib.IMAP4.error as exc:
        raise MailCtlError(
            "IMAP authentication failed",
            exit_code=EXIT_AUTH,
            error_type="authentication_error",
        ) from exc
    except (OSError, SocketTimeout) as exc:
        raise MailCtlError(
            f"IMAP network error: {exc}",
            exit_code=EXIT_NETWORK,
            error_type="network_error",
        ) from exc


def imap_select(client: imaplib.IMAP4_SSL, folder: str) -> None:
    status, _ = client.select(f'"{folder}"')
    if status != "OK":
        raise MailCtlError(
            f"unable to select folder: {folder}",
            exit_code=EXIT_SERVER,
            error_type="server_error",
        )


def parse_headers(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def fetch_message_by_uid(client: imaplib.IMAP4_SSL, uid: str) -> EmailMessage:
    status, data = client.uid("fetch", uid, "(RFC822)")
    if status != "OK" or not data or not data[0]:
        raise MailCtlError(
            f"message not found for uid {uid}",
            exit_code=EXIT_SERVER,
            error_type="server_error",
        )
    raw = data[0][1]
    return parse_headers(raw)


def search_uids(client: imaplib.IMAP4_SSL, *, unread: bool = False, from_filter: str | None = None, subject_filter: str | None = None) -> list[str]:
    criteria = ["ALL"]
    if unread:
        criteria.append("UNSEEN")
    if from_filter:
        criteria.extend(["FROM", f'"{from_filter}"'])
    if subject_filter:
        criteria.extend(["SUBJECT", f'"{subject_filter}"'])
    status, data = client.uid("search", None, *criteria)
    if status != "OK":
        raise MailCtlError(
            "IMAP search failed",
            exit_code=EXIT_SERVER,
            error_type="server_error",
        )
    return [item for item in data[0].decode().split() if item]


def list_messages(client: imaplib.IMAP4_SSL, uids: list[str], *, limit: int | None = None) -> list[dict[str, Any]]:
    selected = uids[-limit:] if limit else uids
    result: list[dict[str, Any]] = []
    for uid in reversed(selected):
        status, data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)] FLAGS BODYSTRUCTURE)")
        if status != "OK" or not data or not data[0]:
            continue
        header_bytes = b""
        flags_text = ""
        for item in data:
            if isinstance(item, tuple):
                if isinstance(item[1], bytes):
                    header_bytes = item[1]
                flags_text += str(item[0])
        message = parse_headers(header_bytes)
        result.append(
            {
                "uid": uid,
                "date": message.get("Date", ""),
                "from": message.get("From", ""),
                "subject": message.get("Subject", ""),
                "unread": "\\Seen" not in flags_text,
                "has_attachments": "ATTACHMENT" in flags_text.upper() or "MIXED" in flags_text.upper(),
            }
        )
    return result


def attachment_metadata(message: EmailMessage) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, part in enumerate(message.iter_attachments(), start=1):
        payload = part.get_payload(decode=True) or b""
        items.append(
            {
                "index": index,
                "filename": sanitize_filename(part.get_filename() or f"attachment-{index}"),
                "content_type": part.get_content_type(),
                "size": len(payload),
            }
        )
    return items


def download_attachments(message: EmailMessage, output_dir: Path, *, overwrite: bool, assume_yes: bool) -> list[dict[str, Any]]:
    ensure_dir(output_dir)
    downloads: list[dict[str, Any]] = []
    for item in attachment_metadata(message):
        target = output_dir / item["filename"]
        if target.exists() and not overwrite:
            if not confirm(f"Overwrite {target}?", assume_yes=assume_yes):
                continue
        part = list(message.iter_attachments())[item["index"] - 1]
        target.write_bytes(part.get_payload(decode=True) or b"")
        downloads.append({"filename": item["filename"], "path": str(target), "size": item["size"]})
    return downloads


def format_human_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"Account: {summary['account']}",
        f"From: {summary['from']}",
        f"To: {', '.join(summary['to']) or '-'}",
        f"Cc: {', '.join(summary['cc']) or '-'}",
        f"Subject: {summary['subject'] or '-'}",
        f"Recipients: {summary['recipient_count']}",
        f"Total size: {summary['total_size']} bytes",
    ]
    if summary["attachments"]:
        lines.append("Attachments:")
        for item in summary["attachments"]:
            lines.append(f"- {item['filename']} ({item['size']} bytes)")
    if summary["gmail_size_warning"]:
        lines.append("Warning: message size is near Gmail limits.")
    if summary["recipient_count"] > 10:
        lines.append("Warning: more than 10 recipients.")
    return "\n".join(lines)


def command_install(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parent
    venv_path = runtime_venv_path()
    ensure_dir(runtime_root())
    try:
        if not venv_path.exists():
            venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(venv_path)
        python_bin = runtime_python_path()
        if not python_bin.exists():
            raise MailCtlError(
                "virtual environment python was not created correctly",
                exit_code=EXIT_CONFIG,
                error_type="configuration_error",
            )
        subprocess.run(
            [str(python_bin), "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
            cwd=project_root,
        )
        subprocess.run(
            [str(python_bin), "-m", "pip", "install", "."],
            check=True,
            cwd=project_root,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise MailCtlError(
            f"installation failed: {exc}",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        ) from exc
    target = install_path()
    ensure_dir(target.parent)
    launcher = "\n".join(
        [
            "#!/usr/bin/env bash",
            f'exec "{venv_path / "bin" / "mailctl"}" "$@"',
            "",
        ]
    )
    target.write_text(launcher, encoding="utf-8")
    os.chmod(target, 0o755)
    in_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
    result = {
        "installed_to": str(target),
        "runtime": str(venv_path),
        "path_ready": in_path,
        "path_hint": None if in_path else 'Add `export PATH="$HOME/.local/bin:$PATH"` to your shell config.',
    }
    return output_result(args, "install", result)


def command_uninstall(args: argparse.Namespace) -> int:
    target = install_path()
    runtime = runtime_root()
    if not target.exists() and not runtime.exists():
        raise MailCtlError(
            "mailctl is not installed in ~/.local/bin or ~/.local/share/mailctl",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )
    if not confirm(f"Remove installed mailctl runtime from {target} and {runtime}?", assume_yes=getattr(args, "yes", False)):
        raise MailCtlError("operation canceled", exit_code=EXIT_CANCELED, error_type="canceled")
    if target.exists():
        target.unlink()
    if runtime.exists():
        shutil.rmtree(runtime)
    return output_result(args, "uninstall", {"removed": [str(target), str(runtime)]})


def command_account_add(args: argparse.Namespace) -> int:
    config = load_config()
    alias = normalize_alias(args.alias or prompt_text("Alias"))
    if alias in config["accounts"]:
        raise MailCtlError(
            f"account already exists: {alias}",
            exit_code=EXIT_CONFLICT,
            error_type="conflict_error",
        )
    provider = (args.provider or prompt_text("Provider", "gmail")).strip().lower()
    email_addr = validate_email(args.email or prompt_text("Email"))
    display_name = args.name or prompt_text("Display name")
    account = {
        "email": email_addr,
        "display_name": display_name,
    }
    if provider == "gmail":
        account.update(
            {
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 465,
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
            }
        )
    else:
        account.update(
            {
                "smtp_host": args.smtp_host or prompt_text("SMTP host"),
                "smtp_port": int(args.smtp_port or prompt_text("SMTP port")),
                "imap_host": args.imap_host or prompt_text("IMAP host"),
                "imap_port": int(args.imap_port or prompt_text("IMAP port")),
            }
        )
    config["accounts"][alias] = account
    if not getattr(args, "skip_password", False):
        password = getpass.getpass(f"App Password for {alias}: ")
        save_password(alias, email_addr, password)
    if not config.get("default_account"):
        config["default_account"] = alias
    save_config(config)
    return output_result(
        args,
        "account_add",
        {
            "alias": alias,
            "default": config.get("default_account") == alias,
            "credential_store": "keyring" if not getattr(args, "skip_password", False) else "not_set",
            "env_password_var": password_env_name(alias),
        },
    )


def command_account_list(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    items = []
    for alias, account in sorted(config["accounts"].items()):
        items.append(
            {
                "alias": alias,
                "email": account["email"],
                "display_name": account.get("display_name", ""),
                "default": alias == config.get("default_account"),
            }
        )
    if args.json:
        return output_result(args, "account_list", items)
    lines = []
    for item in items:
        marker = "*" if item["default"] else "-"
        lines.append(f"{marker} {item['alias']} <{item['email']}>")
    return output_result(args, "account_list", "\n".join(lines) if lines else "No accounts configured.")


def command_account_show(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, account = get_account(config, args.alias)
    result = {"alias": alias, **account, "default": alias == config.get("default_account")}
    return output_result(args, "account_show", result)


def command_account_use(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, _ = get_account(config, args.alias)
    config["default_account"] = alias
    save_config(config)
    return output_result(args, "account_use", {"default_account": alias})


def command_account_remove(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, account = get_account(config, args.alias)
    del config["accounts"][alias]
    if config.get("default_account") == alias:
        config["default_account"] = next(iter(config["accounts"]), None)
    save_config(config)
    delete_password(alias, account["email"])
    return output_result(args, "account_remove", {"removed": alias, "default_account": config.get("default_account")})


def command_account_test(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, account = get_account(config, args.alias)
    result = account_connectivity_check(account, alias)
    return output_result(args, "account_test", {"alias": alias, **result})


def command_account_password_set(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, account = get_account(config, args.alias)
    password = getpass.getpass(f"App Password for {alias}: ")
    save_password(alias, account["email"], password)
    return output_result(
        args,
        "account_password_set",
        {"alias": alias, "credential_store": "keyring", "backend": keyring_backend_name()},
    )


def command_account_password_delete(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, account = get_account(config, args.alias)
    deleted = delete_password(alias, account["email"])
    return output_result(args, "account_password_delete", {"alias": alias, "deleted": deleted})


def command_account_password_status(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, account = get_account(config, args.alias)
    result = {
        "alias": alias,
        "stored_in_keyring": bool(get_stored_password(alias, account["email"])),
        "keyring_library": has_keyring_library(),
        "keyring_backend": keyring_backend_name(),
        "env_password_var": password_env_name(alias),
    }
    return output_result(args, "account_password_status", result)


def maybe_write_draft(build: MessageBuildResult, args: argparse.Namespace) -> int:
    ensure_dir(drafts_root())
    draft_id = uuid.uuid4().hex[:12]
    data = {
        "id": draft_id,
        "timestamp": now_iso(),
        "account": build.account_alias,
        "from": build.account_email,
        "to": split_addresses(getattr(args, "to", None)),
        "cc": split_addresses(getattr(args, "cc", None)),
        "bcc": split_addresses(getattr(args, "bcc", None)),
        "reply_to": split_addresses(getattr(args, "reply_to", None)),
        "subject": build.subject,
        "body": getattr(args, "body", None),
        "body_file": getattr(args, "body_file", None),
        "html": getattr(args, "html", None),
        "html_file": getattr(args, "html_file", None),
        "attach": getattr(args, "attach", None) or [],
    }
    write_private_json(draft_path(draft_id), data)
    return output_result(args, "draft_create", {"draft_id": draft_id})


def command_send(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    build = compose_message(args, config=config)
    summary = summarize_message(build)
    if getattr(args, "draft", False):
        return maybe_write_draft(build, args)
    if not args.json:
        print(format_human_summary(summary))
    if args.dry_run:
        return output_result(args, "send", summary)
    if not confirm("Send this email?", assume_yes=args.yes):
        raise MailCtlError("operation canceled", exit_code=EXIT_CANCELED, error_type="canceled")
    _, account = get_account(config, args.account)
    send_via_smtp(build, account, build.account_alias)
    entry = {
        "timestamp": now_iso(),
        "account": build.account_alias,
        "to": split_addresses(args.to),
        "subject": build.subject,
        "attachments": [item.filename for item in build.attachments],
        "message_id": build.message["Message-ID"],
        "status": "sent",
    }
    append_history(entry)
    return output_result(args, "send", {"message_id": build.message["Message-ID"], "status": "sent"})


def command_history_list(args: argparse.Namespace) -> int:
    entries = load_history()
    return output_result(args, "history_list", entries)


def command_history_show(args: argparse.Namespace) -> int:
    for entry in load_history():
        if entry.get("message_id") == args.message_id:
            return output_result(args, "history_show", entry)
    raise MailCtlError(
        f"message_id not found: {args.message_id}",
        exit_code=EXIT_CONFIG,
        error_type="configuration_error",
    )


def command_history_search(args: argparse.Namespace) -> int:
    term = args.recipient.lower()
    matches = [item for item in load_history() if term in " ".join(item.get("to", [])).lower()]
    return output_result(args, "history_search", matches)


def imap_context(config: dict[str, Any], alias: str | None) -> tuple[str, dict[str, Any], imaplib.IMAP4_SSL]:
    use_alias, account = get_account(config, alias)
    client = connect_imap(account, use_alias)
    return use_alias, account, client


def command_folders(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        status, data = client.list()
        if status != "OK":
            raise MailCtlError("unable to list folders", exit_code=EXIT_SERVER, error_type="server_error")
        folders = []
        for item in data:
            decoded = item.decode(errors="replace")
            folders.append(decoded.split(' "/" ')[-1].strip('"'))
        return output_result(args, "folders", folders)
    finally:
        client.logout()


def command_inbox(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        uids = search_uids(client, unread=args.unread)
        result = list_messages(client, uids, limit=args.limit)
        return output_result(args, "inbox", result)
    finally:
        client.logout()


def command_search(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        uids = search_uids(client, unread=args.unread, from_filter=args.from_address, subject_filter=args.subject)
        result = list_messages(client, uids, limit=args.limit)
        return output_result(args, "search", result)
    finally:
        client.logout()


def command_read(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        message = fetch_message_by_uid(client, args.uid)
        result = {
            "uid": args.uid,
            "subject": message.get("Subject", ""),
            "from": message.get("From", ""),
            "date": message.get("Date", ""),
            "text": extract_message_text(message),
        }
        return output_result(args, "read", result)
    finally:
        client.logout()


def command_attachments(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        message = fetch_message_by_uid(client, args.uid)
        return output_result(args, "attachments", attachment_metadata(message))
    finally:
        client.logout()


def command_download(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        message = fetch_message_by_uid(client, args.uid)
        output_dir = Path(args.output).expanduser().resolve() if args.output else default_download_dir().resolve()
        result = download_attachments(message, output_dir, overwrite=args.overwrite, assume_yes=args.yes)
        return output_result(args, "download", result)
    finally:
        client.logout()


def command_mark(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        flag_action = "+FLAGS" if args.read else "-FLAGS"
        flag = "(\\Seen)"
        status, _ = client.uid("store", args.uid, flag_action, flag)
        if status != "OK":
            raise MailCtlError("unable to update flags", exit_code=EXIT_SERVER, error_type="server_error")
        return output_result(args, "mark", {"uid": args.uid, "read": args.read})
    finally:
        client.logout()


def command_move(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        status, _ = client.uid("copy", args.uid, args.target_folder)
        if status != "OK":
            raise MailCtlError("unable to copy message to target folder", exit_code=EXIT_SERVER, error_type="server_error")
        client.uid("store", args.uid, "+FLAGS", "(\\Deleted)")
        client.expunge()
        return output_result(args, "move", {"uid": args.uid, "folder": args.target_folder})
    finally:
        client.logout()


def command_delete(args: argparse.Namespace) -> int:
    if not confirm(f"Delete message {args.uid}?", assume_yes=args.yes):
        raise MailCtlError("operation canceled", exit_code=EXIT_CANCELED, error_type="canceled")
    config = load_config(required=True)
    _, _, client = imap_context(config, args.account)
    try:
        imap_select(client, args.folder)
        status, _ = client.uid("store", args.uid, "+FLAGS", "(\\Deleted)")
        if status != "OK":
            raise MailCtlError("unable to mark message for deletion", exit_code=EXIT_SERVER, error_type="server_error")
        client.expunge()
        return output_result(args, "delete", {"uid": args.uid, "deleted": True})
    finally:
        client.logout()


def build_reply_context(message: EmailMessage, account_email: str, reply_all: bool) -> dict[str, Any]:
    sender = validate_email(email.utils.parseaddr(message.get("Reply-To") or message.get("From") or "")[1])
    all_to = [sender]
    cc: list[str] = []
    if reply_all:
        seen = {account_email.lower(), sender.lower()}
        for field in ("To", "Cc"):
            for _, addr in email.utils.getaddresses(message.get_all(field, [])):
                if addr and addr.lower() not in seen:
                    seen.add(addr.lower())
                    if field == "To":
                        all_to.append(validate_email(addr))
                    else:
                        cc.append(validate_email(addr))
    refs = (message.get("References") or "").split()
    return {
        "to": all_to,
        "cc": cc,
        "subject": message.get("Subject", ""),
        "message_id": message.get("Message-ID", ""),
        "references": refs,
        "body": extract_message_text(message),
    }


def command_reply(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    alias, account = get_account(config, args.account)
    client = connect_imap(account, alias)
    try:
        imap_select(client, args.folder)
        source_message = fetch_message_by_uid(client, args.uid)
    finally:
        client.logout()
    reply_context = build_reply_context(source_message, account["email"], args.reply_all)
    build = compose_message(args, config=config, reply_context=reply_context)
    summary = summarize_message(build)
    if not args.json:
        print(format_human_summary(summary))
    if args.dry_run:
        return output_result(args, "reply", summary)
    if not confirm("Send this email?", assume_yes=args.yes):
        raise MailCtlError("operation canceled", exit_code=EXIT_CANCELED, error_type="canceled")
    send_via_smtp(build, account, alias)
    append_history(
        {
            "timestamp": now_iso(),
            "account": alias,
            "to": reply_context["to"],
            "subject": build.subject,
            "attachments": [item.filename for item in build.attachments],
            "message_id": build.message["Message-ID"],
            "status": "sent",
        }
    )
    return output_result(args, "reply", {"message_id": build.message["Message-ID"], "status": "sent"})


def load_draft(draft_id: str) -> dict[str, Any]:
    path = draft_path(draft_id)
    if not path.exists():
        raise MailCtlError(
            f"draft not found: {draft_id}",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def command_draft_create(args: argparse.Namespace) -> int:
    config = load_config(required=True)
    build = compose_message(args, config=config)
    return maybe_write_draft(build, args)


def command_draft_list(args: argparse.Namespace) -> int:
    if not drafts_root().exists():
        return output_result(args, "draft_list", [])
    drafts = []
    for path in sorted(drafts_root().glob("*.json")):
        draft = json.loads(path.read_text(encoding="utf-8"))
        drafts.append({"id": draft["id"], "account": draft["account"], "subject": draft.get("subject", ""), "timestamp": draft.get("timestamp", "")})
    return output_result(args, "draft_list", drafts)


def command_draft_show(args: argparse.Namespace) -> int:
    return output_result(args, "draft_show", load_draft(args.draft_id))


def args_from_draft(draft: dict[str, Any], parser: argparse.ArgumentParser) -> argparse.Namespace:
    values = [
        "send",
        "--account",
        draft["account"],
        "--subject",
        draft.get("subject", ""),
    ]
    for field in ("to", "cc", "bcc", "reply_to", "attach"):
        key = field.replace("_", "-")
        for item in draft.get(field, []):
            values.extend([f"--{key}", item])
    if draft.get("body") is not None:
        values.extend(["--body", draft["body"]])
    if draft.get("html") is not None:
        values.extend(["--html", draft["html"]])
    return parser.parse_args(values)


def command_draft_send(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    draft = load_draft(args.draft_id)
    send_args = args_from_draft(draft, parser)
    send_args.yes = args.yes
    send_args.json = args.json
    result = command_send(send_args)
    draft_path(args.draft_id).unlink(missing_ok=True)
    return result


def command_draft_delete(args: argparse.Namespace) -> int:
    path = draft_path(args.draft_id)
    if not path.exists():
        raise MailCtlError(
            f"draft not found: {args.draft_id}",
            exit_code=EXIT_CONFIG,
            error_type="configuration_error",
        )
    if not confirm(f"Delete draft {args.draft_id}?", assume_yes=args.yes):
        raise MailCtlError("operation canceled", exit_code=EXIT_CANCELED, error_type="canceled")
    path.unlink()
    return output_result(args, "draft_delete", {"deleted": args.draft_id})


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit stable JSON output")


def add_account_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", help="account alias")


def add_folder_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--folder", default="INBOX", help="IMAP folder name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailctl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_cmd = subparsers.add_parser("install")
    add_json_flag(install_cmd)
    install_cmd.set_defaults(func=command_install)

    uninstall_cmd = subparsers.add_parser("uninstall")
    add_json_flag(uninstall_cmd)
    uninstall_cmd.add_argument("--yes", action="store_true")
    uninstall_cmd.set_defaults(func=command_uninstall)

    account_cmd = subparsers.add_parser("account")
    account_sub = account_cmd.add_subparsers(dest="account_command", required=True)
    account_add = account_sub.add_parser("add")
    add_json_flag(account_add)
    account_add.add_argument("--alias")
    account_add.add_argument("--email")
    account_add.add_argument("--name")
    account_add.add_argument("--provider")
    account_add.add_argument("--smtp-host")
    account_add.add_argument("--smtp-port", type=int)
    account_add.add_argument("--imap-host")
    account_add.add_argument("--imap-port", type=int)
    account_add.add_argument("--skip-password", action="store_true")
    account_add.set_defaults(func=command_account_add)
    account_list = account_sub.add_parser("list")
    add_json_flag(account_list)
    account_list.set_defaults(func=command_account_list)
    account_show = account_sub.add_parser("show")
    add_json_flag(account_show)
    account_show.add_argument("alias")
    account_show.set_defaults(func=command_account_show)
    account_use = account_sub.add_parser("use")
    add_json_flag(account_use)
    account_use.add_argument("alias")
    account_use.set_defaults(func=command_account_use)
    account_remove = account_sub.add_parser("remove")
    add_json_flag(account_remove)
    account_remove.add_argument("alias")
    account_remove.set_defaults(func=command_account_remove)
    account_test = account_sub.add_parser("test")
    add_json_flag(account_test)
    account_test.add_argument("alias")
    account_test.set_defaults(func=command_account_test)
    account_password = account_sub.add_parser("password")
    account_password_sub = account_password.add_subparsers(dest="account_password_command", required=True)
    account_password_set = account_password_sub.add_parser("set")
    add_json_flag(account_password_set)
    account_password_set.add_argument("alias")
    account_password_set.set_defaults(func=command_account_password_set)
    account_password_delete = account_password_sub.add_parser("delete")
    add_json_flag(account_password_delete)
    account_password_delete.add_argument("alias")
    account_password_delete.set_defaults(func=command_account_password_delete)
    account_password_status = account_password_sub.add_parser("status")
    add_json_flag(account_password_status)
    account_password_status.add_argument("alias")
    account_password_status.set_defaults(func=command_account_password_status)

    send_cmd = subparsers.add_parser("send")
    add_json_flag(send_cmd)
    add_account_flag(send_cmd)
    send_cmd.add_argument("--from", dest="from_address")
    send_cmd.add_argument("--to", action="append")
    send_cmd.add_argument("--cc", action="append")
    send_cmd.add_argument("--bcc", action="append")
    send_cmd.add_argument("--reply-to", action="append")
    send_cmd.add_argument("--subject", default="")
    send_cmd.add_argument("--body")
    send_cmd.add_argument("--body-file")
    send_cmd.add_argument("--html")
    send_cmd.add_argument("--html-file")
    send_cmd.add_argument("--attach", action="append")
    send_cmd.add_argument("--draft", action="store_true")
    send_cmd.add_argument("--dry-run", action="store_true")
    send_cmd.add_argument("--yes", action="store_true")
    send_cmd.add_argument("--allow-many", action="store_true")
    send_cmd.set_defaults(func=command_send)

    history_cmd = subparsers.add_parser("history")
    history_sub = history_cmd.add_subparsers(dest="history_command", required=True)
    history_list = history_sub.add_parser("list")
    add_json_flag(history_list)
    history_list.set_defaults(func=command_history_list)
    history_show = history_sub.add_parser("show")
    add_json_flag(history_show)
    history_show.add_argument("message_id")
    history_show.set_defaults(func=command_history_show)
    history_search = history_sub.add_parser("search")
    add_json_flag(history_search)
    history_search.add_argument("--recipient", required=True)
    history_search.set_defaults(func=command_history_search)

    folders_cmd = subparsers.add_parser("folders")
    add_json_flag(folders_cmd)
    add_account_flag(folders_cmd)
    folders_cmd.set_defaults(func=command_folders)

    inbox_cmd = subparsers.add_parser("inbox")
    add_json_flag(inbox_cmd)
    add_account_flag(inbox_cmd)
    add_folder_flag(inbox_cmd)
    inbox_cmd.add_argument("--limit", type=int, default=20)
    inbox_cmd.add_argument("--unread", action="store_true")
    inbox_cmd.set_defaults(func=command_inbox)

    search_cmd = subparsers.add_parser("search")
    add_json_flag(search_cmd)
    add_account_flag(search_cmd)
    add_folder_flag(search_cmd)
    search_cmd.add_argument("--limit", type=int, default=20)
    search_cmd.add_argument("--unread", action="store_true")
    search_cmd.add_argument("--from", dest="from_address")
    search_cmd.add_argument("--subject")
    search_cmd.set_defaults(func=command_search)

    read_cmd = subparsers.add_parser("read")
    add_json_flag(read_cmd)
    add_account_flag(read_cmd)
    add_folder_flag(read_cmd)
    read_cmd.add_argument("uid")
    read_cmd.set_defaults(func=command_read)

    attach_cmd = subparsers.add_parser("attachments")
    add_json_flag(attach_cmd)
    add_account_flag(attach_cmd)
    add_folder_flag(attach_cmd)
    attach_cmd.add_argument("uid")
    attach_cmd.set_defaults(func=command_attachments)

    download_cmd = subparsers.add_parser("download")
    add_json_flag(download_cmd)
    add_account_flag(download_cmd)
    add_folder_flag(download_cmd)
    download_cmd.add_argument("uid")
    download_cmd.add_argument("--output")
    download_cmd.add_argument("--overwrite", action="store_true")
    download_cmd.add_argument("--yes", action="store_true")
    download_cmd.set_defaults(func=command_download)

    mark_cmd = subparsers.add_parser("mark")
    add_json_flag(mark_cmd)
    add_account_flag(mark_cmd)
    add_folder_flag(mark_cmd)
    mark_cmd.add_argument("uid")
    group = mark_cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--read", action="store_true")
    group.add_argument("--unread", action="store_true")
    mark_cmd.set_defaults(func=command_mark)

    move_cmd = subparsers.add_parser("move")
    add_json_flag(move_cmd)
    add_account_flag(move_cmd)
    move_cmd.add_argument("uid")
    move_cmd.add_argument("--source-folder", dest="folder", default="INBOX")
    move_cmd.add_argument("--folder", dest="target_folder", required=True)
    move_cmd.set_defaults(func=command_move)

    delete_cmd = subparsers.add_parser("delete")
    add_json_flag(delete_cmd)
    add_account_flag(delete_cmd)
    add_folder_flag(delete_cmd)
    delete_cmd.add_argument("uid")
    delete_cmd.add_argument("--yes", action="store_true")
    delete_cmd.set_defaults(func=command_delete)

    reply_cmd = subparsers.add_parser("reply")
    add_json_flag(reply_cmd)
    add_account_flag(reply_cmd)
    add_folder_flag(reply_cmd)
    reply_cmd.add_argument("uid")
    reply_cmd.add_argument("--body")
    reply_cmd.add_argument("--body-file")
    reply_cmd.add_argument("--html-file")
    reply_cmd.add_argument("--subject")
    reply_cmd.add_argument("--attach", action="append")
    reply_cmd.add_argument("--reply-all", action="store_true")
    reply_cmd.add_argument("--dry-run", action="store_true")
    reply_cmd.add_argument("--yes", action="store_true")
    reply_cmd.add_argument("--quote", action="store_true")
    reply_cmd.set_defaults(func=command_reply)

    draft_cmd = subparsers.add_parser("draft")
    draft_sub = draft_cmd.add_subparsers(dest="draft_command", required=True)
    draft_create = draft_sub.add_parser("create")
    add_json_flag(draft_create)
    add_account_flag(draft_create)
    draft_create.add_argument("--to", action="append")
    draft_create.add_argument("--cc", action="append")
    draft_create.add_argument("--bcc", action="append")
    draft_create.add_argument("--reply-to", action="append")
    draft_create.add_argument("--subject", default="")
    draft_create.add_argument("--body")
    draft_create.add_argument("--body-file")
    draft_create.add_argument("--html")
    draft_create.add_argument("--html-file")
    draft_create.add_argument("--attach", action="append")
    draft_create.set_defaults(func=command_draft_create)
    draft_list = draft_sub.add_parser("list")
    add_json_flag(draft_list)
    draft_list.set_defaults(func=command_draft_list)
    draft_show = draft_sub.add_parser("show")
    add_json_flag(draft_show)
    draft_show.add_argument("draft_id")
    draft_show.set_defaults(func=command_draft_show)
    draft_send = draft_sub.add_parser("send")
    add_json_flag(draft_send)
    draft_send.add_argument("draft_id")
    draft_send.add_argument("--yes", action="store_true")
    draft_send.set_defaults(func=command_draft_send)
    draft_delete = draft_sub.add_parser("delete")
    add_json_flag(draft_delete)
    draft_delete.add_argument("draft_id")
    draft_delete.add_argument("--yes", action="store_true")
    draft_delete.set_defaults(func=command_draft_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "func", None) is command_draft_send:
            return args.func(args, parser)
        return args.func(args)
    except MailCtlError as exc:
        return output_error(args, exc)
    except KeyboardInterrupt:
        return output_error(args, MailCtlError("operation canceled", exit_code=EXIT_CANCELED, error_type="canceled"))


if __name__ == "__main__":
    sys.exit(main())
