import json
from unittest.mock import patch

from starlette.testclient import TestClient

from beget_mail_mcp import server


def test_mcp_routes_each_request_to_its_authenticated_mailbox(monkeypatch):
    monkeypatch.setenv("BEGET_ACCOUNTS_JSON", json.dumps([
        {"email": f"{key}@example.com", "password": "test-only", "token": key * 32}
        for key in ("a", "b")
    ]))
    app = server.mcp.streamable_http_app()
    app.add_middleware(server.BearerTokenMiddleware, accounts=server.load_accounts())

    def list_emails(mail, limit):
        return server.EmailList(mailbox=mail.settings.email, emails=[])

    def send(mail, to, cc, subject, text):
        return server.SendResult(
            sent=True, from_address=mail.settings.email, to=to, cc=cc, subject=subject
        )

    with (
        TestClient(app, base_url="http://127.0.0.1:8000") as client,
        patch.object(server.MailClient, "list_emails", list_emails),
        patch.object(server.MailClient, "send", send),
    ):
        def call(key, name, arguments):
            return client.post("/mcp", headers={
                "Authorization": "Bearer " + key * 32,
                "Accept": "application/json, text/event-stream",
            }, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })

        assert call("x", "list_recent_emails", {}).status_code == 401
        for key in ("a", "b", "a"):
            result = call(key, "list_recent_emails", {"limit": 1}).json()["result"]
            assert result["structuredContent"]["mailbox"] == f"{key}@example.com"

        args = {"to": ["test@example.com"], "subject": "Test", "text": "Body"}
        preview = call("a", "prepare_email", args).json()["result"]["structuredContent"]
        assert preview["from_address"] == "a@example.com"
        args["confirmation_token"] = preview["confirmation_token"]
        assert call("b", "send_email", args).json()["result"]["isError"]
        sent = call("a", "send_email", args).json()["result"]["structuredContent"]
        assert sent["from_address"] == "a@example.com"
        assert call("a", "send_email", args).json()["result"]["isError"]
