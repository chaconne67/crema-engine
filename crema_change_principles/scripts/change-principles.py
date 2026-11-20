"""Change principles hook: one hook for Claude Code, Codex and Crema, at the prompt and before edit and shell tools.

It asks only where lasting work is changed (owner 2026-11-15: apply it to named places, not to everything but some):
files git keeps in a work tree, and the owner's installed programs (`in_scope`). At two moments it holds the action
until the agent has answered fixed questions and a freshly called reviewer agent has passed the answers. Each moment
judges what it can see then (owner 2026-11-06: each stage works on its own):
1. Before an action that may change lasting work, until a plan has passed since the owner last spoke (new words may
   widen the work, owner 2026-11-10): an edit of a file there that is not a document, or a shell command that may
   write there. The first hold gives the principles with the plan questions; the reviewer checks the answers against
   the principles and the chat's records, and decides: not a change (this action runs), proceed, or revise.
2. Before a commit with files that are not documents: the agent answers the commit questions; the reviewer checks them
   against the principles, the records and what the commit takes, and decides: commit or revise. A passed commit ends
   the change.
The program finds these moments, checks every question has an answer, gives the reviewer what it needs, and does what
the reviewer decides: the held action runs only once it passes.
"""
from contextlib import closing
import functools
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile

import reviewer
import visual

TAG = "[변경 원칙]"
SHELLS = {"Bash", "PowerShell", "shell", "shell_command", "exec_command", "local_shell", "terminal"}
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "write_file", "patch"}
DOCS = {".md", ".markdown", ".txt", ".rst"}
TEMP = Path(tempfile.gettempdir())
# words that run the command after them, and shell keywords that a command follows (for f in ...; do cp ...)
WRAPPERS = {"time", "env", "sudo", "nohup", "nice", "command", "exec", "do", "then", "else", "if", "while", "until", "!", "{"}
# where git keeps a repository named apart from -C and the folder the command runs in
REPO_OPTIONS = {"--git-dir", "--work-tree"}
REPO_VARIABLES = {"GIT_DIR", "GIT_WORK_TREE"}
# heredoc and PowerShell here-string bodies: text given to a command, not its words (a lone quote in them breaks the lexer).
# The rest of the heredoc's first line is still the command's (<<'EOF' > out.txt)
BODIES = re.compile(r"<<-?\s*(['\"]?)(\w+)\1(?P<rest>[^\n]*)\n.*?\n\s*\2\b|@(['\"])\n.*?\n\4@", re.S)


def without_bodies(command):
    return BODIES.sub(lambda m: " " + (m["rest"] or ""), command)


# ---- tool input ----

def command_of(event):
    args = event.get("tool_input") or {}
    if isinstance(args, str):
        return args
    command = args.get("command") or args.get("cmd") or ""
    if isinstance(command, list):  # argv form, e.g. ["bash", "-lc", "git commit ..."]
        return command[-1] if len(command) > 2 and command[-2] in {"-c", "-lc", "-Command"} else shlex.join(command)
    return command


def edited(event):
    """[path] for an edit tool call."""
    args = event.get("tool_input") or {}
    raw = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
    marks = re.findall(r"\*\*\* (?:Add|Update|Delete) File: ([^\\\n\"]+)", raw)
    if marks:
        return [p.strip() for p in marks]
    path = isinstance(args, dict) and (args.get("file_path") or args.get("notebook_path") or args.get("path"))
    if not path:
        raise ValueError("수정 도구 입력에서 파일 경로를 찾지 못했습니다")
    return [path]


def is_code(path):
    return Path(path).suffix.lower() not in DOCS


