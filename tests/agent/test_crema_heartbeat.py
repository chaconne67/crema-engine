import json

from agent import crema_file_turns
from crema_heartbeat import active, command, reference
from cron import jobs, scheduler


def test_desktop_heartbeat_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("SERVICE_DIR", raising=False)
    build_input = tmp_path / "engine-input.json"
    build_input.write_text(json.dumps({"themeIds": ["test"], "placement": "embedded",
                                      "engineInputs": {"test": {"schedules": {"heartbeat": True}}}}))
    monkeypatch.setenv("CREMA_ENGINE_INPUT", str(build_input))
    chat, session = "mine", "agent-client-mine"
    crema_file_turns.set_chats([{"id": chat, "session": session, "title": "원래 대화"}])

    def run(arg, *, chat_id=chat):
        return command(tmp_path, server=False, chat=chat_id, session=session, arg=arg)

    assert run("") == {"state": "off"}
    assert jobs.list_jobs() == []
    assert "error" in run("every 5s 상태를 알려 줘")
    assert "error" in run("every 1m 상태를 알려 줘", chat_id="foreign")
    assert run("every 1m 상태를 알려 줘")["state"] == "active"
    [job] = jobs.list_jobs()
    old = reference(job)
    assert active(old, session)
    assert not active(old, "agent-client-other")
    assert run("pause")["state"] == "paused"
    assert not active(old, session)
    assert run("resume")["state"] == "active"
    assert not active(old, session)
    [resumed] = jobs.list_jobs()
    assert active(reference(resumed), session)
    assert run("off") == {"state": "off"}
    assert not active(reference(resumed), session)
    assert not scheduler.run_job(resumed)[0]
    assert jobs.list_jobs(include_disabled=True) == []
