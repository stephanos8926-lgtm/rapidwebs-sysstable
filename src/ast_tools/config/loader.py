import os
import shutil
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


def get_config_dir() -> Path:
    if env_home := os.environ.get("AST_TOOLS_HOME"):
        path = Path(env_home).resolve()
        _validate_safe_path(path)
        return path
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "ast-tools"
    # Use Path.home() only if AST_TOOLS_HOME and XDG_CONFIG_HOME are not set
    return Path.home() / ".ast-tools"


def _validate_safe_path(path: Path) -> None:
    path.resolve()
    if ".." in str(path):
        raise ConfigError(f"Path contains '..' : {path}")
    if not path.is_absolute():
        raise ConfigError(f"Path must be absolute: {path}")


def get_cache_dir() -> Path:
    if env_home := os.environ.get("AST_TOOLS_HOME"):
        return Path(env_home) / "cache"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / "ast-tools"
    # Use config_dir as fallback if XDG_CACHE_HOME is not set
    return get_config_dir() / "cache"


def get_data_dir() -> Path:
    if env_home := os.environ.get("AST_TOOLS_HOME"):
        return Path(env_home) / "data"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "ast-tools"
    # Use config_dir as fallback if XDG_DATA_HOME is not set
    return get_config_dir() / "data"


def ensure_config_dir(config_dir: Path | None = None) -> Path:
    cfg = config_dir or get_config_dir()
    cfg = cfg.resolve()
    # Ensure all necessary subdirectories exist
    for subdir in ["config", "cache/models", "cache/tmp", "logs", "backups"]:
        (cfg / subdir).mkdir(parents=True, exist_ok=True)
    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, val in override.items():
        if key in result:
            if isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = _deep_merge(result[key], val)
            elif type(result[key]) != type(val):
                raise ConfigError(
                    f"Type mismatch for '{key}': expected {type(result[key]).__name__}, got {type(val).__name__}"
                )
            else:
                result[key] = val
        else:
            result[key] = val
    return result


def migrate_legacy() -> bool:
    legacy = Path.home() / ".cache" / "ast-tools"
    if not legacy.exists():
        return False
    target = get_data_dir()
    legacy_db = legacy / "codebase.db"
    if legacy_db.exists():
        target_db = target / "codebase.db"
        if not target_db.exists():
            target_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_db, target_db)
    legacy_models = legacy / "models"
    if legacy_models.exists():
        target_models = target / "models"
        if not target_models.exists():
            shutil.copytree(legacy_models, target_models)
    return True


def load_config(name: str = "tokens") -> dict:
    config_dir = get_config_dir()
    config_path = config_dir / "config" / f"{name}.yaml"
    if not config_path.exists():
        return {{}}
    with open(config_path) as f:
        return yaml.safe_load(f) or {{}}


def write_config(path: Path, data: dict) -> None:
    path = path.resolve()
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False))
        tmp_path.chmod(0o600)
        # Atomically replace the file
        tmp_path.rename(path)
    except Exception as e:
        raise ConfigError(f"Failed to write config to {path}: {e}") from e
    finally:
        # Clean up the temporary file if it still exists
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
