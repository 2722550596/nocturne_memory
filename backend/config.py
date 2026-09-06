"""
Configuration management via config.json.

First run (no config.json):
  1. Legacy .env with app-level keys (DATABASE_URL etc.)? → migrate to config.json
  2. Environment variables exist (Docker)? → generate config.json from them
  3. Nothing? → create config.json with defaults

After config.json exists: it is the file-level source of truth. Per-process
environment variables (WEB_PORT, WORLD_CLOCK_*, DATABASE_URL, CORE_MEMORY_URIS…)
override the file values at load time, so multiple role processes can share one
config.json while diverging on port / world clock / namespace. This is the
multi-role (multi-process, shared DB) mode; see `_env_overrides`.

IMPORTANT: config.py NEVER writes to .env. The .env → config.json migration is
read-only and one-directional. .env files containing only Docker Compose vars
(POSTGRES_USER/PASSWORD/DB) are ignored to prevent generating default configs.
"""

import contextvars
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from locales import t

_BACKEND_DIR = Path(__file__).resolve().parent
# 兼容 Docker 部署：Dockerfile 把 backend/* 复制到 WORKDIR，所以容器内
# _BACKEND_DIR 本身就是根目录；本地开发则是 backend/，根目录在上一级。
_IN_DOCKER = Path("/.dockerenv").exists()
ROOT_DIR = _BACKEND_DIR if _IN_DOCKER else _BACKEND_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"


def _parse_config_override() -> Optional[Path]:
    """从命令行 --config 参数解析 config.json 路径，支持每世界独立 DB。"""
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=str, help="Path to config.json (e.g. for multi-world isolation)")
    args, _ = parser.parse_known_args()
    if args.config:
        return Path(args.config).resolve()
    return None


_CONFIG_OVERRIDE = _parse_config_override()
if _CONFIG_OVERRIDE:
    CONFIG_PATH = _CONFIG_OVERRIDE


def set_config_path(path: Path) -> None:
    """Override the config path dynamically (e.g., for tests or seed scripts)."""
    global CONFIG_PATH
    CONFIG_PATH = path.resolve()
    _invalidate()

_DEMO_DB = "demo.db"
_USER_DB = "nocturne_data.db"


def _default_database_url() -> str:
    db_path = (ROOT_DIR / _DEMO_DB).resolve()
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


DEFAULTS: dict[str, Any] = {
    "database_url": _default_database_url(),
    "valid_domains": ["core", "writer", "game", "notes", "narrative"],
    "boot_uris": {"": ["core://agent", "core://my_user", "core://agent/my_user"]},
    "host": "127.0.0.1",
    "web_port": 8233,
    "auto_open_browser": True,
    "api_token": None,
    "cors_origins": None,
    "public_readonly_mcp": False,
    "skip_migration_backup": False,
    "locale": None,
    # Semantic search (vector) config. api_key empty -> semantic search
    # degrades to lexical search; the recall extension stays untouched.
    "embedding": {
        "model": "BAAI/bge-large-zh-v1.5",
        "api_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "batch_size": 32,
        "max_input_chars": 500,
        "chunk_overlap": 80,
    },
}

_ENV_MAP: dict[str, str] = {
    "database_url": "DATABASE_URL",
    "valid_domains": "VALID_DOMAINS",
    "host": "HOST",
    "web_port": "WEB_PORT",
    "auto_open_browser": "AUTO_OPEN_BROWSER",
    "api_token": "API_TOKEN",
    "public_readonly_mcp": "PUBLIC_READONLY_MCP",
    "skip_migration_backup": "SKIP_MIGRATION_BACKUP",
    "locale": "LOCALE",
}

# world_clock is a nested dict; these flat env keys map into it.
_WORLD_CLOCK_ENV_MAP: dict[str, str] = {
    "WORLD_CLOCK_ENABLED": "enabled",
    "WORLD_CLOCK_CURRENT_TIME": "current_time",
    "WORLD_CLOCK_AUTO_TIMESTAMP": "auto_timestamp",
    "WORLD_CLOCK_SHOW_RELATIVE": "show_relative",
    "WORLD_CLOCK_FORMAT": "format",
}

