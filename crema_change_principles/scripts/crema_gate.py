"""Run the unchanged 0.1.1 decision with the engine's scoped subscription transport."""
import importlib.util
import json
import sys
from pathlib import Path

import reviewer


def ask(app, model, settings, system, user, schema, seconds):
    if app != "crema" or settings["provider"] != "openai-codex":
        raise RuntimeError("The change review requires the current chat's subscription authentication.")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent.auxiliary_client import call_llm
    from agent.redact import redact_sensitive_text

    reply = call_llm(
        provider=settings["provider"], model=model, reasoning_config=settings["reasoning"],
        timeout=max(seconds, 1),
        messages=[{"role": "system", "content": redact_sensitive_text(system, force=True)},
                  {"role": "user", "content": redact_sensitive_text(user, force=True)}],
        extra_body={"response_format": {"type": "json_schema", "json_schema": {
            "name": "review", "schema": schema, "strict": True}}},
    )
    return reply.choices[0].message.content


def main():
    reviewer.ask = ask
    spec = importlib.util.spec_from_file_location("change_principles", Path(__file__).with_name("change-principles.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    event = json.load(sys.stdin)
    print(json.dumps(module.decide(event)))


if __name__ == "__main__":
    main()
