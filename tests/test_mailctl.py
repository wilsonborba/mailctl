import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mailctl
from fastapi.testclient import TestClient
from mailctl_app.presentation.api.app import create_app


class MailCtlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tempdir.cleanup()

    def save_config(self) -> dict:
        config = {
            "default_account": "personal",
            "accounts": {
                "personal": {
                    "email": "user@gmail.com",
                    "display_name": "User Name",
                    "smtp_host": "smtp.gmail.com",
                    "smtp_port": 465,
                    "imap_host": "imap.gmail.com",
                    "imap_port": 993,
                },
                "work": {
                    "email": "work@example.com",
                    "display_name": "Work User",
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 465,
                    "imap_host": "imap.example.com",
                    "imap_port": 993,
                },
            },
        }
        mailctl.save_config(config)
        return config

    def test_config_and_multiple_accounts(self) -> None:
        config = self.save_config()
        loaded = mailctl.load_config(required=True)
        self.assertEqual(loaded["default_account"], "personal")
        self.assertEqual(sorted(loaded["accounts"]), ["personal", "work"])
        self.assertEqual(
            stat_mode(mailctl.config_path()),
            0o600,
        )

    def test_password_env_name_normalization(self) -> None:
        self.assertEqual(
            mailctl.password_env_name("my-personal.account"),
            "MAILCTL_PASSWORD_MY_PERSONAL_ACCOUNT",
        )

    def test_keyring_store_and_lookup(self) -> None:
        fake_backend = mock.Mock()
        fake_backend.__class__.__name__ = "SecretService"
        fake_keyring = mock.Mock()
        fake_keyring.get_keyring.return_value = fake_backend
        fake_keyring.get_password.return_value = "stored-password"
        with mock.patch.object(mailctl, "keyring", fake_keyring):
            mailctl.save_password("personal", "user@gmail.com", "stored-password")
            value = mailctl.get_stored_password("personal", "user@gmail.com")
        self.assertEqual(value, "stored-password")
        fake_keyring.set_password.assert_called_once()

    def test_account_add_stores_password_in_keyring(self) -> None:
        fake_backend = mock.Mock()
        fake_backend.__class__.__name__ = "SecretService"
        fake_keyring = mock.Mock()
        fake_keyring.get_keyring.return_value = fake_backend
        args = SimpleNamespace(
            json=True,
            alias="personal",
            email="user@gmail.com",
            name="User Name",
            provider="gmail",
            smtp_host=None,
            smtp_port=None,
            imap_host=None,
            imap_port=None,
            skip_password=False,
        )
        out = io.StringIO()
        with mock.patch.object(mailctl, "keyring", fake_keyring):
            with mock.patch("mailctl.getpass.getpass", return_value="app-password"):
                with redirect_stdout(out):
                    code = mailctl.command_account_add(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["credential_store"], "keyring")
        fake_keyring.set_password.assert_called_once()

    def test_compose_message_with_headers_and_attachment(self) -> None:
        config = self.save_config()
        attachment = self.home / "cv.pdf"
        attachment.write_bytes(b"hello")
        args = SimpleNamespace(
            account="personal",
            from_address=None,
            to=["hr@example.com"],
            cc=["manager@example.com"],
            bcc=None,
            reply_to=["reply@example.com"],
            subject="Application",
            body="Please find attached.",
            body_file=None,
            html=None,
            html_file=None,
            attach=[str(attachment)],
            allow_many=False,
        )
        build = mailctl.compose_message(args, config=config)
        self.assertEqual(build.message["Subject"], "Application")
        self.assertIn("Date", build.message)
        self.assertIn("Message-ID", build.message)
        self.assertEqual(len(build.attachments), 1)
        self.assertEqual(build.attachments[0].filename, "cv.pdf")

    def test_dry_run_json_output(self) -> None:
        self.save_config()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailctl.main(
                [
                    "send",
                    "--account",
                    "personal",
                    "--to",
                    "hr@example.com",
                    "--subject",
                    "Application",
                    "--body",
                    "Hello",
                    "--dry-run",
                    "--json",
                ]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "send")
        self.assertEqual(payload["result"]["recipient_count"], 1)

    def test_history_and_duplicate_search(self) -> None:
        mailctl.append_history(
            {
                "timestamp": "2026-08-02T00:00:00+00:00",
                "account": "personal",
                "to": ["hr@company.com"],
                "subject": "Application",
                "attachments": ["CV.pdf"],
                "message_id": "<abc@example.com>",
                "status": "sent",
            }
        )
        result = io.StringIO()
        args = SimpleNamespace(json=True, recipient="company.com")
        with redirect_stdout(result):
            code = mailctl.command_history_search(args)
        payload = json.loads(result.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["result"]), 1)

    def test_sanitize_filename(self) -> None:
        self.assertEqual(mailctl.sanitize_filename("../../evil.txt"), "evil.txt")

    def test_download_attachments(self) -> None:
        message = mailctl.EmailMessage()
        message.set_content("body")
        message.add_attachment(
            b"attachment-data",
            maintype="application",
            subtype="octet-stream",
            filename="../../unsafe.txt",
        )
        target_dir = self.home / "Downloads" / "mailctl"
        result = mailctl.download_attachments(message, target_dir, overwrite=True, assume_yes=True)
        self.assertEqual(result[0]["filename"], "unsafe.txt")
        self.assertTrue((target_dir / "unsafe.txt").exists())

    def test_reply_context_and_subject_prefix(self) -> None:
        config = self.save_config()
        source = mailctl.EmailMessage()
        source["From"] = "Recruiter <recruiter@example.com>"
        source["To"] = "user@gmail.com"
        source["Subject"] = "Interview"
        source["Message-ID"] = "<thread@example.com>"
        source.set_content("Hello there")
        reply_context = mailctl.build_reply_context(source, "user@gmail.com", False)
        args = SimpleNamespace(
            account="personal",
            from_address=None,
            to=None,
            cc=None,
            bcc=None,
            reply_to=None,
            subject="",
            body="Thanks",
            body_file=None,
            html=None,
            html_file=None,
            attach=None,
            allow_many=False,
            reply_all=False,
            quote=False,
        )
        build = mailctl.compose_message(args, config=config, reply_context=reply_context)
        self.assertEqual(build.message["Subject"], "Re: Interview")
        self.assertEqual(build.message["In-Reply-To"], "<thread@example.com>")

    def test_draft_lifecycle(self) -> None:
        self.save_config()
        create_args = SimpleNamespace(
            json=True,
            account="personal",
            from_address=None,
            to=["hr@example.com"],
            cc=None,
            bcc=None,
            reply_to=None,
            subject="Draft",
            body="Hello",
            body_file=None,
            html=None,
            html_file=None,
            attach=None,
            allow_many=False,
        )
        out = io.StringIO()
        with redirect_stdout(out):
            mailctl.command_draft_create(create_args)
        payload = json.loads(out.getvalue())
        draft_id = payload["result"]["draft_id"]
        draft = mailctl.load_draft(draft_id)
        self.assertEqual(draft["subject"], "Draft")

    def test_account_connectivity_test_uses_clients(self) -> None:
        self.save_config()
        smtp_client = mock.MagicMock()
        smtp_context = mock.MagicMock()
        smtp_context.__enter__.return_value = smtp_client
        smtp_context.__exit__.return_value = False
        imap_client = mock.MagicMock()
        with mock.patch("mailctl.get_stored_password", return_value="app-password"):
            with mock.patch("mailctl.smtplib.SMTP_SSL", return_value=smtp_context):
                with mock.patch("mailctl.imaplib.IMAP4_SSL", return_value=imap_client):
                    result = mailctl.account_connectivity_check(self.save_config()["accounts"]["personal"], "personal")
        self.assertEqual(result, {"smtp": "ok", "imap": "ok"})
        smtp_client.login.assert_called_once()
        imap_client.login.assert_called_once()

    def test_api_health_and_scalar(self) -> None:
        client = TestClient(create_app())
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "mailctl-api")
        docs = client.get("/scalar")
        self.assertEqual(docs.status_code, 200)
        self.assertIn("Scalar.createApiReference", docs.text)

    def test_api_account_and_send_dry_run(self) -> None:
        self.save_config()
        client = TestClient(create_app())
        accounts = client.get("/accounts")
        self.assertEqual(accounts.status_code, 200)
        self.assertTrue(accounts.json()["ok"])
        send = client.post(
            "/messages/send",
            json={
                "account": "personal",
                "to": ["hr@example.com"],
                "subject": "Application",
                "body": "Hello",
                "dry_run": True,
                "yes": True,
            },
        )
        self.assertEqual(send.status_code, 200)
        payload = send.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["recipient_count"], 1)

    def test_runtime_service_falls_back_when_port_busy(self) -> None:
        from mailctl_app.domain.services import runtime_service

        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        busy_port = busy.getsockname()[1]
        try:
            selected_port, fallback_used = runtime_service.find_available_port("127.0.0.1", busy_port)
        finally:
            busy.close()
        self.assertTrue(fallback_used)
        self.assertNotEqual(selected_port, busy_port)

    def test_install_parser_accepts_profiles_and_api_commands(self) -> None:
        parser = mailctl.build_parser()
        install_args = parser.parse_args(["install", "--profile", "cli+api", "--port", "9090"])
        self.assertEqual(install_args.profile, "cli+api")
        self.assertEqual(install_args.port, 9090)
        api_args = parser.parse_args(["api", "status", "--json"])
        self.assertEqual(api_args.command, "api")
        self.assertEqual(api_args.api_command, "status")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