# embedding is a nested dict; these flat env keys map into it.
_EMBEDDING_ENV_MAP: dict[str, str] = {
    "NOCTURNE_EMBEDDING_MODEL": "model",
    "NOCTURNE_EMBEDDING_API_URL": "api_url",
    "NOCTURNE_EMBEDDING_API_KEY": "api_key",
    "NOCTURNE_EMBEDDING_BATCH_SIZE": "batch_size",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class ConfigWriteError(Exception):
    """Raised when config.json cannot be written due to permissions."""
    pass


def _docker_setup_hint() -> str:
    return t("config.docker_hint")


def _save_file(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except PermissionError as e:
        import sys
        print(t("config.permission_denied").format(filename=CONFIG_PATH.name), file=sys.stderr)
        msg = t("config.permission_denied").format(filename=CONFIG_PATH.name) + " "
        if _IN_DOCKER:
            msg += t("config.permission_docker_hint")
        else:
            msg += t("config.permission_local_hint")
        raise ConfigWriteError(msg) from e


def _coerce(key: str, raw: str) -> Any:
    if key == "valid_domains":
        return [d.strip() for d in raw.split(",") if d.strip()]
    if key == "web_port":
        return int(raw)
    if key in ("auto_open_browser", "public_readonly_mcp", "skip_migration_backup"):
        return raw.lower() not in ("false", "0", "no")
    return raw


def _db_path_from_url(url: str) -> Optional[Path]:
    """Extract the file path from a sqlite database_url, or None if not sqlite."""
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix):]
    return Path(raw) if raw else None


def _make_db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _unique_db_path(directory: Path, base_name: str) -> Path:
    """Return a non-colliding path under *directory*. Tries base_name first,
    then appends random hex suffixes until a free slot is found."""
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    candidate = directory / base_name
    if not candidate.exists():
        return candidate
    for _ in range(100):
        rand = secrets.token_hex(3)
        candidate = directory / f"{stem}_{rand}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{secrets.token_hex(8)}{suffix}"


def _migrate_away_from_demo(cfg: dict) -> bool:
    """If database_url points to demo.db, copy it to a user-owned file that is
    safe from ``git pull`` overwrites.  Returns True if the config was changed."""
    url = cfg.get("database_url", "")
    db_path = _db_path_from_url(url)
    if db_path is None:
        return False
    if db_path.name != _DEMO_DB:
        return False

    target = _unique_db_path(db_path.parent, _USER_DB)

    if db_path.exists():
        shutil.copy2(str(db_path), str(target))
        print(
            t("config.demo_copied").format(demo_db=_DEMO_DB, target=target.name),
            file=sys.stderr,
        )
    else:
        print(t("config.using_db").format(name=target.name), file=sys.stderr)

    cfg["database_url"] = _make_db_url(target)
    return True


def _extract_boot_uris(source: dict) -> dict[str, list[str]]:
    """Extract boot URI config from a flat key-value dict (os.environ)."""
    boot: dict[str, list[str]] = {}
    if "CORE_MEMORY_URIS" in source:
        base = source["CORE_MEMORY_URIS"] or ""
        boot[""] = [u.strip() for u in base.split(",") if u.strip()]
    for key, val in source.items():
        if key.startswith("CORE_MEMORY_URIS__"):
            ns = key[len("CORE_MEMORY_URIS__"):]
            val_str = val or ""
            boot[ns] = [u.strip() for u in val_str.split(",") if u.strip()]
    return boot


def _extract_world_clock(source: dict) -> dict:
    """Extract a nested world_clock dict from flat env keys.

    Only keys present in *source* are set, so partial overrides merge with the
    file-level clock instead of replacing it wholesale.
    """
    clock: dict = {}
    for env_key, cfg_key in _WORLD_CLOCK_ENV_MAP.items():
        val = source.get(env_key)
        if val is None:
            continue
        if cfg_key in ("enabled", "auto_timestamp"):
            clock[cfg_key] = str(val).lower() not in ("false", "0", "no")
        else:
            clock[cfg_key] = val
    return clock


