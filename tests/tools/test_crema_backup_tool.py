"""Crema's backup tool: "백업해 줘" in the chat makes the keyless backup Settings used to make."""
import json

import tools.crema_backup_tool as cb
from toolsets import resolve_toolset


def test_backs_up_without_secrets_into_the_named_folder(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("hermes_cli.backup.run_backup", lambda args: seen.update(vars(args)) or True)
    out = json.loads(cb.crema_backup({"folder": str(tmp_path / "백업")}))
    assert out["complete"] is True
    assert out["path"].startswith(str(tmp_path / "백업")) and out["path"].endswith(".zip")
    assert (seen["output"], seen["no_secrets"], seen["keep"]) == (out["path"], True, 0)


def test_goes_to_documents_crema_backup_when_no_folder_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "documents_folder", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.backup.run_backup", lambda args: False)
    out = json.loads(cb.crema_backup({}))
    assert out["path"].startswith(str(tmp_path / "Crema 백업"))
    assert out["complete"] is False  # some files unreadable: the zip holds the rest


def test_a_backup_that_cannot_run_is_a_tool_error(tmp_path, monkeypatch):
    def busy(args):
        raise SystemExit(2)
    monkeypatch.setattr("hermes_cli.backup.run_backup", busy)
    assert "error" in json.loads(cb.crema_backup({"folder": str(tmp_path)}))


def test_crema_offers_it():
    assert "crema_backup" in resolve_toolset("hermes-api-server")
