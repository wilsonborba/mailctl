from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from mailctl_app.presentation.api import handlers, schemas


def create_app() -> FastAPI:
    app = FastAPI(
        title="mailctl API",
        version="1.1.0",
        description="HTTP API for the mailctl CLI capabilities, optimized for local and LAN use.",
        docs_url=None,
        redoc_url=None,
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": {"type": "http_error", "message": exc.detail}})
        return JSONResponse(status_code=500, content={"ok": False, "error": {"type": "internal_error", "message": str(exc)}})

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "service": "mailctl-api"}

    @app.get("/scalar", response_class=HTMLResponse)
    async def scalar_docs() -> str:
        return """<!doctype html>
<html>
  <head>
    <title>mailctl API Reference</title>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  </head>
  <body>
    <div id=\"app\"></div>
    <script src=\"https://cdn.jsdelivr.net/npm/@scalar/api-reference\"></script>
    <script>
      Scalar.createApiReference('#app', {
        url: '/openapi.json',
        theme: 'purple',
        layout: 'modern',
        showSidebar: true,
        showDeveloperTools: 'localhost'
      })
    </script>
  </body>
</html>"""

    def unwrap(payload: dict[str, object]) -> JSONResponse:
        if not payload.get("ok", False):
            error = payload.get("error") or {"message": "request failed"}
            exit_code = int(payload.get("exit_code", 1) or 1)
            status_code = {
                2: 400,
                3: 400,
                4: 401,
                5: 502,
                6: 500,
                7: 499,
                8: 400,
                9: 409,
            }.get(exit_code, 500)
            raise HTTPException(status_code=status_code, detail=error.get("message", "request failed"))
        return JSONResponse(status_code=200, content=payload)

    @app.get("/accounts", response_model=schemas.ApiResponse)
    async def list_accounts() -> JSONResponse:
        return unwrap(handlers.list_accounts())

    @app.post("/accounts", response_model=schemas.ApiResponse)
    async def create_account(request: schemas.AccountCreateRequest) -> JSONResponse:
        return unwrap(handlers.add_account(request.model_dump(by_alias=False)))

    @app.get("/accounts/{alias}", response_model=schemas.ApiResponse)
    async def get_account(alias: str) -> JSONResponse:
        return unwrap(handlers.show_account(alias))

    @app.delete("/accounts/{alias}", response_model=schemas.ApiResponse)
    async def delete_account(alias: str) -> JSONResponse:
        return unwrap(handlers.remove_account(alias))

    @app.post("/accounts/{alias}/use", response_model=schemas.ApiResponse)
    async def select_account(alias: str) -> JSONResponse:
        return unwrap(handlers.use_account(alias))

    @app.post("/accounts/{alias}/test", response_model=schemas.ApiResponse)
    async def test_account(alias: str) -> JSONResponse:
        return unwrap(handlers.test_account(alias))

    @app.get("/accounts/{alias}/password/status", response_model=schemas.ApiResponse)
    async def get_password_status(alias: str) -> JSONResponse:
        return unwrap(handlers.password_status(alias))

    @app.put("/accounts/{alias}/password", response_model=schemas.ApiResponse)
    async def set_password(alias: str, request: schemas.PasswordSetRequest) -> JSONResponse:
        return unwrap(handlers.password_set(alias, request.password))

    @app.delete("/accounts/{alias}/password", response_model=schemas.ApiResponse)
    async def delete_password(alias: str) -> JSONResponse:
        return unwrap(handlers.password_delete(alias))

    @app.post("/messages/send", response_model=schemas.ApiResponse)
    async def send_message(request: schemas.SendMessageRequest) -> JSONResponse:
        return unwrap(handlers.send_message(request.model_dump(by_alias=False)))

    @app.post("/messages/{uid}/reply", response_model=schemas.ApiResponse)
    async def reply_message(uid: str, request: schemas.ReplyRequest) -> JSONResponse:
        return unwrap(handlers.reply_to_message(uid, request.model_dump()))

    @app.get("/history", response_model=schemas.ApiResponse)
    async def list_history() -> JSONResponse:
        return unwrap(handlers.history_list())

    @app.get("/history/{message_id}", response_model=schemas.ApiResponse)
    async def show_history(message_id: str) -> JSONResponse:
        return unwrap(handlers.history_show(message_id))

    @app.get("/history/search", response_model=schemas.ApiResponse)
    async def search_history(recipient: str = Query(...)) -> JSONResponse:
        return unwrap(handlers.history_search(recipient))

    @app.get("/folders", response_model=schemas.ApiResponse)
    async def list_folders(account: str | None = None) -> JSONResponse:
        return unwrap(handlers.folders(account))

    @app.get("/messages/inbox", response_model=schemas.ApiResponse)
    async def inbox(account: str | None = None, folder: str = "INBOX", limit: int = 20, unread: bool = False) -> JSONResponse:
        return unwrap(handlers.inbox(account=account, folder=folder, limit=limit, unread=unread))

    @app.get("/messages/search", response_model=schemas.ApiResponse)
    async def search_messages(account: str | None = None, folder: str = "INBOX", limit: int = 20, unread: bool = False, from_address: str | None = None, subject: str | None = None) -> JSONResponse:
        return unwrap(handlers.search_messages(account=account, folder=folder, limit=limit, unread=unread, from_address=from_address, subject=subject))

    @app.get("/messages/{uid}", response_model=schemas.ApiResponse)
    async def read_message(uid: str, account: str | None = None, folder: str = "INBOX") -> JSONResponse:
        return unwrap(handlers.read_message(uid=uid, account=account, folder=folder))

    @app.get("/messages/{uid}/attachments", response_model=schemas.ApiResponse)
    async def message_attachments(uid: str, account: str | None = None, folder: str = "INBOX") -> JSONResponse:
        return unwrap(handlers.attachments(uid=uid, account=account, folder=folder))

    @app.post("/messages/{uid}/download", response_model=schemas.ApiResponse)
    async def download_message_attachments(uid: str, request: schemas.DownloadRequest) -> JSONResponse:
        return unwrap(handlers.download_attachments(uid, request.model_dump()))

    @app.post("/messages/{uid}/mark", response_model=schemas.ApiResponse)
    async def mark_message(uid: str, request: schemas.MarkRequest) -> JSONResponse:
        return unwrap(handlers.mark_message(uid, request.model_dump()))

    @app.post("/messages/{uid}/move", response_model=schemas.ApiResponse)
    async def move_message(uid: str, request: schemas.MoveRequest) -> JSONResponse:
        return unwrap(handlers.move_message(uid, request.model_dump()))

    @app.delete("/messages/{uid}", response_model=schemas.ApiResponse)
    async def delete_message(uid: str, request: schemas.DeleteRequest) -> JSONResponse:
        return unwrap(handlers.delete_message(uid, request.model_dump()))

    @app.post("/drafts", response_model=schemas.ApiResponse)
    async def create_draft(request: schemas.DraftCreateRequest) -> JSONResponse:
        return unwrap(handlers.draft_create(request.model_dump(by_alias=False)))

    @app.get("/drafts", response_model=schemas.ApiResponse)
    async def list_drafts() -> JSONResponse:
        return unwrap(handlers.draft_list())

    @app.get("/drafts/{draft_id}", response_model=schemas.ApiResponse)
    async def show_draft(draft_id: str) -> JSONResponse:
        return unwrap(handlers.draft_show(draft_id))

    @app.post("/drafts/{draft_id}/send", response_model=schemas.ApiResponse)
    async def send_draft(draft_id: str) -> JSONResponse:
        return unwrap(handlers.draft_send(draft_id))

    @app.delete("/drafts/{draft_id}", response_model=schemas.ApiResponse)
    async def delete_draft(draft_id: str) -> JSONResponse:
        return unwrap(handlers.draft_delete(draft_id))

    @app.get("/system/api/status", response_model=schemas.ApiResponse)
    async def api_runtime_status() -> JSONResponse:
        return unwrap(handlers.api_status())

    @app.get("/")
    async def root() -> JSONResponse:
        return JSONResponse({"ok": True, "docs": "/scalar", "openapi": "/openapi.json"})

    return app
