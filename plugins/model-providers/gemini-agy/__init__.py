"""Google Gemini subscription, enabled by the server for its developer profile."""

import json
import os
import shutil
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from hermes_constants import get_hermes_home
from providers import register_provider
from providers.base import ProviderProfile
from utils import atomic_json_write


_connection_lock = Lock()
_USAGE_WINDOWS = {"5h": "5시간 한도", "weekly": "주간 한도"}


class GeminiAGYProfile(ProviderProfile):
    def subscription_allowed(self) -> bool:
        # Crema's server prepares the snapshot from the authenticated account's
        # theme. The normal settings API can edit config.yaml, never this file.
        if os.name != "posix":
            return False
        try:
            snapshot = get_hermes_home() / ".service-config.yaml"
            return (yaml.safe_load(snapshot.read_text(encoding="utf-8")) or {}).get(
                "crema_gemini_subscription") is True
        except (OSError, AttributeError, yaml.YAMLError):
            return False

    def _catalog(self) -> Path:
        return get_hermes_home() / "gemini-agy-models.json"

    def fetch_models(self, **_kwargs) -> list[str] | None:
        if not self.subscription_allowed():
            return None
        try:
            models = json.loads(self._catalog().read_text(encoding="utf-8"))
            if isinstance(models, list) and all(isinstance(item, str) and item for item in models):
                return models or None
        except (OSError, ValueError):
            pass
        return None

    def external_process_allowed(self) -> bool:
        return bool(self.fetch_models())

    def _executable(self) -> str | None:
        override = os.environ.get("HERMES_GEMINI_AGY_COMMAND", "").strip()
        if override:
            return shutil.which(override)
        return shutil.which("agy") or shutil.which(self.process_command)

    def connect_subscription(self) -> list[str]:
        from agent.gemini_agy_client import probe_models

        if not self.subscription_allowed():
            raise PermissionError("Google Gemini 구독은 Krema 개발자만 연결할 수 있습니다.")
        command = self._executable()
        if not command:
            raise RuntimeError("서버에 agy가 없습니다. main 서버에 agy를 설치하고 실행해 로그인해 주세요.")
        with _connection_lock:
            try:
                models = probe_models(command, list(self.process_args), timeout=15.0)
            except (OSError, RuntimeError) as exc:
                error = str(exc).lower()
                if any(marker in error for marker in ("auth", "login", "sign in", "signed in")):
                    raise RuntimeError("agy 로그인을 확인하지 못했습니다. main 서버에서 agy를 실행해 로그인해 주세요.") from exc
                raise RuntimeError("agy에 연결하지 못했습니다. main 서버에서 agy가 실행되는지 확인해 주세요.") from exc
            if not models:
                raise RuntimeError("agy에서 사용 가능한 모델을 받지 못했습니다. main 서버에서 agy 로그인을 확인해 주세요.")
            atomic_json_write(self._catalog(), models, mode=0o600)
            return models

    def disconnect_subscription(self) -> None:
        if not self.subscription_allowed():
            raise PermissionError("Google Gemini 구독은 Krema 개발자만 연결할 수 있습니다.")
        from agent.gemini_agy_client import reset_gemini_session

        with _connection_lock:
            self._catalog().unlink(missing_ok=True)
            reset_gemini_session(str(get_hermes_home()))

    def create_client(self, *, api_key=None, base_url=None, **_: Any) -> Any:
        from agent.gemini_agy_client import GeminiACPClient

        if not self.external_process_allowed():
            raise PermissionError("Google Gemini 구독을 Krema 개발자 설정에서 연결해 주세요.")
        if not (command := self._executable()):
            raise RuntimeError("서버에 agy가 없습니다. main 서버에 agy를 설치하고 실행해 로그인해 주세요.")
        return GeminiACPClient(api_key=api_key, base_url=base_url, command=command, args=list(self.process_args))

    def fetch_account_usage(self, *, base_url=None, api_key=None):
        """The subscription's limits as agy's own ``/usage`` prints them; agy answers it without a model call."""
        import subprocess
        from datetime import datetime, timezone
        from tempfile import TemporaryDirectory

        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
        from agent.acp_client import _build_subprocess_env

        if not self.external_process_allowed() or not (command := self._executable()):
            return None
        with TemporaryDirectory(prefix="crema-agy-") as cwd:
            # agy takes about 5 s; the engine's hook deadline is 10 s.
            done = subprocess.run(
                [command, "-p", "/usage", "--output-format", "json"], cwd=cwd, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", timeout=9.0, check=True,
                env=_build_subprocess_env(inherit_credentials=False))
        windows = [
            AccountUsageWindow(
                label=f"{_USAGE_WINDOWS.get(bucket.get('window'), bucket['name'])} · {group['name']}",
                used_percent=(1 - bucket["remaining_fraction"]) * 100,
                reset_at=datetime.fromisoformat(bucket["reset_time"].replace("Z", "+00:00")))
            for group in json.loads(done.stdout)["command"]["data"]["groups"] for bucket in group["buckets"]
        ]
        return AccountUsageSnapshot(provider=self.name, source="agy_usage_command",
                                    fetched_at=datetime.now(timezone.utc), windows=tuple(windows))

    def build_api_kwargs_extras(self, *, session_id=None, ephemeral_system_prompt=None, crema_service_policy=None, **_):
        # Auxiliaries do not get the conversational route's Hermes session ID.
        return {}, {"crema_session_id": session_id, "crema_run_instructions": ephemeral_system_prompt,
                    "crema_service_policy": crema_service_policy}


register_provider(GeminiAGYProfile(
    name="gemini-agy", display_name="Google Gemini 구독",
    api_mode="chat_completions", base_url="acp://gemini-agy", auth_type="external_process",
    process_command=shutil.which("agy") or str(Path.home() / ".local/bin/agy"),
    process_command_env_vars=("HERMES_GEMINI_AGY_COMMAND",),
    process_args=("acp", "--sandbox", "--include-default-customizations=false"),
))
