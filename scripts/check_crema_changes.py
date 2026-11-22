"""Check CREMA_CHANGES.tsv against the Hermes baseline and the current HEAD."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "CREMA_CHANGES.tsv"
HEADER = "path\tkind\twhat\twhy\tbasis"
BASE_REFS = ("refs/heads/hermes-upstream", "refs/remotes/origin/hermes-upstream")
KINDS = {"M": "modified", "A": "added"}


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=True,
    ).stdout


def baseline():
    for ref in BASE_REFS:
        try:
            git("show-ref", "--verify", "--quiet", ref)
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
        else:
            return ref
    raise ValueError("Hermes baseline branch missing (tried hermes-upstream and origin/hermes-upstream)")


def git_changes():
    raw = git("diff", "--name-status", "--no-renames", "-z", baseline(), "HEAD")
    tokens = iter(raw.decode("utf-8").removesuffix("\0").split("\0") if raw else ())
    expected = {}
    counts = dict.fromkeys(("M", "A", "D"), 0)
    deleted = []
    for status, path in zip(tokens, tokens, strict=True):
        if status in counts:
            counts[status] += 1
        if status in KINDS:
            expected[path] = KINDS[status]
        elif status == "D":
            deleted.append(path)
    return expected, counts, deleted


def check_list(expected):
    content = LIST.read_bytes().decode("utf-8")
    header, _, body = content.partition("\n")
    errors = []
    if header != HEADER:
        errors.append(f"line 1: expected header {HEADER!r}")
    if not content.endswith("\n"):
        errors.append("expected an LF at the end of the file")

    paths = set()
    previous = None
    unknown = 0
    for number, line in enumerate(body.removesuffix("\n").split("\n") if body else (), 2):
        fields = line.split("\t")
        path = fields[0]
        location = f"line {number} ({path!r})"
        if len(fields) != 5 or "\r" in line or not all(field.strip() for field in fields):
            errors.append(f"{location}: expected five nonempty tab-separated fields with LF line endings")
            continue
        _, kind, _, why, _ = fields
        if path in paths:
            errors.append(f"{location}: duplicate path")
        if previous is not None and path < previous:
            errors.append(f"{location}: path sorts before {previous!r}")
        if path not in expected:
            errors.append(f"{location}: path is not added or modified in Git")
        elif kind != expected[path]:
            errors.append(f"{location}: kind is {kind!r}, expected {expected[path]!r}")
        paths.add(path)
        previous = path
        unknown += why == "까닭 미확인"

    errors.extend(f"{path!r}: missing from list (Git kind {expected[path]!r})" for path in sorted(expected.keys() - paths))
    return errors, unknown


def main():
    try:
        expected, counts, deleted = git_changes()
        errors, unknown = check_list(expected)
        errors.extend(f"{path!r}: upstream file deleted" for path in deleted)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        errors = [detail or str(error)]
    except (OSError, ValueError) as error:
        errors = [str(error)]

    if errors:
        for error in errors:
            print(f"{LIST.name}: {error}", file=sys.stderr)
        return 1

    print(f"M (modified): {counts['M']}, A (added): {counts['A']}, D (deleted): {counts['D']}")
    print(f"까닭 미확인: {unknown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
