"""Home initialization must survive a SOUL.md symlink the seed cannot write through.

A cyclic ``SOUL.md -> SOUL.md`` link (ELOOP) or a link dangling into a missing directory
(ENOENT) made the first-run seed raise OSError, which ``initialize_home`` turned into
``HomeInitializationError`` on every gateway spawn (launchd exit-75 relaunch storm). The seed
now replaces such a link with the default identity file; a link that resolves is operator
wiring and stays a link.
"""

import pytest

from hermes_cli.config import DEFAULT_SOUL_MD, _ensure_default_soul_md
from hermes_cli.config_home import initialize_home

_SUBDIRS = ("cron", "sessions", "logs", "memories")


@pytest.mark.parametrize("target", ["SOUL.md", "missing-dir/SOUL.md"], ids=["cyclic", "dangling"])
def test_initialize_home_replaces_unwritable_soul_symlink(tmp_path, target):
    home = tmp_path / ".hermes"
    home.mkdir()
    soul = home / "SOUL.md"
    soul.symlink_to(home / target)

    initialize_home(home, _SUBDIRS, set())

    assert not soul.is_symlink()
    assert soul.read_text(encoding="utf-8") == DEFAULT_SOUL_MD
    assert not [p for p in home.iterdir() if p.name.startswith(".SOUL.md.")]


def test_soul_symlink_to_customized_file_is_left_alone(tmp_path):
    """Control: a resolving link survives and its content is untouched."""
    home = tmp_path / ".hermes"
    home.mkdir()
    target = tmp_path / "shared-identity.md"
    target.write_text("custom identity\n", encoding="utf-8")
    soul = home / "SOUL.md"
    soul.symlink_to(target)

    _ensure_default_soul_md(home)
    initialize_home(home, _SUBDIRS, set())

    assert soul.is_symlink()
    assert soul.read_text(encoding="utf-8") == "custom identity\n"


def test_hermes_default_soul_becomes_cremas_and_a_written_one_stays(tmp_path):
    """Crema: a SOUL.md still holding Hermes' seeded default is replaced by Crema's on the next start;
    one the person wrote is never touched."""
    from hermes_cli.default_soul import _HERMES_SOUL_MD

    seeded, written = tmp_path / "seeded", tmp_path / "written"
    for home, text in ((seeded, _HERMES_SOUL_MD.replace("\n", "\r\n") + "\r\n"), (written, "당신은 JUDY입니다.\n")):
        home.mkdir()
        (home / "SOUL.md").write_text(text, encoding="utf-8")
        _ensure_default_soul_md(home)

    assert (seeded / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD
    assert "Hermes" not in DEFAULT_SOUL_MD and "Crema" in DEFAULT_SOUL_MD
    assert (written / "SOUL.md").read_text(encoding="utf-8") == "당신은 JUDY입니다.\n"
