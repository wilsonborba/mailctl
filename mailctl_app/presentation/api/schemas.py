from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AccountCreateRequest(BaseModel):
    alias: str
    email: str
    name: str
    provider: str = "gmail"
    smtp_host: str | None = None
    smtp_port: int | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    password: str | None = Field(default=None, description="App password stored in keyring")


class PasswordSetRequest(BaseModel):
    password: str


class SendMessageRequest(BaseModel):
    account: str | None = None
    from_address: str | None = Field(default=None, alias="from")
    to: list[str] | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    reply_to: list[str] | None = None
    subject: str = ""
    body: str | None = None
    body_file: str | None = None
    html: str | None = None
    html_file: str | None = None
    attach: list[str] | None = None
    draft: bool = False
    dry_run: bool = False
    yes: bool = True
    allow_many: bool = False


class ReplyRequest(BaseModel):
    account: str | None = None
    folder: str = "INBOX"
    body: str | None = None
    body_file: str | None = None
    html_file: str | None = None
    subject: str | None = None
    attach: list[str] | None = None
    reply_all: bool = False
    dry_run: bool = False
    yes: bool = True
    quote: bool = False


class DraftCreateRequest(BaseModel):
    account: str | None = None
    from_address: str | None = Field(default=None, alias="from")
    to: list[str] | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    reply_to: list[str] | None = None
    subject: str = ""
    body: str | None = None
    body_file: str | None = None
    html: str | None = None
    html_file: str | None = None
    attach: list[str] | None = None
    allow_many: bool = False


class DownloadRequest(BaseModel):
    account: str | None = None
    folder: str = "INBOX"
    output: str | None = None
    overwrite: bool = False
    yes: bool = True


class MarkRequest(BaseModel):
    account: str | None = None
    folder: str = "INBOX"
    read: bool = False
    unread: bool = False


class MoveRequest(BaseModel):
    account: str | None = None
    folder: str = "INBOX"
    target_folder: str


class DeleteRequest(BaseModel):
    account: str | None = None
    folder: str = "INBOX"
    yes: bool = True


class ApiServeRequest(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    localhost_only: bool = False


class ApiResponse(BaseModel):
    ok: bool
    command: str | None = None
    result: Any | None = None
    error: dict[str, Any] | None = None