def _extract_embedding(source: dict) -> dict:
    """Extract a nested embedding dict from flat env keys.

    Only keys present in *source* are set, so partial overrides merge with the
    file-level embedding config instead of replacing it wholesale.
    """
    embedding: dict = {}
    for env_key, cfg_key in _EMBEDDING_ENV_MAP.items():
        val = source.get(env_key)
        if val is None:
            continue
        if cfg_key == "batch_size":
            try:
                embedding[cfg_key] = int(val)
            except (TypeError, ValueError):
                continue
        else:
            embedding[cfg_key] = val
    return embedding


def _build_cfg_from_kvs(kvs: dict) -> dict:
    """Build a config dict from flat key-value pairs (.env or env vars)."""
    cfg = dict(DEFAULTS)
    for cfg_key, env_key in _ENV_MAP.items():
        val = kvs.get(env_key)
        # 统一标准：只认 WEB_PORT。PORT 仅作为历史遗留的 fallback。
        if cfg_key == "web_port" and not val:
            val = kvs.get("PORT")
        if val:
            cfg[cfg_key] = _coerce(cfg_key, val)
    boot = _extract_boot_uris(kvs)
    if boot:
        cfg["boot_uris"] = boot
    clock = _extract_world_clock(kvs)
    if clock:
        cfg["world_clock"] = clock
    embedding = _extract_embedding(kvs)
    if embedding:
        cfg["embedding"] = {**cfg.get("embedding", {}), **embedding}
    return cfg


def _env_overrides() -> dict:
    """Collect per-process overrides from os.environ on top of an existing
    config.json (env wins). Only keys actually present in the environment are
    returned, so a clean environment changes nothing."""
    kvs = dict(os.environ)
    out: dict = {}
    for cfg_key, env_key in _ENV_MAP.items():
        val = kvs.get(env_key)
        if cfg_key == "web_port" and not val:
            val = kvs.get("PORT")
        if val:
            out[cfg_key] = _coerce(cfg_key, val)
    boot = _extract_boot_uris(kvs)
    if boot:
        out["boot_uris"] = boot
    clock = _extract_world_clock(kvs)
    if clock:
        out["world_clock"] = clock
    embedding = _extract_embedding(kvs)
    if embedding:
        out["embedding"] = embedding
    return out



def _migrate_from_dotenv() -> Optional[dict]:
    """One-time migration: read legacy .env and build a config dict.
    Only triggers if .env contains app-level keys (DATABASE_URL, API_TOKEN, etc.),
    not just Docker Compose vars (POSTGRES_USER/PASSWORD/DB)."""
    dotenv_path = ROOT_DIR / ".env"
    if not dotenv_path.exists():
        return None
    try:
        from dotenv import dotenv_values
        env = dotenv_values(dotenv_path)
    except ImportError:
        return None
    if not env:
        return None
    app_keys = set(_ENV_MAP.values()) | {"CORE_MEMORY_URIS"}
    if not any(k in app_keys or k.startswith("CORE_MEMORY_URIS__") for k in env):
        return None
    print(t("config.migrating_dotenv"), file=sys.stderr)
    return _build_cfg_from_kvs(env)


def _migrate_from_env_vars() -> Optional[dict]:
    """Build config from os.environ (Docker first boot). Returns None if nothing relevant found."""
    # Only trigger on Nocturne-specific vars to avoid false positives from
    # common env vars like PORT that exist in many environments.
    strong_signals = {"DATABASE_URL", "API_TOKEN", "VALID_DOMAINS", "CORE_MEMORY_URIS"}
    if not any(k in strong_signals or k.startswith("CORE_MEMORY_URIS__") for k in os.environ):
        return None
    print(t("config.generating_env"), file=sys.stderr)
    return _build_cfg_from_kvs(dict(os.environ))


# ---------------------------------------------------------------------------
# Config loading (cached per-process)
# ---------------------------------------------------------------------------

_cache: Optional[dict] = None

