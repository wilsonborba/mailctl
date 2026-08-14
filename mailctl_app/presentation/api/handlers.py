from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any

from mailctl_app import legacy
from mailctl_app.domain.services import runtime_service


def run_legacy_command(argv: list[str]) -> dict[str, Any]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = legacy.main([*argv, "--json"])
    payload = json.loads(output.getvalue())
    payload["exit_code"] = code
    return payload


def namespace_from_payload(payload: dict[str, Any], **extra: Any) -> SimpleNamespace:
    values = dict(payload)
    values.update(extra)
    return SimpleNamespace(**values)


def list_accounts() -> dict[str, Any]:
    return run_legacy_command(["account", "list"])


def show_account(alias: str) -> dict[str, Any]:
    return run_legacy_command(["account", "show", alias])


def add_account(payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, json=True, skip_password=True)
    code, content = _capture_direct(legacy.command_account_add, args, "account_add")
    if payload.get("password"):
        legacy.save_password(payload["alias"], payload["email"], payload["password"])
        content["result"]["credential_store"] = "keyring"
        content["result"]["password_saved_via_api"] = True
    content["exit_code"] = code
    return content


def remove_account(alias: str) -> dict[str, Any]:
    return run_legacy_command(["account", "remove", alias])


def use_account(alias: str) -> dict[str, Any]:
    return run_legacy_command(["account", "use", alias])


def test_account(alias: str) -> dict[str, Any]:
    return run_legacy_command(["account", "test", alias])


def password_status(alias: str) -> dict[str, Any]:
    return run_legacy_command(["account", "password", "status", alias])


def password_set(alias: str, password: str) -> dict[str, Any]:
    config = legacy.load_config(required=True)
    normalized, account = legacy.get_account(config, alias)
    legacy.save_password(normalized, account["email"], password)
    return {"ok": True, "command": "account_password_set", "result": {"alias": normalized, "stored": True}, "exit_code": 0}


def password_delete(alias: str) -> dict[str, Any]:
    return run_legacy_command(["account", "password", "delete", alias])


def send_message(payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, json=True)
    code, content = _capture_direct(legacy.command_send, args, "send")
    content["exit_code"] = code
    return content


def reply_to_message(uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, uid=uid, json=True)
    code, content = _capture_direct(legacy.command_reply, args, "reply")
    content["exit_code"] = code
    return content


def history_list() -> dict[str, Any]:
    return run_legacy_command(["history", "list"])


def history_show(message_id: str) -> dict[str, Any]:
    return run_legacy_command(["history", "show", message_id])


def history_search(recipient: str) -> dict[str, Any]:
    return run_legacy_command(["history", "search", "--recipient", recipient])


def folders(account: str | None) -> dict[str, Any]:
    args = ["folders"]
    if account:
        args.extend(["--account", account])
    return run_legacy_command(args)


def inbox(*, account: str | None, folder: str, limit: int, unread: bool) -> dict[str, Any]:
    args = ["inbox", "--folder", folder, "--limit", str(limit)]
    if account:
        args.extend(["--account", account])
    if unread:
        args.append("--unread")
    return run_legacy_command(args)


def search_messages(*, account: str | None, folder: str, limit: int, unread: bool, from_address: str | None, subject: str | None) -> dict[str, Any]:
    args = ["search", "--folder", folder, "--limit", str(limit)]
    if account:
        args.extend(["--account", account])
    if unread:
        args.append("--unread")
    if from_address:
        args.extend(["--from", from_address])
    if subject:
        args.extend(["--subject", subject])
    return run_legacy_command(args)


def read_message(*, uid: str, account: str | None, folder: str) -> dict[str, Any]:
    args = ["read", uid, "--folder", folder]
    if account:
        args.extend(["--account", account])
    return run_legacy_command(args)


def attachments(*, uid: str, account: str | None, folder: str) -> dict[str, Any]:
    args = ["attachments", uid, "--folder", folder]
    if account:
        args.extend(["--account", account])
    return run_legacy_command(args)


def download_attachments(uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, uid=uid, json=True)
    code, content = _capture_direct(legacy.command_download, args, "download")
    content["exit_code"] = code
    return content


def mark_message(uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, uid=uid, json=True)
    code, content = _capture_direct(legacy.command_mark, args, "mark")
    content["exit_code"] = code
    return content


def move_message(uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, uid=uid, json=True)
    code, content = _capture_direct(legacy.command_move, args, "move")
    content["exit_code"] = code
    return content


def delete_message(uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, uid=uid, json=True)
    code, content = _capture_direct(legacy.command_delete, args, "delete")
    content["exit_code"] = code
    return content


def draft_create(payload: dict[str, Any]) -> dict[str, Any]:
    args = namespace_from_payload(payload, json=True)
    code, content = _capture_direct(legacy.command_draft_create, args, "draft_create")
    content["exit_code"] = code
    return content


def draft_list() -> dict[str, Any]:
    return run_legacy_command(["draft", "list"])


def draft_show(draft_id: str) -> dict[str, Any]:
    return run_legacy_command(["draft", "show", draft_id])


def draft_send(draft_id: str) -> dict[str, Any]:
    return run_legacy_command(["draft", "send", draft_id, "--yes"])


def draft_delete(draft_id: str) -> dict[str, Any]:
    return run_legacy_command(["draft", "delete", draft_id, "--yes"])


def api_status() -> dict[str, Any]:
    return {"ok": True, "command": "api_status", "result": runtime_service.service_status(), "exit_code": 0}


def _capture_direct(func: Any, args: Any, command_name: str) -> tuple[int, dict[str, Any]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = func(args)
    raw = output.getvalue().strip()
    if raw:
        return code, json.loads(raw)
    return code, {"ok": code == 0, "command": command_name, "result": None}