def words_of(command):
    # Windows paths: a backslash that is not escaping a quote or $ is a path separator
    lexer = shlex.shlex(re.sub(r"\\(?![\"'$`\\])", "/", command), posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"  # a newline outside quotes ends a command, as in the shell
    return list(lexer)


def ssh_at(part):
    """Where the host is in an ssh call's words."""
    i = 1
    while i < len(part) and part[i].startswith("-"):
        i += 2 if part[i] in {"-p", "-i", "-o", "-l", "-F", "-J", "-E"} else 1
    return i


def ssh_parts(part):
    """(host, the remote command's words) of an ssh call's words, or None."""
    i = ssh_at(part)
    return (part[i], part[i + 1:]) if i + 1 < len(part) else None


def segments(words):
    part = []
    for w in words + [";"]:
        if w in {";", "&&", "||", "|", "(", ")"} or ("\n" in w and set(w) <= set(";&|()\n")):  # a newline ends a command too
            if part:
                yield part
            part = []
        else:
            part.append(w)


def local_path(path, cwd):
    """A path as the hook's own git reads it: Git Bash's /c/ as C:, relative to the folder the command ran in."""
    if os.name == "nt":
        path = re.sub(r"^/([a-zA-Z])(?=/)", lambda m: m[1].upper() + ":", path)
    if not Path(path).is_absolute() and not path.startswith("~"):
        path = Path(cwd, path).as_posix()
    return path


def commit_target(command, cwd):
    """For a git commit command return (ssh host or None, repo dir or None, all flag, index file or None); else None.
    The index is GIT_INDEX_FILE set for the commit: a commit made from its own index takes what is staged there.
    A repository named by --git-dir, --work-tree, GIT_DIR or GIT_WORK_TREE is not read as `git -C <dir>` reads it, so
    its dir is None: what the commit takes cannot be read, not read from another repository."""
    try:
        words = words_of(without_bodies(command))
    except ValueError:
        return None
    where, index, named = None, None, False
    for part in segments(words):
        if part[0] == "export":
            index = next((w.split("=", 1)[1] for w in part[1:] if w.startswith("GIT_INDEX_FILE=")), index)
            named = named or any(w.split("=", 1)[0] in REPO_VARIABLES for w in part[1:] if "=" in w)
            continue
        own, apart = index, named
        while len(part) > 1 and (part[0] in WRAPPERS or re.match(r"^\w+=", part[0])):  # time git commit, env X=1 git commit
            if part[0].startswith("GIT_INDEX_FILE="):
                own = part[0].split("=", 1)[1]
            apart = apart or part[0].split("=", 1)[0] in REPO_VARIABLES
            part = part[1:]
        exe = part[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if exe in {"ssh", "ssh.exe"}:
            remote = ssh_parts(part)
            if remote:
                inner = commit_target(" ".join(remote[1]), None)
                if inner:
                    return (remote[0],) + inner[1:]
            continue
        if exe in {"bash", "sh", "pwsh", "powershell", "powershell.exe"} and len(part) > 1:
            inner = commit_target(part[-1], where or cwd)
            if inner:
                return inner
            continue
        if exe in {"cd", "set-location"} and len(part) > 1:
            where = part[-1]
            continue
        if exe not in {"git", "git.exe"}:
            continue
        i, repo = 1, None
        while i < len(part) and part[i].startswith("-"):
            if part[i] == "-C" and i + 1 < len(part):
                repo = part[i + 1]
            apart = apart or part[i].split("=", 1)[0] in REPO_OPTIONS
            i += 2 if part[i] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
        if i < len(part) and part[i] == "commit":
            flags = [w for w in part[i + 1:] if w.startswith("-") and not w.startswith("--")]
            every = "--all" in part or any("a" in f[1:] for f in flags if not f.startswith("-m"))
            base = repo or where
            if cwd:  # on this machine; a path over ssh (no cwd) stays as written
                base = local_path(base, cwd) if base else cwd
                own = local_path(own, local_path(where, cwd) if where else cwd) if own else None
            return (None, None if apart else base, every, own)
    return None


def commit_paths(command):
    """The paths after `--` in a `git commit -- <paths>`: only these are committed, whatever else is staged."""
    try:
        words = words_of(without_bodies(command))
    except ValueError:
        return []
    for part in segments(words):
        exe = part[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if exe in {"ssh", "ssh.exe", "bash", "sh", "pwsh", "powershell", "powershell.exe"} and len(part) > 1:
            inner = commit_paths(part[-1])
            if inner:
                return inner
        elif exe in {"git", "git.exe"} and "commit" in part and "--" in part[part.index("commit"):]:
            rest = part[part.index("commit"):]
            return rest[rest.index("--") + 1:]
    return []


def visual_commit_command(command):
    """Only commands whose contents the existing staged/path diff can predict."""
    try:
        parts = list(segments(words_of(without_bodies(command))))
    except ValueError:
        return False
    if len(parts) == 2 and program(parts[0][0]) in {"cd", "set-location"} and len(parts[0]) == 2:
        parts = parts[1:]  # the existing target reader accounts for this directory
    if len(parts) != 1:
        return False
    part = parts[0]
    exe = program(part[0])
    if exe == "ssh":
        remote = ssh_parts(part)
        return bool(remote and visual_commit_command(" ".join(remote[1])))
    if exe != "git":
        return False
    args = iter(part[3:] if part[1:2] == ["-C"] else part[1:])
    if next(args, None) != "commit":
        return False
    for arg in args:
        if arg == "--":
            return True  # explicit names; Git compares these with the work tree
        if arg in {"-m", "--message", "-F", "--file"} and next(args, None) is not None:
            continue
        if arg not in {"-q", "--quiet", "-a", "--all"}:
            return False
    return True


def added_files(command):
    """Every word after `git add` in the command, so a folder, '.' or -A counts as something that is not a document."""
    try:
        words = words_of(without_bodies(command))
    except ValueError:
        return ["?"]
    named = []
    for part in segments(words):
        exe = part[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if exe in {"ssh", "ssh.exe", "bash", "sh", "pwsh", "powershell", "powershell.exe"} and len(part) > 1:
            named += added_files(part[-1])
        elif exe in {"git", "git.exe"} and "add" in part:
            named += [w for w in part[part.index("add") + 1:] if w != "--"]
    return named


def remote_path(path):
    """A path for the remote shell, quoted; a leading ~ becomes "$HOME", which the shell still expands inside the command."""
    if path == "~" or path.startswith("~/"):
        return '"$HOME"' + ("/" + shlex.quote(path[2:]) if path[2:] else "")
    return shlex.quote(path)


def git_text(host, repo, index, *args):
    """Output of a git command in the repo (over ssh when host is given) with the commit's index, or None when it cannot be read."""
    if not repo:
        return None
    env = None
    if host:
        remote = " ".join(["git", "-C", remote_path(repo), *map(shlex.quote, args)])
        if index:
            remote = f"env GIT_INDEX_FILE={remote_path(index)} {remote}"
        argv = ["ssh", "-o", "BatchMode=yes", host, remote]
    else:
        argv = ["git", "-C", os.path.expanduser(repo), *args]
        if index:
            env = dict(os.environ, GIT_INDEX_FILE=os.path.expanduser(index))
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=25, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


WHOLE_DIFF = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--unified=1000000")


def independent_view(host, repo, index, path):
    """Limit the exception to a static checkout without a program that reads it."""
    root = git_text(host, repo, index, "rev-parse", "--show-toplevel")
    if not root:
        return False
    root = root.rstrip("\r\n")
    if isinstance(path, Path):  # the edit hook has an absolute local path
        tree = Path(root).resolve()
        if not path.is_relative_to(tree):
            return False
        path = path.relative_to(tree).as_posix()
    current = git_text(host, root, index, "ls-files", "-z", "--cached", "--others")
    previous = git_text(host, root, index, "ls-tree", "-r", "--full-tree", "--name-only", "-z", "HEAD")
    if current is None or previous is None:
        return False
    files = {f for listing in (current, previous) for f in listing.split("\0") if f}
    if path not in files:
        return False
    others = files - {path, ".gitignore"}
    static_files = DOCS | visual.EXTENSIONS | visual.RASTER_IMAGES
    if any(Path(f).suffix.lower() not in static_files | {".py"} for f in others):
        return False
    for f in sorted(others):
        suffix = Path(f).suffix.lower()
        if suffix not in {".html", ".htm", ".py"}:
            continue
        original = git_text(host, root, index, "show", f"HEAD:{f}")
        if original is None:
            return False
        if suffix == ".py":
            if not visual.isolated_python(original):
                return False
            flags = ("--quiet",)  # the demonstrated independent script must be unchanged
        else:
            if visual.html_spans(original) is None:
                return False
            flags = WHOLE_DIFF  # a companion page may have its own display-only delta
        for views in (("HEAD",), ("--cached", "HEAD")):
            diff = git_text(host, root, index, "diff", *flags, *views, "--", f)
            if diff is None or (diff and not visual.committed([f], diff)):
                return False
    return True


def committed_files(host, repo, every, index, paths=()):
    """Files the commit will take, or None when they cannot be read. -z gives each name as written (no quotes, no octal escapes)."""
    text = git_text(host, repo, index, "diff", "HEAD" if every or paths else "--cached", "--name-only", "-z",
                    *(["--", *paths] if paths else []))
    return None if text is None else [name for name in text.split("\0") if name]


def committed_diff(host, repo, every, index, paths=(), *, whole=False):
    flags = WHOLE_DIFF if whole else ()
    diff = git_text(host, repo, index, "diff", *flags, "HEAD" if every or paths else "--cached",
                    *(["--", *paths] if paths else []))
    if diff is None:
        raise RuntimeError(f"커밋할 변경 내용을 git에서 읽지 못했습니다({repo})")
    return diff

# ---- state ----

def home():
    return Path(os.environ.get("CHANGE_PRINCIPLES_HOME") or Path.home() / ".change-principles")


def state_path(event):
    if not event.get("session_id"):
        raise ValueError("훅 입력에 session_id가 없습니다")
    identity = [event["session_id"], event.get("transcript_path"), os.environ.get("HERMES_HOME")]
    if event.get("agent_id"):
        identity.append(event["agent_id"])  # a Claude subagent can report the parent's session and transcript
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    return home() / f"{key}.sqlite3"


def open_state(event):
    path = state_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT)")
    con.execute("BEGIN IMMEDIATE")
    row = con.execute("SELECT value FROM state WHERE id=1").fetchone()
    return con, json.loads(row[0]) if row else {}


# ---- decisions ----

# a shell command that may write files; only these are given to the reviewer, and only until the plan has passed
WRITES = re.compile(r"\b(sed|perl)\s+-\w*i|\btee\b|\b(Set|Add)-Content\b|\bOut-File\b|\b(New|Remove|Move|Copy|Rename)-Item\b"
                    r"|\bgit\s+(apply|checkout|restore|reset|stash(?!\s+(list|show)\b)|"
                    r"merge|rebase|cherry-pick|revert|am|switch|mv|rm)\b"  # conservative fallback for opaque interpreter source
                    r"|(^|[\s;&|(])(un)?install\s"  # also npm/pip/apt install
                    r"|write_text|write_bytes|(?<!stdout)(?<!stderr)\.write\("
                    r"|open\([^)]*,\s*(mode\s*=\s*)?\\?['\"][rb]?[wax]")  # the mode, not a name that starts with a
REDIRECT = re.compile(r"(?<![-=])(?:\d|&)?>>?(?!=)")  # in a word outside quotes
QUIET = {"/dev/null", "$null", "nul"}  # output thrown away, not written
FILE_TOOLS = {"rm", "mv", "touch", "truncate", "cp", "ln", "patch"}
SHELL_RUNNERS = {"bash", "sh", "pwsh", "powershell", "cmd"}
RUN_FLAGS = {"-c", "-lc", "-Command", "-command", "/c", "/C"}


# a shell word as written, quotes kept (inside double quotes a backslash escapes), or a word that ends a command
QUOTED_WORD = r"""(?:[^\s;&|()'"\\]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+"""
SHELL_WORDS = re.compile(rf"[ \t\r]*({QUOTED_WORD}|&&|\|\||[;&|()\n])", re.S)


def quoted_words(command):
    """The words of a command with their quotes, so a quoted word can be told from a redirect; ValueError if unclosed."""
    words, at = [], 0
    while command[at:].strip(" \t\r"):
        found = SHELL_WORDS.match(command, at)
        if not found:
            raise ValueError("unclosed quote")
        words.append(found[1])
        at = found.end()
    return words


def unquote(word):
    """A word as the shell passes it on: quotes taken off ('a'"b"c is abc). A backslash outside quotes is kept (a
    Windows path)."""
    out = []
    for piece in re.findall(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"|[^'\"]+", word, re.S):
        if piece[0] == "'":
            out.append(piece[1:-1])
        elif piece[0] == '"':
            out.append(re.sub(r"\\([\\\"$`])", r"\1", piece[1:-1]))
        else:
            out.append(piece)
    return "".join(out)


def in_temp(path):
    """Recognize OS temp and the engine's scratch directory even when inherited TEMP points elsewhere."""
    scratch = Path(os.environ.get("HERMES_SCRATCH_DIR") or reviewer.hermes_db().parent / "cache" / "scratch")
    names = ["$TEMP", "${TEMP}", "$TMP", "${TMP}", "$TMPDIR", "${TMPDIR}", "$env:TEMP", "%TEMP%", "/tmp"]
    for root in (TEMP, scratch):
        temp = root.as_posix().rstrip("/")
        names.append(temp)
        if temp[1:3] == ":/":
            names.append("/" + temp[0].lower() + temp[2:])  # Git Bash spelling
    path = os.path.normcase(unquote(path)).replace("\\", "/")
    return any(path.startswith(os.path.normcase(name).replace("\\", "/") + "/") for name in names)


def installs():
    """The owner's programs installed on this PC, which the quick-install scripts change in place: lasting work with no git."""
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        return []
    local = Path(os.environ["LOCALAPPDATA"])
    return [local / "Crema", local / "Programs" / "Thock",
            Path(os.environ.get("CREMA_HOME") or local / "local.agentclient.prototype" / "engine")]


def quietly(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None


@functools.lru_cache(maxsize=None)
def in_scope(host, path):
    """Whether a file or folder is lasting work, the only place this gate asks: in a git work tree and not ignored by it,
    or in an installed program. `host` is the ssh host the path is on; "container" is inside a container, which cannot
    be looked into, so it is asked. What cannot be checked is asked too."""
    if host == "container":
        return True
    if host:
        script = (f"p={remote_path(path)}; d=$p; while [ ! -d \"$d\" ]; do d=$(dirname \"$d\"); done; cd \"$d\" || exit 2; "
                  "[ \"$(git rev-parse --is-inside-work-tree 2>/dev/null)\" = true ] || exit 1; "
                  "git check-ignore -q -- \"$p\" && exit 1; exit 0")
        done = quietly(["ssh", "-o", "BatchMode=yes", host, script])
        return done is None or done.returncode != 1
    target = Path(os.path.abspath(os.path.expanduser(path)))
    if any(target == root or root in target.parents for root in installs()):
        return True
    folder = target
    while not folder.is_dir() and folder.parent != folder:
        folder = folder.parent
    inside = quietly(["git", "-C", str(folder), "rev-parse", "--is-inside-work-tree"])
    if inside is None:
        return True
    if inside.stdout.strip() != "true":
        return False
    ignored = quietly(["git", "-C", str(folder), "check-ignore", "-q", "--", str(target)])
    return ignored is None or ignored.returncode != 0


def place_in_scope(host, cwd):
    """Whether a command run in folder `cwd` (None: not known) may write lasting work when what it writes is not shown."""
    if cwd is None:
        return True
    if in_temp(cwd.rstrip("/\\") + "/x"):
        return False
    return in_scope(host, cwd)


def operands(part, exe):
    """The files an in-place editor (sed -i, perl -i) or tee writes: its words that are not options or the script."""
    files, script, i = [], exe in {"sed", "perl"}, 1
    while i < len(part):
        word = unquote(part[i])
        if word.startswith("-"):
            if exe in {"sed", "perl"} and re.fullmatch(r"-\w*[ef]", word):  # -e SCRIPT, -pe SCRIPT, -f FILE
                script, i = False, i + 2
            else:
                i += 1
            continue
        if script:
            script = False
        else:
            files.append(part[i])
        i += 1
    return files


HEREDOC = re.compile(r"__heredoc(\d+)__")
INTERPRETERS = re.compile(r"(python[\d.]*|py|node|perl|ruby)")
PATH_LITERAL = re.compile(r"""['"]((?:[A-Za-z]:[\\/]|/|~[\\/])[^'"\n]*)['"]""")


def program(word):
    return re.sub(r"\.exe$", "", unquote(word).replace("\\", "/").rsplit("/", 1)[-1].lower())


def writes(command, cwd=None, host=None, bodies=None):
    """Whether a shell command may write lasting work (`in_scope`): a file it names there, or, when what it writes is not
    shown, the folder it runs in (`cd`, `ssh`'s home, the hook's folder). Words are read with their quotes: a quoted word
    is data given to the command (a search word), except the command an ssh call or a shell's -c runs, which is read
    the same way on that host. A heredoc's body is text given to a command, except a script given to a shell (read as a
    command) or to an interpreter such as python (looked at for file writes). Documents, output thrown away and files
    in the temp folder are no lasting work."""
    if bodies is None:  # each heredoc's body, read where it is used: in the folder and on the host it runs in
        bodies = []

        def keep(found):
            bodies.append(found[0].split("\n", 1)[1] if "\n" in found[0] else "")
            return f" __heredoc{len(bodies) - 1}__ " + (found["rest"] or "")
        command = BODIES.sub(keep, command)
    # D=$(mktemp -d): a folder in the temp folder, marked before the words are split at its parentheses
    command = re.sub(r"\b(\w+)=(\"?)(\$\(\s*mktemp\b[^)]*\)|`\s*mktemp\b[^`]*`)\2", r"\1=__mktemp__", command)
    try:
        words = quoted_words(command)
    except ValueError:
        return True  # the words cannot be read: asked
    where = {"cwd": cwd, "vars": {}, "env": {}}  # the folder the next command runs in; variables set to a known value; exported GIT_DIR, GIT_WORK_TREE

    def resolved(word):
        """The path a word names, or None when it cannot be known (an unset variable, a command's output)."""
        path = unquote(word)
        for name, value in where["vars"].items():
            path = re.sub(r"\$\{?" + name + r"\}?(?!\w)", lambda _: value, path)
        path = re.sub(r"^\$\{?HOME\}?(?=[/\\]|$)", "~", path)
        if not host:
            path = os.path.expandvars(re.sub(r"\$env:(\w+)", r"${\1}", path, flags=re.I))
        if re.search(r"[$`%]", path):
            return None
        if host:
            return path if path.startswith(("/", "~")) else (posixpath.join(where["cwd"], path) if where["cwd"] else None)
        if os.name == "nt":
            path = re.sub(r"^/([a-zA-Z])(?=/|$)", lambda m: m[1].upper() + ":", path)
        path = os.path.expanduser(path)
        if Path(path).is_absolute():
            return path
        return os.path.join(where["cwd"], path) if where["cwd"] else None

    def outside(word):
        """A path that is no lasting work: a document, in the temp folder, or known to be outside `in_scope`."""
        raw = unquote(word)
        if not is_code(raw):
            return True
        path = resolved(word)
        if in_temp(raw) or path and in_temp(path):
            return True
        return path is not None and not in_scope(host, path)

    def here():
        return place_in_scope(host, where["cwd"])

    def temporary(word):
        path = resolved(word)
        return in_temp(unquote(word)) or bool(path) and in_temp(path)

    def assign(name, value):
        raw = unquote(value)
        if raw == "__mktemp__":
            where["vars"][name] = "/tmp/mktemp" if host else (TEMP / "mktemp").as_posix()
            return
        known = resolved(value) if raw else ""
        if known is None:
            where["vars"].pop(name, None)
        else:
            where["vars"][name] = known

    for part in segments(words):
        scripts = [bodies[int(found[1])] for found in map(HEREDOC.fullmatch, part) if found]
        part = [w for w in part if not HEREDOC.fullmatch(w)]
        if not part:
            continue
        wrapped, env = False, dict(where["env"])
        while len(part) > 1 and (part[0] in WRAPPERS or re.match(r"^\w+=", part[0]) or wrapped and part[0].startswith("-")):
            if WRITES.search(part[0]) and here():
                return True  # execution configuration is not query data
            name, _, value = part[0].partition("=")
            if name in REPO_VARIABLES:  # GIT_DIR=... git commit
                env[name] = value
            wrapped, part = wrapped or part[0] in WRAPPERS, part[1:]  # sudo -n rm: the wrapper's options too
        exe = program(part[0])
        assigned = re.match(r"^(\w+)=(.*)$", part[0], re.S) if len(part) == 1 else None
        if assigned or exe == "export" or len(part) == 3 and re.fullmatch(r"\$\w+", part[0]) and part[1] == "=":
            if WRITES.search(" ".join(part)) and here():
                return True
            if assigned:  # D="$TEMP/x": $D/... is in the temp folder
                assign(assigned[1], assigned[2])
            elif exe == "export":
                for w in part[1:]:
                    if "=" in w:
                        assign(*w.split("=", 1))
                        if w.split("=", 1)[0] in REPO_VARIABLES:
                            where["env"][w.split("=", 1)[0]] = w.split("=", 1)[1]
            else:  # PowerShell: $d = "C:\..."
                assign(part[0][1:], part[2])
            continue
        if exe in {"cd", "set-location", "pushd", "sl", "chdir"}:  # cd ~/p && rm a.py: a.py is in ~/p
            raw = unquote(part[-1]) if len(part) > 1 else "~"
            where["cwd"] = resolved(part[-1] if len(part) > 1 else "~") or (raw if in_temp(raw.rstrip("/\\") + "/x") else None)
            continue
        if scripts:  # bash <<EOF, ssh host <<EOF, python - <<EOF, docker exec -i web python - <<EOF
            remote = ssh_parts(part) if exe == "ssh" else None
            at = ssh_at(part)
            on = ("container" if exe in {"docker", "podman"} and "exec" in part else
                  (part[at] if at < len(part) else None) if exe == "ssh" else host)
            run_in = "~" if exe == "ssh" else None if on == "container" else where["cwd"]
            runs = [program(w) for w in (remote[1] if remote else part)]
            shell = exe == "ssh" and not remote or any(r in SHELL_RUNNERS for r in runs)
            interpreter = any(INTERPRETERS.fullmatch(r) for r in runs)
            for script in scripts:
                if shell and writes(script, run_in, on, bodies):
                    return True
                if interpreter and WRITES.search(script) and (place_in_scope(on, run_in) or any(
                        is_code(p) and not in_temp(p) and in_scope(on, p if on else os.path.expanduser(p))
                        for p in PATH_LITERAL.findall(script)[:10])):
                    return True
        if exe in {"docker", "podman"} and part[1:2] == ["cp"]:  # docker cp code.py web:/app/: a copy into a container
            if re.match(r"^[\w.-]{2,}:", unquote(part[-1])):
                return True
            part, exe = part[1:], "cp"
        if exe == "xargs":  # ... | xargs rm -f: the command xargs runs
            i = 1
            while i < len(part) and part[i].startswith("-"):
                i += 2 if part[i] in {"-n", "-I", "-L", "-P", "-d", "-s", "-E", "-a"} else 1
            if i < len(part) and writes(" ".join(map(unquote, part[i:])), where["cwd"], host, bodies):
                return True
            continue
        if exe == "find":  # find -delete, find -exec rm {} \;
            root = next((w for w in part[1:] if not w.startswith("-")), ".")
            if "-delete" in part and not outside(root):
                return True
            for i, w in enumerate(part):
                if w in {"-exec", "-execdir", "-ok"}:
                    end = next((j for j in range(i + 1, len(part)) if unquote(part[j]) in {";", "\\;", "+"}), len(part))
                    if writes(" ".join(map(unquote, part[i + 1:end])), where["cwd"], host, bodies):
                        return True
            continue
        remote = ssh_parts(part) if exe == "ssh" else None
        if remote:  # ssh joins its words and the remote shell reads them in its home; a redirect after them is this shell's
            if WRITES.search(" ".join(part[:-len(remote[1])])) and here():
                return True  # preserve execution-option checks before reading the remote command
            cut = next((i for i, w in enumerate(remote[1]) if re.match(r"(\d|&)?>", w)), len(remote[1]))
            if writes(" ".join(map(unquote, remote[1][:cut])), "~", remote[0], bodies):
                return True
            part = ["ssh"] + remote[1][cut:]  # this shell's redirects, read below
        # a shell run by this one, also inside docker exec, sudo, wsl: bash -c '...', powershell -NoProfile -Command "..."
        runs = [i for i, w in enumerate(part) if i and part[i - 1] in RUN_FLAGS and any(
            program(r) in SHELL_RUNNERS for r in part[:i - 1])]
        if runs:
            boxed = any(program(w) in {"docker", "podman"} for w in part[:runs[0]]) and "exec" in part[:runs[0]]
            if WRITES.search(" ".join(part[:runs[0]])) and here() or writes(
                    " ".join(map(unquote, part[runs[0]:])), None if boxed else where["cwd"], "container" if boxed else host, bodies):
                return True
            continue
        write_source = part
        if exe == "git":
            i, repo, apart = 1, None, list(env.values())  # apart: the repository or work tree named apart from -C
            while i < len(part) and part[i].startswith("-"):
                option, given, value = unquote(part[i]).partition("=")
                if part[i] == "-C" and i + 1 < len(part):
                    repo = part[i + 1]
                elif option in REPO_OPTIONS:
                    apart.append(value if given else part[i + 1] if i + 1 < len(part) else "")
                i += 2 if part[i] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
            operation = unquote(part[i]) if i < len(part) else ""
            git_query = operation in {"status", "diff", "log", "show", "grep", "ls-remote", "ls-files", "rev-parse", "rev-list",
                                      "merge-base", "merge-tree", "cat-file", "show-ref", "for-each-ref", "describe", "fetch"}
            if operation == "stash" and part[i + 1:i + 2] in (["list"], ["show"]):
                git_query = True
            elif operation in {"apply", "checkout", "restore", "reset", "stash", "merge", "rebase", "cherry-pick", "revert", "am", "switch", "mv", "rm", "commit"}:
                # a repository named apart is asked unless it is in the temp folder (a git dir is no work tree to look into)
                if not all(map(temporary, apart)) or place_in_scope(host, resolved(repo) if repo else where["cwd"]):
                    return True  # the repository's own files
                continue
            options_end = part.index("--", i + 1) if "--" in part[i + 1:] else len(part)
            for k in range(i + 1, options_end):  # a file write, not a query
                word = unquote(part[k])
                if word.startswith("--output=") and operation in {"diff", "log", "show"} and not outside(word.split("=", 1)[1]):
                    return True
                if word == "--output" and operation in {"diff", "log", "show"} and (k + 1 >= len(part) or not outside(part[k + 1])):
                    return True
            if git_query and not any(("$(" in w or "`" in w) and not w.startswith("'") for w in part[i + 1:]):
                # Omit only static query data, never global options or unknown option values that can run programs.
                write_source = part[1:i]
                j = i + 1
                while j < len(part):
                    word = unquote(part[j])
                    if word == "--":
                        break  # all remaining operands are data; shell substitutions were checked above
                    if word.startswith("-"):
                        key = "-e" if operation == "grep" and word.startswith("-e") else word.split("=", 1)[0]
                        if key in {"--grep", "--format", "--pretty", "-e"}:
                            j += 2 if word == key else 1
                            continue
                        write_source.append(part[j])
                        if "=" not in word and j + 1 < len(part) and not unquote(part[j + 1]).startswith("-"):
                            j += 1
                            write_source.append(part[j])
                    j += 1
        for k, word in enumerate(part):
            found = None if word[:1] in "'\"" else REDIRECT.search(word)
            if found:
                target = unquote(word[found.end():] or (part[k + 1] if k + 1 < len(part) else ""))
                # no target: 2>&1 inside $( ), whose & is read as its own word
                if target and not (target.lower() in QUIET or target.startswith("&") or outside(target)):
                    return True
        if exe in FILE_TOOLS:
            paths = [w for w in part[1:] if not w.startswith("-")]
            written = paths[-1:] if exe in {"cp", "ln"} else paths  # cp only reads its sources
            if exe == "patch" or not written:
                if here():  # what it writes is not shown
                    return True
            elif not all(outside(p) for p in written):
                return True
        if WRITES.search(" ".join(write_source)):
            if exe in {"sed", "perl", "tee"}:
                files = operands(part, exe)
            elif re.fullmatch(r"(set|add)-content|out-file|(new|remove|move|copy|rename)-item", exe):  # PowerShell: the words that name a path
                files = [w for w in part[1:] if not unquote(w).startswith("-") and re.search(r"[/\\~$]|\.\w{1,8}$", unquote(w))]
            else:
                files = []
            if not all(outside(f) for f in files) if files else here():
                return True
    return False


CANNOT = ("잠시 뒤 같은 작업을 다시 하세요. 저장소 경로를 못 읽었으면 `cd ~/...`나 `git -C <경로>`로 다시 하세요. 계속 안 되면 "
          "막힌 작업과 이 오류를 변경 게이트(change-principles 플러그인)의 문제로 주인님께 보고하세요.")


def principles():
    return "\n\n--- 원칙 원문 ---\n" + reviewer.PRINCIPLES.read_text(encoding="utf-8")


def answers_file(event, stage):
    """This session's answers file for a gate, next to its state file."""
    return state_path(event).with_suffix(f".{stage}.json")


def questions_text(path, stage):
    asked = "\n".join(f"- {name}: {q}" for name, q in reviewer.QUESTIONS[stage].items())
    return (f"\n\n아래 질문마다 답을 {path} 에 JSON으로 쓰고 같은 작업을 다시 하세요. 모양: {{\"<질문 이름>\": \"답\", ...}}. "
            "답 파일은 그 작업과 다른 명령으로 먼저 쓰세요. 같은 명령 안에서 쓰면 변경 게이트가 볼 때는 아직 답이 없습니다. "
            "질문이 해당하지 않으면 '해당 없음'과 짧은 이유를 쓰세요. 이 대화에 이미 밝힌 내용은 답에서 짧게 언급하면 됩니다. "
            + ("이 동작이 작업물을 바꾸지 않으면 change만 답하면 됩니다. " if stage == "plan" else "") + "새로 부른 검토 에이전트가 답을 원칙에 비추어 보고, 답이 이 대화 기록"
            "(커밋 때는 커밋할 내용도)과 맞는지 확인합니다. 했다·돌렸다고 쓴 것은 그 실행이 대화 기록에 있어야 합니다. 하지 않은 일은 하지 "
            "않았다고 쓰세요.\n" + asked)


def judged(state, event, stage, action, diff=None):
    """The reviewer's verdict on the agent's answers and this change's records, or ("hold", text) when the answers are
    missing, the reviewer asks to revise, or no verdict can be had."""
    path = answers_file(event, stage)
    reused = stage == "plan" and not path.exists()
    try:
        answers = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                   state.get("accepted_plan") if reused else None)
    except ValueError as error:
        return ("hold", f"답 파일이 JSON이 아닙니다({error}). 고친 뒤 같은 작업을 다시 하세요." + questions_text(path, stage)), None
    if answers is None:
        return ("hold", "변경 게이트입니다. 이 작업을 하기 전에 질문에 답하세요." + questions_text(path, stage) + principles()), None
    missing = reviewer.unanswered(answers, stage)
    # only `change` answered: the agent holds that the action does not change the work, and the reviewer judges that alone
    only_change = stage == "plan" and missing == [q for q in reviewer.QUESTIONS["plan"] if q != "change"]
    if missing and not only_change:
        return ("hold", f"답이 없는 질문: {', '.join(missing)}. 모두 답한 뒤 같은 작업을 다시 하세요." + questions_text(path, stage)), None
    try:
        records, end = reviewer.records_of(event, state.get("since", 0))
        records = reviewer.without_answers(records, path.name)
        verdict, why, todo = reviewer.review(stage, records, action, event, answers, diff,
                                             **({"reused": True} if reused else {}))
        if only_change and verdict == "proceed":  # it does change the work: the plan is still to be answered
            verdict, todo = "revise", "이 동작은 작업물을 바꿉니다. 나머지 질문에도 답하세요."
    except Exception as error:  # what cannot be judged does not go through unjudged
        return ("hold", f"판정을 하지 못해 막았습니다({type(error).__name__}: {error}). " + CANNOT), None
    if verdict != "revise":
        state["held"] = 0
        if stage == "plan" and verdict == "proceed":
            state["accepted_plan"] = answers
        path.unlink(missing_ok=True)
        return verdict, end
    if reused:
        state.pop("accepted_plan", None)
    state["held"] = state.get("held", 0) + 1
    stop = (f"\n같은 변경이 {state['held']}번째 막혔습니다. 같은 답을 되풀이하지 말고 위에 적힌 할 일을 하세요. 판정이 틀렸다고 보이면 그 근거를 "
            "들어 변경 게이트(change-principles 플러그인)의 문제로 주인님께 보고하세요. 답을 고치는 일은 주인님께 묻지 말고 직접 하세요." if state["held"] >= 3 else "")
    return ("hold", f"검토 에이전트가 보완을 요청했습니다. {why}\n할 일: {todo}\n한 뒤 답 파일({path})을 고치고 같은 작업을 다시 하세요."
                    + stop), None


def new_change(state):
    """After a passed commit the next change is judged from the records after it. Applied when that change shows itself,
    so a commit git refused is judged again from the same records."""
    if "next_since" in state:
        state["since"] = state.pop("next_since")


def on_prompt(state):
    """The owner's new words may widen the work: its next change is planned anew. The principles are told at that
    change's first hold, so a chat that changes no lasting work is not given them (owner 2026-11-15)."""
    new_change(state)
    state["planned"] = False


def before_change(state, event, action):
    """An action that may change the work, before this change's plan has passed."""
    new_change(state)
    if state.get("planned"):
        return None
    verdict, _ = judged(state, event, "plan", action)
    if verdict == "proceed":
        state["planned"] = True
    return verdict if isinstance(verdict, tuple) else None


def on_edit(state, event):
    # only lasting work is asked: not this gate's answers files, nor files in the temp folder (scripts written to look around)
    targets = edited(event)
    paths = [Path(event.get("cwd") or ".", p).resolve() for p in targets if is_code(p)]
    if not [p for p in paths if not (in_temp(p.as_posix()) or home().resolve() in p.parents) and in_scope(None, str(p))]:
        return None
    if (len(targets) == 1 and paths[0].suffix.lower() in visual.EXTENSIONS and visual.proposed(event, paths[0])
            and independent_view(None, str(paths[0].parent), None, paths[0])):
        return ("notice", "표시 값만 바뀝니다. 수정 후 실제 화면에서 적용 결과를 확인하세요. 이 변경만을 위한 자동 테스트나 코드 리뷰는 필요하지 않습니다.")
    return before_change(state, event, json.dumps({"tool": event.get("tool_name"), "input": event.get("tool_input")}, ensure_ascii=False))


def on_commit(state, target, event):
    command = command_of(event)
    paths = commit_paths(command)  # git commit -- <paths> takes only these, whatever else is staged
    files = committed_files(*target, paths)
    if paths and files == []:  # the hook does not expand $P or * in them: named paths that match nothing leave no change to judge
        return ("hold", "git commit -- 뒤에 적은 경로에서 커밋할 변경을 찾지 못했습니다. 변경 게이트는 $P 같은 셸 변수와 * 같은 패턴을 풀지 않으니, "
                        "경로를 글자 그대로 적어 다시 커밋하세요. 그 경로에 바꾼 것이 없다면 커밋할 것이 없습니다.")
    if files is not None:  # a git add in the same command has not run yet when the hook looks
        files = files + added_files(command)
    if files and not any(is_code(f) for f in files):  # documents only
        return None
    if added_files(command):  # what it adds is not staged yet when the hook looks, so its changes cannot be judged
        return ("hold", "이 커밋은 같은 명령에서 git add를 해, 커밋할 변경 내용을 아직 볼 수 없습니다. git add를 먼저 따로 돌린 뒤 git commit만 다시 하세요.")
    if files is None:
        return ("hold", f"판정을 하지 못해 막았습니다(커밋할 파일 목록을 git에서 읽지 못했습니다: {target[1]}). " + CANNOT)
    try:
        whole = (bool(files) and all(Path(f).suffix.lower() in visual.EXTENSIONS for f in files)
                 and visual_commit_command(command))
        diff = committed_diff(*target, paths, **({"whole": True} if whole else {}))
    except RuntimeError as error:
        return ("hold", f"판정을 하지 못해 막았습니다({error}). " + CANNOT)
    if (whole and visual.committed(files, diff)
            and all(independent_view(target[0], target[1], target[3], f) for f in files)):
        return None
    verdict, end = judged(state, event, "commit", command, diff)
    if isinstance(verdict, tuple):
        return verdict
    state.update(planned=False, next_since=end)  # the next change plans anew, judged from the records after this
    return None


def decide(event):
    """("hold", text) stops the tool once, None does nothing."""
    name = (event.get("tool_name") or "").split(".")[-1]
    if event.get("hook_event_name") in {"UserPromptSubmit", "pre_llm_call"}:
        check = on_prompt
    elif name in EDITS:
        check = lambda state: on_edit(state, event)
    elif name in SHELLS:
        command = command_of(event)
        args = event.get("tool_input")
        cwd = (args.get("workdir") if isinstance(args, dict) else None) or event.get("cwd")
        target = commit_target(command, cwd)
        changes = writes(command, cwd)  # a commit too is asked only where lasting work is: not in a test repository in the temp folder
        if target and changes:
            check = lambda state: on_commit(state, target, event)
        elif changes and not target and state_path(event).stem not in command:  # not its answers
            check = lambda state: before_change(state, event, json.dumps({"tool": name, "command": command}, ensure_ascii=False))
        else:
            return None
    else:
        return None
    con, state = open_state(event)
    with closing(con), con:
        answer = check(state)
        con.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(state),))
        return answer


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    event = json.load(sys.stdin)
    answer = decide(event)
    if not answer:
        return 0
    kind, text = answer
    output = ({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": TAG + " " + text}}
              if kind == "notice" else {"decision": "block", "reason": TAG + " " + text})
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
