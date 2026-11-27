"""Run the unchanged decision with the active conversation's connection."""
import importlib.util
import json
import sys
from functools import partial
from pathlib import Path



def ask(runtime, app, model, settings, system, user, schema, seconds):
    from agent.auxiliary_client import (
        _build_call_kwargs, _create_with_progress_once, _provider_requires_stream, resolve_provider_client,
    )
    from agent.redact import redact_sensitive_text

    provider = settings["provider"]
    if app != "crema" or not runtime or provider in ("", "auto"):
        raise RuntimeError("현재 대화의 모델 연결을 확인하지 못했습니다.")
    try:
        client, name = resolve_provider_client(
            provider, model, explicit_base_url=runtime["base_url"] or None,
            explicit_api_key=runtime["api_key"] or None, api_mode=runtime["api_mode"] or None,
            use_bound_connection=True,
        )
        if client is None or not name:
            raise RuntimeError("unavailable chat connection")
        base_url = str(getattr(client, "base_url", "") or runtime["base_url"])
        kwargs = _build_call_kwargs(
            provider, name, reasoning_config=settings["reasoning"], base_url=base_url,
            timeout=max(seconds, 1),
            messages=[{"role": "system", "content": redact_sensitive_text(system, force=True)},
                      {"role": "user", "content": redact_sensitive_text(user, force=True)}],
            extra_body={"response_format": {"type": "json_schema", "json_schema": {
                "name": "review", "schema": schema, "strict": True}}},
        )
        reply = _create_with_progress_once(client, kwargs,
                                           force_stream=_provider_requires_stream(provider, base_url))
        return reply.choices[0].message.content
    except Exception:
        # The decision prints its exception to the chat. Provider errors can contain credentials.
        raise RuntimeError("현재 대화에 연결된 모델에서 판정 결과를 받지 못했습니다.") from None


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    scripts = Path(__file__).resolve().parent
    if not (scripts / "change-principles.py").is_file():
        # A product build supplies the policy beside this adapter. Source checkouts use kitsync's install.
        shared = Path.home() / ".local/share/controlroom/change-principles"
        revision = (shared / "kitsync-version").read_text(encoding="ascii").strip()
        if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("Invalid kitsync gate revision")
        scripts = shared / "kitsync" / revision / "scripts"
    sys.path.insert(0, str(scripts))
    import reviewer

    request = json.load(sys.stdin)
    reviewer.ask = partial(ask, request["runtime"])
    spec = importlib.util.spec_from_file_location("change_principles", scripts / "change-principles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    print(json.dumps(module.decide(request["event"])))


if __name__ == "__main__":
    main()
