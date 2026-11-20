"""Apply the application's assembled engine input on either Crema startup surface."""

import inspect
import json
import os
from functools import lru_cache, wraps
from pathlib import Path


@lru_cache(maxsize=1)
def _deployment(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def engine_input(config):
    path = os.environ.get("CREMA_ENGINE_INPUT")
    if not path:
        return None
    deployment = _deployment(path)
    theme = deployment["themeIds"][0]
    if deployment["placement"] != "embedded":
        theme = config.get("crema_theme", theme)
    return deployment["engineInputs"][theme]


def connection_policy():
    from hermes_cli.config import load_config_readonly
    startup = engine_input(load_config_readonly()) if os.environ.get("CREMA_ENGINE_INPUT") else None
    return startup["connectionPolicy"] if startup is not None else {}


def prepare_desktop(isolate_connections):
    startup = engine_input({})
    if startup is None or startup["brain"] != "pc":
        raise ValueError("CREMA_ENGINE_INPUT must point to an embedded engine deployment")
    if startup["connectionPolicy"]["source"] == "local":
        isolate_connections()
    os.environ["HERMES_BUNDLED_SKILLS"] = str(Path(os.environ["CREMA_ENGINE_INPUT"]).with_name("skills"))
    if any(hook.startswith("engine/tools/crema_failures.py:")
           for hooks in startup["hooks"].values() for hook in hooks):
        from tools import crema_failures
        crema_failures.watch()


def desktop_toolsets(config, platform, names):
    """Use the assembled default chat tools and honour existing per-user selections."""
    if platform != "api_server":
        return names
    startup = engine_input(config)
    if startup is None or startup["brain"] != "pc":
        return names
    base = startup["tools"]["chat"]
    if names is None:
        return base
    # Older settings save a platform composite plus the user's media selections.
    # Expand that composite from the same input as an untouched home.
    return list(dict.fromkeys(
        item
        for name in names
        for item in (base if name == "hermes-api-server" else [name])
    ))


def hook_id(callback, home):
    try:
        fn = inspect.unwrap(callback)
    except ValueError:
        return None
    module = getattr(fn, "__module__", None)
    if module == "agent.outbound_webhooks" and fn.__name__.startswith("outbound_webhook["):
        return "outbound:" + fn.__name__.split(":", 1)[1][:-1]
    if not (inspect.isfunction(fn) or inspect.ismethod(fn)):
        return None
    source = inspect.getsourcefile(fn)
    if source is None:
        return None
    source = Path(source)
    for base, prefix in ((home / "plugins", "plugins"), (Path(__file__).parent, "engine")):
        if source.is_relative_to(base):
            return f"{prefix}/{source.relative_to(base).as_posix()}:{fn.__qualname__}"
    if module == "fc_lifecycle":
        return f"server/fc_lifecycle.py:{fn.__qualname__}"
    return None


def _callbacks(desktop):
    from agent import crema_file_turns
    from crema_engine import drop_next_input
    from tools import crema_failures

    desktop_hooks = (crema_file_turns.before_tool, crema_file_turns.after_tool, crema_file_turns.notes_for_turn,
                     crema_file_turns.tool_result) if desktop else ()
    return (*desktop_hooks, *crema_failures.observers(), drop_next_input)


def _profile_hooks(selected, additions):
    hooks = {event: list(callbacks) for event, callbacks in selected.items()}
    for event, callbacks in additions.items():
        position = {"on_session_end": 0, "post_tool_call": 1}.get(event, len(hooks.get(event, ())))
        hooks.setdefault(event, [])[position:position] = callbacks
    return hooks


def _chain_reply(first, second):
    first = inspect.unwrap(first)
    @wraps(first)
    def transform(response_text, **info):
        noticed = first(response_text=response_text, **info)
        cleaned = second(response_text=noticed or response_text, **info)
        return cleaned if cleaned is not None else noticed
    return transform


def select_hooks(manager, config):
    """Put the selected Crema callbacks in their declared order. Preserve other plugin hooks."""
    startup = engine_input(config)
    if startup is None:
        return
    from utils import env_var_enabled
    if env_var_enabled("HERMES_SAFE_MODE"):
        return

    home = Path(manager.home_path)
    selected = _profile_hooks(startup["hooks"], config.get("crema_profile_hooks", {}))
    own_plugins = tuple(f"plugins/{name}/" for name in startup["plugins"]["enabled"])
    builtins = {hook_id(callback, home): callback for callback in _callbacks(startup["brain"] == "pc")}
    target_names = {f"outbound:{entry['name']}" for entry in startup["hookOptions"].get("outbound", [])}
    ordered = {}
    for event in dict.fromkeys([*manager._hooks, *selected]):
        available = dict(builtins)
        extensions = []
        config_hooks = []
        wanted = selected.get(event, ())
        for callback in manager._hooks.get(event, ()):
            name = hook_id(callback, home)
            if name and (name in wanted or name in builtins or name in target_names
                         or name.startswith((*own_plugins, "server/fc_lifecycle.py:"))):
                available.setdefault(name, callback)
            elif getattr(callback, "__module__", None) in {"agent.shell_hooks", "agent.outbound_webhooks"}:
                config_hooks.append(callback)
            else:
                extensions.append(callback)
        required = [name for name in wanted if name not in available and not name.startswith("outbound:")]
        if required:
            raise ValueError(f"Missing engine hooks for {event}: {', '.join(required)}")
        callbacks = extensions + [available[name] for name in wanted if name in available] + config_hooks
        pair = ("plugins/settings/browser_fallback.py:reply", "engine/crema_engine.py:drop_next_input")
        if event == "transform_llm_output" and all(name in wanted for name in pair):
            first, second = (available[name] for name in pair)
            # The engine uses the first replacement. Take both transformations at that seam.
            callbacks = [(_chain_reply(first, second) if callback is first else callback)
                         for callback in callbacks if callback is not second]
        from crema_engine import change_principles_hook, change_principles_start
        if event in ("pre_tool_call", "pre_llm_call"):
            gate = change_principles_hook if event == "pre_tool_call" else change_principles_start
            callbacks = [callback for callback in callbacks if callback is not gate]
            if "changePrinciples" in startup["hookOptions"]:
                callbacks.insert(0, gate)
        if callbacks:
            ordered[event] = callbacks
    # Keep the original mapping because plugin registration leases capture it.
    for event in manager._hooks.keys() - ordered.keys():
        del manager._hooks[event]
    manager._hooks.update(ordered)


def initialize(*, api=None):
    """Discover providers and register the scoped config hooks using their existing loaders."""
    from agent.outbound_webhooks import register_from_config as register_outbound
    from agent.shell_hooks import register_from_config as register_shell
    from hermes_cli.config import load_config
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    config = load_config()
    startup = engine_input(config)
    if config.get("crema_profile_hooks"):
        from tools import crema_failures
        crema_failures.watch()
    if api is not None:
        from agent import crema_file_turns
        crema_file_turns.install(api)
    if startup is not None:
        discover_plugins()
    manager = get_plugin_manager()
    with manager._discovery_lock:
        if startup is None or startup["hookOptions"].get("userShellHooks") == "allowlisted":
            register_shell(config, accept_hooks=False)
        if startup is None or startup["hookOptions"].get("outbound"):
            register_outbound(config)
        select_hooks(manager, config)
