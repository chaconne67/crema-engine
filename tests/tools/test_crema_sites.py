"""Crema My Sites (tools/crema_sites.py): the list, the login window, the saved login, removing, the agent's tool."""
import json
import sqlite3

import pytest

from agent.vault_store import get_vault_store
from tools import crema_sites as sites

PASSWORD = "s3cret-pw!"


class FakeChrome:
    launched = []

    def __init__(self, argv):
        FakeChrome.launched.append(argv)
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True


@pytest.fixture(autouse=True)
def window(monkeypatch):
    FakeChrome.launched = []
    closed = []
    monkeypatch.setattr(sites, "browser_executable", lambda: "chrome.exe")
    monkeypatch.setattr(sites.subprocess, "Popen", FakeChrome)
    monkeypatch.setattr(sites, "_devtools", lambda: None)
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: closed.append(True))
    sites._window.clear()
    yield closed
    sites._window.clear()


def saved():
    return json.loads((sites._home() / "sites.json").read_text(encoding="utf-8"))


def test_site_domain():
    assert sites.site_domain("sell.smartstore.naver.com") == "naver.com"
    assert sites.site_domain("nid.naver.com") == "naver.com"
    assert sites.site_domain("www.hometax.go.kr") == "hometax.go.kr"
    assert sites.site_domain("gw.example.co.kr") == "example.co.kr"
    assert sites.site_domain("github.com") == "github.com"
    assert sites.site_domain("192.168.0.10") == "192.168.0.10"


def test_a_new_site_is_kept_only_when_the_user_is_done(window):
    started = sites.start_login(url="www.4insure.or.kr", name="", identifier="me@corp.kr", password=PASSWORD)
    argv = FakeChrome.launched[0]
    assert argv[0] == "chrome.exe" and argv[-1] == "https://www.4insure.or.kr"
    assert f"--user-data-dir={sites.profile_dir()}" in argv
    assert window, "the agent's browser on the profile is closed first"
    assert sites.state() == {"sites": [], "browser": True,
                             "waiting": {"id": started["site"], "name": "", "url": "https://www.4insure.or.kr"}}
    assert "--restore-last-session" in argv

    done = sites.finish_login()["site"]
    assert done["name"] == "www.4insure.or.kr"  # no title from a window that is gone: the address
    assert done["identifier"] == "me@corp.kr" and done["logged_in_at"] and not done["needs_login"]
    assert PASSWORD not in (sites._home() / "sites.json").read_text(encoding="utf-8")
    item = get_vault_store().get_meta(saved()[0]["vault"])
    assert item.origin == "https://www.4insure.or.kr" and item.identifier == "me@corp.kr"
    assert sites.state()["waiting"] is None


def test_an_unnamed_site_takes_its_page_title(monkeypatch):
    monkeypatch.setattr(sites, "_page_title", lambda record: "4대 사회보험 정보연계센터")
    sites.start_login(url="https://www.4insure.or.kr")
    assert sites.finish_login()["site"]["name"] == "4대 사회보험 정보연계센터"
    assert "vault" not in saved()[0]


def test_a_failing_vault_still_keeps_the_signed_in_site(monkeypatch):
    from agent.vault_store import VaultError, VaultStore

    def broken(*args, **kwargs):
        raise VaultError("vault file could not be decrypted")

    monkeypatch.setattr(VaultStore, "add_item", broken)
    sites.start_login(url="github.com", name="깃허브", identifier="me", password=PASSWORD)
    with pytest.raises(VaultError):
        sites.finish_login()
    assert [site["name"] for site in saved()] == ["깃허브"] and "vault" not in saved()[0]


def test_cancel_leaves_no_new_site():
    sites.start_login(url="example.com", name="예시")
    sites.cancel_login()
    assert sites.state() == {"sites": [], "browser": True, "waiting": None}


def test_signing_in_again_keeps_the_site_in_place():
    for url in ("a.example.com", "b.example.org"):
        sites.start_login(url=url, name=url[0])
        sites.finish_login()
    sites._save([{**site, "needs_login": True} if site["name"] == "a" else site for site in saved()])
    sites.start_login(site="a")
    sites.finish_login()
    assert [(site["name"], site["needs_login"]) for site in saved()] == [("a", False), ("b", False)]


def test_no_browser_and_no_address(monkeypatch):
    with pytest.raises(ValueError):
        sites.start_login(url="  ")
    with pytest.raises(ValueError):
        sites.start_login(url="example.com", password="only-a-password")
    assert not FakeChrome.launched
    monkeypatch.setattr(sites, "browser_executable", lambda: None)
    assert sites.state()["browser"] is False
    with pytest.raises(LookupError):
        sites.start_login(url="example.com")


