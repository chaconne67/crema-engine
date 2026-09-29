"""web_deps.late: a router helper called while another thread is still importing its module waits for
that import instead of reading the half-built module (Crema: the first /api/env of an app start failed
with "partially initialized module 'hermes_cli.web_server_messaging'")."""
import sys
import threading
import time

from hermes_cli.web_deps import late


def test_late_waits_for_a_module_another_thread_is_importing(tmp_path, monkeypatch):
    (tmp_path / "crema_slow_router_mod.py").write_text(
        "import time\ntime.sleep(0.5)\ndef helper():\n    return 'ready'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "crema_slow_router_mod", raising=False)

    importer = threading.Thread(target=__import__, args=("crema_slow_router_mod",))
    importer.start()
    deadline = time.time() + 5
    while "crema_slow_router_mod" not in sys.modules and time.time() < deadline:
        time.sleep(0.01)
    try:
        assert late("helper", "crema_slow_router_mod")() == "ready"
    finally:
        importer.join()
