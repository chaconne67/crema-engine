"""Check Crema's file and feature inventories against Git and export rules."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "CREMA_CHANGES.tsv"
FEATURES = ROOT / "CREMA_FEATURES.tsv"
ATTRIBUTES = ROOT / ".gitattributes"
HEADER = "path\tkind\twhat\twhy\tbasis\tfeatures\tnon_feature_reason"
FEATURE_HEADER = "id\tname\tkind\tbehavior\twhy\tbasis\ttests\tinspect\tverify\tstatus"
BASE_REFS = ("refs/heads/hermes-upstream", "refs/remotes/origin/hermes-upstream")
KINDS = {"M": "modified", "A": "added"}
EXCLUDED = "싣지 않는 기능"
FEATURE_KINDS = {"더한 기능", "고친 기능", EXCLUDED}
FEATURE_STATUSES = {"온전히 있다", "일부만 있다", "없다"}
NON_FEATURE = "non-feature"
EMPTY = "-"
EXPORT_LABEL = "# crema-feature "
# The first field is a pattern, possibly C-quoted by Git. Only later fields are attributes.
ATTRIBUTE_RULE = re.compile(r'(?:"(?:[^"\\]|\\.)*"|\S+)\s+(.+)')


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


def table_rows(path, expected_header, errors):
    content = path.read_bytes().decode("utf-8")
    header, _, body = content.partition("\n")
    if header != expected_header:
        errors.append(f"{path.name}:1: expected header {expected_header!r}")
    if not content.endswith("\n"):
        errors.append(f"{path.name}: expected an LF at the end of the file")

    field_count = expected_header.count("\t") + 1
    for number, line in enumerate(body.removesuffix("\n").split("\n") if body else (), 2):
        fields = line.split("\t")
        location = f"{path.name}:{number} ({fields[0]!r})"
        if len(fields) != field_count or "\r" in line or not all(field.strip() for field in fields):
            errors.append(f"{location}: expected {field_count} nonempty tab-separated fields with LF line endings")
            continue
        yield location, fields


def check_list(expected, features, errors):
    paths = set()
    linked = set()
    previous = None
    unknown = 0
    for location, (path, kind, _, why, _, members, reason) in table_rows(LIST, HEADER, errors):
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
        if members == NON_FEATURE:
            if reason == EMPTY:
                errors.append(f"{location}: non-feature requires a reason")
            continue
        ids = members.split(",")
        if reason != EMPTY or len(set(ids)) != len(ids):
            errors.append(f"{location}: feature links must be unique and use '-' for non_feature_reason")
        for feature_id in ids:
            if feature_id not in features:
                errors.append(f"{location}: unknown feature {feature_id!r}")
            elif path in expected:
                linked.add(feature_id)

    errors.extend(f"{LIST.name}: {path!r} missing from list (Git kind {expected[path]!r})"
                  for path in sorted(expected.keys() - paths))
    return unknown, linked


def check_features(tracked, errors):
    features = {}
    for location, (feature_id, _, kind, _, _, _, tests, inspect, _, status) in table_rows(
        FEATURES, FEATURE_HEADER, errors
    ):
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", feature_id) or feature_id == NON_FEATURE:
            errors.append(f"{location}: invalid feature ID")
        if feature_id in features:
            errors.append(f"{location}: duplicate feature ID")
        if kind not in FEATURE_KINDS or status not in FEATURE_STATUSES:
            errors.append(f"{location}: invalid feature kind or status")
        for field, value in (("tests", tests), ("inspect", inspect)):
            if value == EMPTY:
                continue
            for entry in value.split(","):
                path, separator, symbol = entry.partition("::")
                if (field == "tests" and separator) or (separator and not symbol):
                    errors.append(f"{location}: malformed {field} reference {entry!r}")
                if path not in tracked:
                    errors.append(f"{location}: {field} path {path!r} is not in HEAD")
        features[feature_id] = (kind, status)
    return features


def check_exports(features, errors):
    feature_id, reason = "", ""
    linked = set()
    count = 0
    for number, line in enumerate(ATTRIBUTES.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if line.startswith(EXPORT_LABEL):
            feature_id, _, reason = line.removeprefix(EXPORT_LABEL).partition(" ")
            continue
        rule = ATTRIBUTE_RULE.fullmatch(line) if not line.startswith("#") else None
        if rule is None or "export-ignore" not in rule[1].split():
            feature_id, reason = "", ""
            continue
        count += 1
        if feature_id == NON_FEATURE and reason.strip() and reason != EMPTY:
            continue
        if features.get(feature_id, (None, None))[0] != EXCLUDED or reason:
            errors.append(f"{ATTRIBUTES.name}:{number}: export-ignore needs an excluded feature or non-feature reason")
        else:
            linked.add(feature_id)
    return count, linked


def main():
    errors = []
    try:
        expected, counts, deleted = git_changes()
        tracked = set(git("ls-tree", "-r", "--name-only", "-z", "HEAD").decode("utf-8").split("\0")[:-1])
        features = check_features(tracked, errors)
        unknown, linked = check_list(expected, features, errors)
        export_count, excluded = check_exports(features, errors)
        errors.extend(f"{path!r}: upstream file deleted" for path in deleted)
        for feature_id, (kind, status) in features.items():
            if kind == EXCLUDED and feature_id not in excluded:
                errors.append(f"{FEATURES.name}: {feature_id!r} has no export-ignore rule")
            elif kind != EXCLUDED and status != "없다" and feature_id not in linked:
                errors.append(f"{FEATURES.name}: {feature_id!r} has no changed file")
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        errors = [detail or str(error)]
    except (OSError, ValueError) as error:
        errors = [str(error)]

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"M (modified): {counts['M']}, A (added): {counts['A']}, D (deleted): {counts['D']}")
    print(f"까닭 미확인: {unknown}")
    print(f"기능: {len(features)}, export-ignore: {export_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