def test_rename_and_save_login_replace_the_old_one():
    sites.start_login(url="hometax.go.kr", name="홈택스", identifier="old", password="old-pw")
    sites.finish_login()
    first = saved()[0]["vault"]
    sites.rename("홈택스", "국세청 홈택스")
    sites.save_login("국세청 홈택스", "chaconne67", PASSWORD)
    record = saved()[0]
    assert record["name"] == "국세청 홈택스" and record["vault"] != first
    assert get_vault_store().get_meta(first) is None
    assert get_vault_store().resolve_secret(record["vault"])["password"] == PASSWORD
    with pytest.raises(ValueError):
        sites.rename("국세청 홈택스", " ")
    with pytest.raises(ValueError):
        sites.save_login("국세청 홈택스", "chaconne67", "")


def test_remove_takes_the_login_and_the_domain_cookies(window):
    sites.start_login(url="sell.smartstore.naver.com", name="스마트스토어", identifier="me", password=PASSWORD)
    sites.finish_login()
    vault = saved()[0]["vault"]
    cookies = sites.profile_dir() / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cookies) as db:
        db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
        db.executemany("INSERT INTO cookies VALUES (?, ?)", [(".naver.com", "NID"), ("sell.smartstore.naver.com", "s"),
                                                            ("naver.com", "n"), (".github.com", "gh")])
    window.clear()
    sites.remove("스마트스토어")
    assert window and saved() == [] and get_vault_store().get_meta(vault) is None
    with sqlite3.connect(cookies) as db:
        assert [row[0] for row in db.execute("SELECT host_key FROM cookies")] == [".github.com"]


def test_the_agents_list_and_mark():
    assert json.loads(sites.my_sites({"action": "list"})) == {"sites": []}
    sites.start_login(url="hometax.go.kr", name="홈택스", identifier="chaconne67", password=PASSWORD)
    sites.finish_login()
    listed = sites.my_sites({"action": "list"})
    assert PASSWORD not in listed
    assert json.loads(listed)["sites"] == [{"name": "홈택스", "url": "https://hometax.go.kr", "signed_in": True,
                                            "identifier": "chaconne67"}]
    sites.my_sites({"action": "mark_logged_out", "site": "홈택스"})
    assert json.loads(sites.my_sites({"action": "list"}))["sites"][0]["signed_in"] is False
    assert "error" in json.loads(sites.my_sites({"action": "mark_logged_out", "site": "없는 곳"}))


def test_fill_login_only_on_the_sites_domain(monkeypatch):
    import tools.browser_vault_tool as vault_tool

    sites.start_login(url="sell.smartstore.naver.com", name="스마트스토어", identifier="me", password=PASSWORD)
    sites.finish_login()
    filled = []
    page = {"origin": "https://evil.example.com"}
    monkeypatch.setattr(vault_tool, "_focus_bound_origin", lambda *args: None)
    monkeypatch.setattr(vault_tool, "_current_page_origin", lambda task_id: page["origin"])
    monkeypatch.setattr(vault_tool, "browser_vault_fill", lambda handle, task_id=None: filled.append(handle) or '{"success": true}')

    refused = json.loads(sites.my_sites({"action": "fill_login", "site": "스마트스토어"}, task_id="t"))
    assert "error" in refused and not filled

    page["origin"] = "https://nid.naver.com"
    assert json.loads(sites.my_sites({"action": "fill_login", "site": "스마트스토어"}, task_id="t")) == {"success": True}
    moved = get_vault_store().get_meta(saved()[0]["vault"])
    assert filled == [moved.id] and moved.origin == "https://nid.naver.com" and moved.identifier == "me"
    assert get_vault_store().resolve_secret(moved.id)["password"] == PASSWORD
    assert len(get_vault_store().list_items()) == 1


def test_fill_login_without_a_saved_login():
    sites.start_login(url="github.com", name="깃허브")
    sites.finish_login()
    assert "no saved login" in json.loads(sites.my_sites({"action": "fill_login", "site": "깃허브"}))["error"]


def test_the_tool_shows_with_a_browser_even_before_a_site(monkeypatch):
    # With no site yet the agent still learns from the tool to send the user to Settings → My Sites.
    from tools.registry import registry
    from tools.tool_search import is_deferrable_tool_name

    entry = registry._tools["my_sites"]
    assert entry.check_fn() is True
    assert not is_deferrable_tool_name("my_sites")
    monkeypatch.setattr(sites, "browser_executable", lambda: None)
    assert entry.check_fn() is False
