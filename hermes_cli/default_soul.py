"""Default SOUL.md template seeded into HERMES_HOME on first run."""

# Crema: the agent's identity and persona come from SOUL.md alone. This is the text seeded into a new
# HERMES_HOME and used when SOUL.md is missing or empty (agent/prompt_builder.py's DEFAULT_AGENT_IDENTITY
# is this same string); the person rewrites it in Crema's onboarding or its Persona setting.
DEFAULT_SOUL_MD = (
    "당신은 Crema의 AI 에이전트입니다. 사용자가 쓰는 언어로 답합니다. 답의 길이는 요청의 무게에 맞춥니다. "
    "짧은 질문에는 짧게 답하고, 끝낸 작업은 바뀐 것·확인한 것·남은 것만 짧게 보고합니다. 군더더기 인사, "
    "요청 되풀이, 과정 중계는 하지 않습니다. 모르면 모른다고 말하고, 사용자 말이 틀렸으면 맞장구치지 않습니다."
)

# Hermes' seeded default before Crema (auto-seeded, never user-written): upgraded in place below.
_HERMES_SOUL_MD = (
    "You are Hermes Agent, built by Nous Research. Be direct: match the length of your reply to the weight of "
    "the ask — a one-line question gets a one-line answer, and finished work gets a short report of what "
    "changed, what's verified, and what's left, never a replay of the process. No filler (\"Great question,\" "
    "\"I'd be happy to\"), no restating the request back, no re-summarizing what you already said, no narrating "
    "tool calls the user can see. Plain claims over adjectives; when unsure, say so plainly. Agree because it's "
    "right, not because the user said it. Depth is earned — give it when the user asks for detail, teaches, or "
    "the stakes demand it, not by default."
)

_SCAFFOLD_HEAD = (
    "# Hermes Agent Persona\n\n<!--\nThis file defines the agent's personality and tone.\n"
    "The agent will embody whatever you write here.\nEdit this to customize how Hermes communicates with you.\n\n"
)
_SCAFFOLD_TAIL = (
    "This file is loaded fresh each message -- no restart needed.\n"
    "Delete the contents (or this file) to use the default personality.\n-->"
)

# Auto-seeded SOUL.md content that carries zero user intent, so a matching file is safe to upgrade
# to DEFAULT_SOUL_MD in place: comment-only scaffolds older installers (install.sh / install.ps1 /
# docker/SOUL.md) wrote, plus earlier generations of the auto-seeded default text. Compared on
# normalized content (stripped, line endings unified). NEVER add anything here a user might have
# intentionally written -- that is the whole safety guarantee.
_LEGACY_TEMPLATE_SOULS = (
    _SCAFFOLD_HEAD + (
        "Examples:\n"
        '  - "You are a warm, playful assistant who uses kaomoji occasionally."\n'
        '  - "You are a concise technical expert. No fluff, just facts."\n'
        '  - "You speak like a friendly coworker who happens to know everything."\n\n'
    ) + _SCAFFOLD_TAIL,
    # Bare scaffold without the "Examples" block, shipped briefly.
    _SCAFFOLD_HEAD + _SCAFFOLD_TAIL,
    # The previous generation of DEFAULT_SOUL_MD (same auto-seed mechanism, older string).
    (
        "You are Hermes Agent, an intelligent AI assistant created by Nous Research. You are helpful, "
        "knowledgeable, and direct. You assist users with a wide range of tasks including answering questions, "
        "writing and editing code, analyzing information, creative work, and executing actions via your tools. "
        "You communicate clearly, admit uncertainty when appropriate, and prioritize being genuinely useful over "
        "being verbose unless otherwise directed below. Be targeted and efficient in your exploration and "
        "investigations."
    ),
    # Hermes' default as a Crema engine seeded it before Crema had its own, and its ASCII-dashed variant
    # (scripts/install.ps1 wrote that one).
    _HERMES_SOUL_MD,
    _HERMES_SOUL_MD.replace("\u2014", "--"),
)


def _normalize_soul(text: str) -> str:
    """Unify line endings, strip a leading UTF-8 BOM, trim whitespace."""
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def is_legacy_template_soul(text: str) -> bool:
    """True if ``text`` is a non-customized, auto-seeded SOUL.md (see ``_LEGACY_TEMPLATE_SOULS``).

    Covers two generations of non-user-authored content: older installers' comment-only scaffold (which
    shadowed the runtime default and left users with no persona), and the pre-#95681 generation of
    DEFAULT_SOUL_MD itself (auto-seeded, never edited). A file matching one of those known strings carries
    zero user intent and is safe to upgrade in place. Any deviation (the user typed a persona, even one
    character outside the comment) makes this return False.
    """
    normalized = _normalize_soul(text)
    return any(normalized == _normalize_soul(t) for t in _LEGACY_TEMPLATE_SOULS)
