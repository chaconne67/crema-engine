from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import crema_engine


def test_pc_collaboration_route_allows_only_lifecycle_actions_without_user_trust():
    app = FastAPI()
    crema_engine.install_turn_collaboration_route(app)
    client = TestClient(app)
    with patch("agent.crema_file_turns.collaborate", return_value={"ok": True}) as collaborate:
        for action in ("claim", "validate", "accepted", "report"):
            body = {"action": action, "session_id": "agent-client-a", "request_id": "r1",
                    "attempt_id": "a1", "message_id": "m1"}
            response = client.post("/api/crema/turns/collab", json=body)
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            assert collaborate.call_args == ((body,), {"trusted_user": False})
        for action in ("grant", "revoke"):
            response = client.post("/api/crema/turns/collab", json={"action": action})
            assert response.status_code == 403
        response = client.post("/api/crema/turns/collab", json={"action": "unknown"})
        assert response.status_code == 400
        response = client.post("/api/crema/turns/collab", json={"action": ["grant"]})
        assert response.status_code == 400
        assert collaborate.call_count == 4


def test_pc_user_collaboration_requires_private_native_consent_capability():
    app = FastAPI()
    consent = "c" * 48
    crema_engine.install_user_collaboration_route(app, consent)
    client = TestClient(app)
    body = {"action": "grant", "session_id": "agent-client-a", "peer_session_id": "agent-client-b",
            "project_id": "project-a", "message_id": "user-message"}
    with patch("agent.crema_file_turns.collaborate", return_value={"ok": True, "request_id": "r1"}) as collaborate:
        response = client.post("/api/crema/turns/user-collab", json=body,
                               headers={"X-Hermes-Session-Token": "generic-settings-token"})
        assert response.status_code == 403
        assert collaborate.call_count == 0

        response = client.post("/api/crema/turns/user-collab", json={**body, "action": ["grant"]},
                               headers={"X-Crema-Collaboration-Consent": consent})
        assert response.status_code == 400
        assert collaborate.call_count == 0

        response = client.post("/api/crema/turns/user-collab", json=body,
                               headers={"X-Crema-Collaboration-Consent": consent})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "request_id": "r1"}
        collaborate.assert_called_once_with(body, trusted_user=True)

    blank = FastAPI()
    crema_engine.install_user_collaboration_route(blank, "")
    response = TestClient(blank).post("/api/crema/turns/user-collab", json=body,
                                     headers={"X-Crema-Collaboration-Consent": consent})
    assert response.status_code == 403
