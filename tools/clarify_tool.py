"""Clarify tool: structured multiple-choice / open-ended questions to the user.
Schema, validation and a thin dispatcher; the UI lives in a platform-provided
callback (cli.py, gateway/run.py, tui_gateway)."""

import json
from typing import Callable, Dict, List, Optional

MAX_CHOICES = 4  # the UI always appends an "Other (type your answer)" row
MAX_QUESTIONS = 5  # independent questions per call
# Per-choice cap. Surfaces wrap long choice text (newlines kept), so anything longer is rejected here, at the
# source, instead of a surface silently dropping a choice that ``choices_offered`` still reports as shown.
MAX_CHOICE_CHARS = 8000
# Applied to the first choice here (not per-surface) so every adapter renders it identically.
RECOMMENDED_LABEL = "(Recommended)"
_UNAVAILABLE = "Clarify tool is not available in this execution context."
_SHAPE = "Pass questions=[{question, choices?, multi_select?}]; a single question is a one-entry array."


def _flatten_choice(c) -> str:
    """Coerce one choice to display text. LLMs sometimes emit dict-shaped choices and ``str(c)``
    would leak the repr onto every surface and back as the answer; unwrap order ``label`` >
    ``description`` > ``text`` > ``title`` (``name``/``value`` excluded: raw component enums,
    not labels). No match -> "" and dropped: no choice beats a garbage label."""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        return next((v.strip() for k in ("label", "description", "text", "title")
                     if isinstance(v := c.get(k), str) and v.strip()), "")
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return "" if c is None else str(c).strip()


def _choice_note(c) -> str:
    """A ``{label, description}`` choice's description: what picking it does, shown under its label. Empty
    when the choice is bare text or the description already stands in as the label."""
    note = c.get("description") if isinstance(c, dict) else None
    note = note.strip() if isinstance(note, str) else ""
    return note if note and note != _flatten_choice(c) else ""


def mark_recommended(choices: list[str]) -> list[str]:
    """Suffix the first choice (schema says best-first) with RECOMMENDED_LABEL; idempotent,
    and a lone choice is left untouched (nothing to prefer it over)."""
    first = str(choices[0]).strip() if choices else ""
    if len(choices) < 2 or first != strip_recommended(first):
        return choices
    return [f"{first} {RECOMMENDED_LABEL}"] + list(choices[1:])


def strip_recommended(text: str) -> str:
    """Remove the recommendation label so presentation never leaks into ``user_response``."""
    stripped = str(text).strip()
    if stripped.casefold().endswith(RECOMMENDED_LABEL.casefold()):
        return stripped[: -len(RECOMMENDED_LABEL)].strip()
    return stripped


