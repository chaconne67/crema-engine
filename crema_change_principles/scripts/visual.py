"""Identify literal presentation changes without executing the proposed edit."""

import ast
from html.parser import HTMLParser
from pathlib import Path
import re


EXTENSIONS = {".css", ".html", ".htm"}
RASTER_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp"}
CSS_TOKEN = re.compile(r"""/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{};()\[\]"'\\]|/\*""")
COLOR = re.compile(
    r"\s*background-color\s*:\s*(#[\da-f]{3,8}|[a-z]+)\s*(?:!important\s*)?", re.I | re.ASCII
)
GROUP = re.compile(r"\s*@(media|supports|layer|container|scope)\b", re.I)
DISPLAY = frozenset("h1 h2 h3 h4 h5 h6 p span strong em b i small div label button li th td caption figcaption".split())
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
OPAQUE = frozenset("script style template textarea title pre code svg math select option noscript xmp plaintext iframe".split())
EXECUTABLE = frozenset({"script", "iframe", "embed", "object"})


def isolated_python(text):
    """Recognize standalone calculations and printing without external reads."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    allowed = (ast.Module, ast.Assign, ast.Expr, ast.Load, ast.Store, ast.Constant,
               ast.BinOp, ast.UnaryOp, ast.operator, ast.unaryop)
    names, used = {"print"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                return False
            if isinstance(node.ctx, ast.Store):
                if node.id == "print":
                    return False
                names.add(node.id)
            else:
                used.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id != "print" or node.keywords:
                return False
        elif not isinstance(node, allowed):
            return False
    return used <= names


def css_spans(text):
    spans, blocks, start, parens, brackets = [], [], 0, 0, 0
    for match in CSS_TOKEN.finditer(text):
        token = match[0]
        if token in ('"', "'", "\\", "/*"):
            return None
        if len(token) != 1:  # comment or string
            continue
        if token in ("(", ")"):
            parens += 1 if token == "(" else -1
        elif token in ("[", "]"):
            brackets += 1 if token == "[" else -1
        if min(parens, brackets) < 0:
            return None
        if parens or brackets or token not in ("{", ";", "}"):
            continue
        head = text[start:match.start()]
        if token == "{":
            # A brace inside declarations can belong to a custom property.
            # Leave such a file to ordinary review, including text after the brace.
            if blocks and blocks[-1] == "style":
                return None
            in_rules = not blocks or blocks[-1] == "rules"
            head = re.sub(r"/\*[\s\S]*?\*/", "", head).lstrip()
            blocks.append(
                "rules" if in_rules and GROUP.match(head) else
                "style" if in_rules and head and not head.startswith("@") else "opaque"
            )
        else:
            if blocks and blocks[-1] == "style":
                color = COLOR.fullmatch(head)
                if color and (not color[1].startswith("#") or len(color[1]) in (4, 5, 7, 9)):
                    spans.append((start + color.start(1), start + color.end(1)))
            if token == "}":
                if not blocks:
                    return None
                blocks.pop()
        start = match.end()
    return None if blocks or parens or brackets else spans


class DisplayText(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.stack, self.spans, self.valid = [], [], True
        self.lines = [0, *(m.end() for m in re.finditer("\n", text))]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        # Unchanged code can still branch on the changed text. A document that
        # loads code or embeds a browsing context is not a static display file.
        if (tag in EXECUTABLE or "-" in tag or any(name.startswith("on") or
                (value and value.lstrip().lower().startswith("javascript:")) for name, value in attrs)):
            self.valid = False
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
        else:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.valid = False

    def handle_data(self, data):
        if not (self.stack and self.stack[-1] in DISPLAY and not OPAQUE.intersection(self.stack)):
            return
        value = data.strip()
        if value and re.fullmatch(r"[^<>&{}\[\]$%@\r\n]+", value):
            row, col = self.getpos()
            start = self.lines[row - 1] + col + len(data) - len(data.lstrip())
            self.spans.append((start, start + len(value)))


def html_spans(text):
    # A fragment's caller may bind behavior to its text. Only recognize a
    # standalone, explicitly formed document here.
    if (not re.match(r"\s*<!doctype\s+html\s*>\s*<html(?:\s[^<>]*)?>", text, re.I)
            or not re.search(r"<body(?:\s[^<>]*)?>", text, re.I)
            or not re.search(r"</body>\s*</html>\s*\Z", text, re.I)
            or re.search(r"\{\{|\{%|<%|<\?|\$\{", text)):
        return None
    html = DisplayText(text)
    return html.spans if html.valid and not html.stack else None


def shape(text, spans):
    if spans is None:
        return None
    result, end = [], 0
    for left, right in spans:
        result.append(text[end:left])
        end = right
    return (*result, text[end:])


def only(path, before, after):
    ext = Path(path).suffix.lower()
    if ext not in EXTENSIONS or before == after or "\0" in before + after:
        return False
    spans = css_spans if ext == ".css" else html_spans
    old = shape(before, spans(before))
    return old is not None and old == shape(after, spans(after))


def proposed(event, path):
    """Compare the file with a supported structured edit using exact context."""
    args = event.get("tool_input")
    tool = (event.get("tool_name") or "").split(".")[-1]
    mode = args.get("mode", "replace") if isinstance(args, dict) else "patch"
    try:
        before = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if (tool == "Edit" or (tool == "patch" and mode == "replace")) and isinstance(args, dict):
        if args.get("patch"):
            return False
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return False
        count = before.count(old)
        if count != 1 and not (count and args.get("replace_all") is True):
            return False
        after = before.replace(old, new)
    elif tool in {"Write", "write_file"} and isinstance(args, dict) and isinstance(args.get("content"), str):
        after = args["content"]
    elif tool == "apply_patch" or (tool == "patch" and mode == "patch"):
        raw = args.get("patch") or args.get("command") if isinstance(args, dict) else args
        if not isinstance(raw, str):
            return False
        parts = raw.splitlines()
        if (len(parts) < 5 or parts[0] != "*** Begin Patch" or parts[-1] != "*** End Patch"
                or not parts[1].startswith("*** Update File: ") or parts[2] != "@@"
                or any(not line.startswith((" ", "+", "-")) for line in parts[3:-1])):
            return False
        old = "\n".join(line[1:] for line in parts[3:-1] if line[0] != "+")
        new = "\n".join(line[1:] for line in parts[3:-1] if line[0] != "-")
        if not old or before.count(old) != 1:
            return False
        start = before.index(old)
        end = start + len(old)
        if (start and before[start - 1] != "\n") or (end < len(before) and before[end] != "\n"):
            return False
        after = before[:start] + new + before[end:]
    else:
        return False
    return only(path, before, after)


def committed(paths, diff):
    """Compare whole file text from the existing Git diff reader."""
    sections = re.split(r"(?m)^diff --git ", diff)
    if sections[0] or len(sections) != len(paths) + 1:
        return False
    for path, section in zip(paths, sections[1:]):
        lines = section.splitlines(keepends=True)
        if (len(lines) < 6 or not re.fullmatch(r"index [a-f0-9]+\.\.[a-f0-9]+ 100(?:644|755)\n", lines[1])
                or not lines[2].startswith("--- ") or not lines[3].startswith("+++ ")
                or not re.fullmatch(r"@@ -1(?:,\d+)? \+1(?:,\d+)? @@.*\n", lines[4])):
            return False
        body = lines[5:]
        if any(line[0] not in " +-" for line in body):
            return False
        before = "".join(line[1:] for line in body if line[0] != "+")
        after = "".join(line[1:] for line in body if line[0] != "-")
        if not only(path, before, after):
            return False
    return True