def _load() -> dict:
    global _cache

    if CONFIG_PATH.exists():
        if CONFIG_PATH.is_dir():
            if _IN_DOCKER:
                raise RuntimeError(_docker_setup_hint())
            raise RuntimeError(
                f"{CONFIG_PATH} is a directory, but Nocturne expects a JSON file."
            )

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except json.JSONDecodeError as e:
            if _IN_DOCKER:
                raise RuntimeError(
                    f"Failed to parse config.json: {e}\n\n{_docker_setup_hint()}"
                ) from e
            raise
            
        if _migrate_away_from_demo(_cache):
            try:
                _save_file(_cache)
            except ConfigWriteError as e:
                raise RuntimeError(
                    t("config.db_migrated_not_writable").format(demo_db=_DEMO_DB)
                ) from e

        overrides = _env_overrides()
        if overrides:
            base_clock = _cache.get("world_clock", {}) or {}
            base_embedding = _cache.get("embedding", {}) or {}
            base_boot = _cache.get("boot_uris", {}) or {}
            for key, value in overrides.items():
                if key == "world_clock":
                    _cache["world_clock"] = {**base_clock, **value}
                elif key == "embedding":
                    _cache["embedding"] = {**base_embedding, **value}
                elif key == "boot_uris":
                    # Nested merge like the other dict sections: env overrides
                    # only the keys it provides (e.g. "" via CORE_MEMORY_URIS)
                    # and must NOT wipe per-namespace overrides from config.json.
                    _cache["boot_uris"] = {**base_boot, **value}
                else:
                    _cache[key] = value

        return _cache

    cfg = _migrate_from_dotenv()
    if cfg is None:
        cfg = _migrate_from_env_vars()
    if cfg is None:
        if _IN_DOCKER:
            raise RuntimeError(_docker_setup_hint())
        cfg = dict(DEFAULTS)

    migrated = _migrate_away_from_demo(cfg)
    try:
        _save_file(cfg)
    except ConfigWriteError as e:
        if migrated:
            raise RuntimeError(
                t("config.db_migrated_not_writable").format(demo_db=_DEMO_DB)
            ) from e

    _cache = cfg
    return _cache


def _invalidate():
    global _cache
    _cache = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get(key: str) -> Any:
    """Get a config value. Reads only from config.json.
    
    If key is "database_url" and the value contains "$(NOCTURNE_ROOT)",
    the placeholder is replaced with the NOCTURNE_ROOT environment variable
    (or falls back to the parent of the backend directory).
    """
    val = _load().get(key, DEFAULTS.get(key))
    if key == "database_url" and isinstance(val, str):
        if "$(NOCTURNE_DATA_DIR)" in val:
            data_dir = os.environ.get("NOCTURNE_DATA_DIR") or ""
            if data_dir:
                val = val.replace("$(NOCTURNE_DATA_DIR)", data_dir)
        if "$(NOCTURNE_ROOT)" in val:
            nocturne_root = os.environ.get("NOCTURNE_ROOT") or str(ROOT_DIR)
            val = val.replace("$(NOCTURNE_ROOT)", nocturne_root)
        
        # Dynamic isolation per Run (for Worldlines)
        run_id = os.environ.get("LW_RUN_ID")
        if run_id:
            # Change <slug>.db to <slug>_run-<run_id>.db
            val = val.replace(".db", f"_run-{run_id}.db")
            
    return val


def get_locale() -> str:
    """Get the current locale code, falling back to 'en'."""
    return get("locale") or "en"


def get_boot_uris(namespace: str = "") -> list[str]:
    """Get boot URIs for a namespace."""
    boot = _load().get("boot_uris", {})
    if namespace in boot:
        return boot[namespace]
    if "" in boot:
        return boot[""]
    return []


# Per-request world-clock override (multi-role single-process mode).
# A pi extension may set the active clock mode for the duration of one HTTP
# request (see pi_tools.invoke); when unset, falls back to the process-level
# config.json / env world_clock. Mirrors db.namespace's contextvar pattern.
_clock_override: "contextvars.ContextVar[Optional[dict[str, Any]]]" = contextvars.ContextVar(
    "world_clock_override", default=None
)


def set_world_clock_override(enabled: bool, current_time: Optional[str] = None) -> contextvars.Token:
    """Scope the active world clock to the current async task/request.

    enabled=True -> world-clock mode; reference is *current_time* (falls back
    to the process-level config value when omitted).
    enabled=False -> real-clock mode.
    """
    return _clock_override.set({"enabled": enabled, "current_time": current_time})


