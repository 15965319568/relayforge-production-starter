from __future__ import annotations

import sqlite3
import unittest

from _production_support import ProductionCase, invoke
from _support import BASE_TIME
from relayforge.errors import NotFound, ValidationError


class CredentialServiceTests(ProductionCase, unittest.TestCase):
    def test_token_is_returned_once_and_only_hash_is_persisted(self) -> None:
        issued = self.platform.credentials.issue(
            tenant_id="org-a", scopes=["audit:read"], now=BASE_TIME
        )
        self.assertTrue(issued.token.startswith(f"rfg_{issued.id}."))
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM api_credentials WHERE id = ?", (issued.id,)
            ).fetchone()
        self.assertNotIn(issued.token.encode(), bytes(row["token_hash"]))
        self.assertNotIn(issued.token, repr(dict(row)))

    def test_authenticate_updates_last_used_and_revoke_is_immediate(self) -> None:
        issued = self.platform.credentials.issue(
            tenant_id="org-a", scopes=["events:write"], now=BASE_TIME
        )
        principal = self.platform.credentials.authenticate(
            issued.token, now=BASE_TIME
        )
        self.assertEqual("org-a", principal.tenant_id)
        self.platform.credentials.revoke(issued.id, now=BASE_TIME)
        with self.assertRaises(ValidationError):
            self.platform.credentials.authenticate(issued.token, now=BASE_TIME)

    def test_operator_must_be_unscoped(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.credentials.issue(
                tenant_id="org-a", scopes=["operator"], now=BASE_TIME
            )

    def test_tenant_credential_cannot_authorize_unscoped_operation(self) -> None:
        issued = self.platform.credentials.issue(
            tenant_id="org-a", scopes=["audit:read"], now=BASE_TIME
        )
        principal = self.platform.credentials.authenticate(issued.token, now=BASE_TIME)
        with self.assertRaises(ValidationError):
            self.platform.credentials.authorize(principal, scope="audit:read")

    def test_malformed_identifier_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.credentials.authenticate("rfg_bad/id.secret", now=BASE_TIME)


class AuthMiddlewareTests(ProductionCase, unittest.TestCase):
    def test_liveness_and_readiness_do_not_require_credentials(self) -> None:
        app = self.authenticated_app()
        live, payload, _ = invoke(app, "GET", "/live")
        ready, ready_payload, _ = invoke(app, "GET", "/ready")
        self.assertEqual((200, "ok"), (live, payload["status"]))
        self.assertEqual((200, "ready"), (ready, ready_payload["status"]))

    def test_missing_and_invalid_tokens_are_401_without_token_echo(self) -> None:
        app = self.authenticated_app()
        missing, _, _ = invoke(app, "GET", "/v1/audit", query="tenant_id=org-a")
        invalid, payload, _ = invoke(
            app,
            "GET",
            "/v1/audit",
            query="tenant_id=org-a",
            token="rfg_unknown.super-secret-value",
        )
        self.assertEqual(401, missing)
        self.assertEqual(401, invalid)
        self.assertNotIn("super-secret-value", str(payload))

    def test_cross_tenant_audit_is_forbidden(self) -> None:
        credential = self.platform.credentials.issue(
            tenant_id="org-a", scopes=["audit:read"], now=BASE_TIME
        )
        status, _, _ = invoke(
            self.authenticated_app(),
            "GET",
            "/v1/audit",
            query="tenant_id=org-b",
            token=credential.token,
        )
        self.assertEqual(403, status)

    def test_tenant_cannot_request_all_tenant_audit(self) -> None:
        credential = self.platform.credentials.issue(
            tenant_id="org-a", scopes=["audit:read"], now=BASE_TIME
        )
        status, _, _ = invoke(
            self.authenticated_app(), "GET", "/v1/audit", token=credential.token
        )
        self.assertEqual(403, status)

    def test_invalid_json_is_400_not_403(self) -> None:
        credential = self.platform.credentials.issue(
            tenant_id="org-a", scopes=["subscriptions:write"], now=BASE_TIME
        )
        app = self.authenticated_app()
        captured = {}
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/subscriptions",
            "CONTENT_LENGTH": "1",
            "wsgi.input": __import__("io").BytesIO(b"{"),
            "HTTP_AUTHORIZATION": f"Bearer {credential.token}",
        }
        payload = b"".join(
            app(environ, lambda status, headers: captured.update(status=status))
        )
        self.assertTrue(captured["status"].startswith("400"))
        self.assertNotIn(credential.token.encode(), payload)

    def test_unknown_tenant_resource_is_404_for_operator(self) -> None:
        operator = self.platform.credentials.issue(
            tenant_id=None, scopes=["operator"], now=BASE_TIME
        )
        status, _, _ = invoke(
            self.authenticated_app(),
            "POST",
            "/v1/secrets/not-found/rotate",
            body={"secret_env": "NEXT"},
            token=operator.token,
        )
        self.assertEqual(404, status)

    def test_request_id_is_preserved_or_generated(self) -> None:
        app = self.authenticated_app()
        status, _, headers = invoke(
            app, "GET", "/live", headers={"X-Request-Id": "request-123"}
        )
        self.assertEqual(200, status)
        self.assertIn(("X-Request-Id", "request-123"), headers)