def _clean_answer(raw, multi: bool):
    """Strip presentation (the label, multi-select JSON) from a locked answer: a multi-select
    answer is a list, a JSON array string, or one typed ("Other") answer."""
    if not multi:
        return strip_recommended(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        raw = parsed if isinstance(parsed, list) else [raw]
    return [strip_recommended(item) for item in raw if str(item).strip()]


def _normalize_questions(questions) -> tuple:
    """Validate ``questions`` -> ``(normalized, error)``. Entries carry ``qid`` (stable wire id
    ``q<index>`` surfaces key answers by), ``question``, decorated ``choices``, bare
    ``choices_offered`` and ``multi_select``."""
    if not isinstance(questions, list) or not questions:
        return None, f"questions must be a non-empty array. {_SHAPE}"
    if len(questions) > MAX_QUESTIONS:
        return None, f"questions supports at most {MAX_QUESTIONS} items."
    normalized = []
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            return None, f"questions[{index}] must be an object. {_SHAPE}"
        text = str(item.get("question") or "").strip()
        if not text:
            return None, f"questions[{index}].question must be non-empty text."
        choices = item.get("choices")
        notes = None
        if choices is not None:
            if not isinstance(choices, list) or not all(isinstance(c, (str, dict)) for c in choices):
                return None, f"questions[{index}].choices must be a list of labels or choice objects."
            pairs = [(_flatten_choice(choice), _choice_note(choice)) for choice in choices]
            for pos, (choice, _note) in enumerate(pairs):
                if len(choice.strip()) > MAX_CHOICE_CHARS:
                    return None, (f"questions[{index}].choices[{pos}] is {len(choice.strip())} characters; the limit is "
                                  f"{MAX_CHOICE_CHARS}. Move detail into the question text and resend.")
            cleaned = [(label, note) for label, note in pairs if label][:MAX_CHOICES]
            if choices and not cleaned:
                # A card with no options strands the turn: say so instead of silently asking open-ended.
                return None, (f"questions[{index}].choices has {len(choices)} entries but all are blank. "
                              "Send real labels, or omit choices to ask open-ended.")
            choices = [label for label, _ in cleaned] or None
            notes = [note for _, note in cleaned] if any(note for _, note in cleaned) else None
        normalized.append({
            "qid": f"q{index}", "question": text, "context": str(item.get("context") or "").strip() or None,
            "descriptions": notes,
            "choices": mark_recommended(list(choices)) if choices else None,
            "choices_offered": list(choices) if choices else None,
            "multi_select": bool(item.get("multi_select")) and bool(choices)})
    return normalized, None


def _response_status(qid: str, answers: dict, multi: bool) -> tuple:
    raw = answers.get(qid)
    cleaned = _clean_answer(raw, multi) if raw not in (None, "") else None
    if cleaned:
        return "answered", cleaned
    return ("skipped" if qid in answers else "unanswered"), None


def _result(normalized: list[dict], reply: dict, shown: list[dict] | None = None) -> str:
    """Result JSON from a callback reply ``{"answers": {qid: raw | None}, "outcome", "notice"?}``:
    every response carries ``status`` and ``user_response`` (null unless answered); ``outcome``
    says how the wait ended and ``notice`` (surface-supplied) says why. A picked display-copy
    choice (from ``shown``) maps back to the raw choice by position."""
    answers = reply.get("answers") or {}
    written_text = reply.get("text")
    responses = []
    for entry, seen in zip(normalized, shown or normalized):
        status, value = _response_status(entry["qid"], answers, entry["multi_select"])
        back = dict(zip(seen["choices_offered"] or [], entry["choices_offered"] or []))
        if back and value:
            value = [back.get(v, v) for v in value] if isinstance(value, list) else back.get(value, value)
        responses.append({"question": entry["question"], "choices_offered": entry["choices_offered"],
                          "status": status,
                          "user_response": "" if written_text and value is None else value})
    result: dict[str, object] = {"responses": responses, "outcome": reply["outcome"]}
    if written_text:
        result["user_wrote_instead_of_picking"] = str(written_text)
    if reply["outcome"] == "timed_out":
        result["timed_out"] = True
    if reply.get("notice"):
        result["notice"] = str(reply["notice"])
    return json.dumps(result, ensure_ascii=False)


def clarify_tool(questions=None, callback: Optional[Callable] = None, *,
                 question: str = "", choices=None, multi_select: bool = False) -> str:
    """Ask 1-5 questions in one call. ``callback(questions) -> {"answers", "outcome", "notice"?}``
    is platform injected (cli.py / gateway / tui_gateway) and receives the normalized list."""
    title = str(question or "").strip()
    single = not questions and bool(title)
    if single:
        questions = [{"question": title, "choices": choices, "multi_select": multi_select}]
    normalized, error = _normalize_questions(questions)
    if error:
        return tool_error(error)
    if callback is None:
        return tool_error(_UNAVAILABLE)
    from agent.redact import redact_approval_display
    from tools.human_input_hooks import human_input_request

    def prose(texts):
        if not texts:
            return texts
        out: list[str] = []
        for text in (redact_approval_display(t, prose=True) for t in texts):
            label, n = text, 1
            while label in out:  # choices that redact alike (two `***` secrets) stay distinct, so a pick maps back to its own raw
                n += 1
                label = f"{text} ({n})"
            out.append(label)
        return out
    # Every surface renders the prose display copy of the question and choices; the result echoes the raw text.
    shown = []
    for q in normalized:
        offered = prose(q["choices_offered"])
        shown.append({**q, "question": prose([q["question"]])[0], "choices_offered": offered,
                      "context": redact_approval_display(q["context"], prose=True) if q["context"] else None,
                      "descriptions": [redact_approval_display(note, prose=True) for note in q["descriptions"]]
                      if q["descriptions"] else None,
                      "choices": mark_recommended(list(offered)) if offered else None})
    if title and not single:
        shown[0]["title"] = redact_approval_display(title, prose=True)
    # Observers see the questions only; the answers stay in the tool result.
    with human_input_request("clarify", prompt="\n".join(q["question"] for q in shown)) as human:
        try:
            reply = callback(shown)
            human.outcome = str(reply.get("outcome") or "")
            if single:
                row = json.loads(_result(normalized, reply, shown))["responses"][0]
                row.pop("status")
                row["user_response"] = row["user_response"] or reply.get("text") or ""
                return json.dumps(row, ensure_ascii=False)
            return _result(normalized, reply, shown)
        except Exception as exc:
            human.outcome = "error"
            return tool_error(f"Failed to get user input: {exc}")


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user one or more questions when you need a decision or feedback. Give each question a context "
        "that explains why the user needs to decide, and give each choice a description of its consequences. "
        "Pass every question "
        f"in `questions` (1-{MAX_QUESTIONS} entries) — a single question is a "
        "one-entry array, and several INDEPENDENT questions belong in ONE "
        "call (one form beats a chain of clarify calls; if one answer would "
        "change another question, ask separately). Per question: "
        f"single-select (up to {MAX_CHOICES} choices — put your recommended "
        "option FIRST, the UI marks it '(Recommended)' and auto-appends an "
        "'Other' free-text row), multi-select (multi_select=true), or "
        "open-ended (omit choices). Options go ONLY in `choices`, never "
        "enumerated inside the question text (choices render as pickable "
        "rows; options written into the question are dead prose the user "
        "can't click). Result: {responses: [...], outcome} in question order. "
        "Each response has status answered, skipped or unanswered "
        "(user_response is null unless answered); outcome is submitted, "
        "cancelled, timed_out or undelivered, with a notice saying why "
        "when the wait ended without a submit. Prefer deciding "
        "low-stakes questions yourself; don't use this for dangerous-command "
        "confirmation (the terminal tool handles that)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": (
                    "The question(s). Each: question text (options excluded), "
                    "optional choices (recommended first; omit for free-text), "
                    "optional multi_select. Responses come back in question "
                    "order with the question text echoed."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "context": {
                            "type": "string",
                            "description": (
                                "The background, in the user's language and plain words: what this "
                                "is about, where it stands now, why it needs their decision, and "
                                "what follows from the answer. Call things by what they do or "
                                "what the user knows them as; an ID, hash, file, package or "
                                "command name the user would not know is explained or left out."
                            ),
                        },
                        "choices": {
                            "type": "array",
                            "maxItems": MAX_CHOICES,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "A few words naming the option.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": (
                                            "What happens if the user picks it, and what it "
                                            "costs or risks next to the other options."
                                        ),
                                    },
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multi_select": {"type": "boolean"},
                    },
                    "required": ["question"],
                },
            },
        },
        "required": ["questions"],
    },
}

# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        args.get("questions"), callback=kw.get("callback"), question=args.get("question", ""),
        choices=args.get("choices"), multi_select=args.get("multi_select", False)),
    check_fn=check_clarify_requirements,
    emoji="❓",
    self_checked_args=("questions",),
)