def reset_world_clock_override(token: contextvars.Token) -> None:
    _clock_override.reset(token)


def _active_namespace() -> str:
    """Current namespace from the db.namespace contextvar (lazy import avoids cycles)."""
    try:
        from db.namespace import get_namespace
        return get_namespace()
    except Exception:  # noqa: BLE001 - best-effort; fall back to global
        return ""


def get_world_clock(namespace: str = "") -> dict:
    """Get the world-clock config for a namespace.

    Per-namespace overrides live under config `world_clocks[namespace]` (same
    per-namespace pattern as boot_uris). When a namespace has no override, the
    global `world_clock` section is the default, so one backend serves both
    per-world clocks and a shared fallback.
    """
    cfg = _load()
    clocks = cfg.get("world_clocks", {}) or {}
    if namespace and clocks.get(namespace):
        return clocks[namespace]
    return cfg.get("world_clock", {}) or {}


def set_world_clock(clock: dict, namespace: str = "") -> None:
    """Persist the world-clock config for a namespace under world_clocks[namespace]."""
    cfg = _load()
    if "world_clocks" not in cfg:
        cfg["world_clocks"] = {}
    cfg["world_clocks"][namespace] = clock
    _save_file(cfg)
    _invalidate()


def _config_clock_state() -> tuple[bool, Optional[str]]:
    """Resolve (enabled, reference_time) from config: per-namespace, else global."""
    clock = get_world_clock(_active_namespace())
    enabled = clock.get("enabled", True) is not False
    if enabled:
        return True, clock.get("current_time")
    from datetime import datetime
    return False, datetime.now().strftime("%Y-%m-%d")


def get_clock_state() -> tuple[bool, Optional[str]]:
    """Return (enabled, reference_time) for the active clock.

    enabled=True (default): world-clock mode; reference is the per-namespace /
    global world_clock.current_time (None when unset).
    enabled=False: real-clock mode; the world clock is off and the reference
    falls back to today's real date (YYYY-MM-DD).

    Resolution order:
      1. per-request override (set_world_clock_override) — enables one process
         to serve multiple roles (e.g. elias world-clock vs luzhou real-clock);
         an override with enabled=True but no current_time falls back to config.
      2. per-namespace world_clocks[ns] from config.
      3. global world_clock from config.
    """
    ov = _clock_override.get()
    if ov is not None:
        enabled = bool(ov.get("enabled", True))
        if not enabled:
            from datetime import datetime
            return False, datetime.now().strftime("%Y-%m-%d")
        ov_time = ov.get("current_time")
        if ov_time:
            return True, ov_time
        # enabled=True but no explicit reference -> resolve from config
        return _config_clock_state()
    return _config_clock_state()


def get_all_boot_uris() -> dict[str, list[str]]:
    """Get the full boot_uris dict (all namespaces)."""
    return dict(_load().get("boot_uris", {}))


def get_embedding_config() -> dict:
    """Get the embedding (semantic search) config section.

    Empty api_key means semantic search is disabled and search_memory
    degrades to lexical search.  Defaults are always filled in so partial
    env overrides (e.g. only NOCTURNE_EMBEDDING_API_KEY) still yield a
    complete section.
    """
    base = dict(DEFAULTS.get("embedding", {}))
    base.update(_load().get("embedding", {}))
    return base


def set_boot_uris(uris: list[str], namespace: str = "") -> None:
    cfg = _load()
    if "boot_uris" not in cfg:
        cfg["boot_uris"] = {}
    cfg["boot_uris"][namespace] = uris
    _save_file(cfg)
    _invalidate()


def delete_boot_uris(namespace: str) -> bool:
    """Remove a namespace override. Returns True if it existed."""
    cfg = _load()
    boot = cfg.get("boot_uris", {})
    if namespace not in boot:
        return False
    del boot[namespace]
    _save_file(cfg)
    _invalidate()
    return True


def set_value(key: str, value: Any) -> None:
    cfg = _load()
    cfg[key] = value
    _save_file(cfg)
    _invalidate()


def get_all() -> dict:
    """Get all settings for the UI."""
    return dict(_load())


def ensure_config_exists() -> None:
    _load()


