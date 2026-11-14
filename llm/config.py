"""Cached access to config/config.yaml, resolved relative to the repository (not the cwd)."""
import functools
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


@functools.lru_cache(maxsize=4)
def _read_config(path: str, mtime_ns: int) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config() -> dict:
    """Return the parsed config. Re-read only when the file's mtime changes.

    Returns {} if the file is missing or unreadable, so callers fall back to their defaults.
    """
    try:
        mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        return _read_config(str(CONFIG_PATH), mtime_ns)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("load_config: could not read %s (%s); using defaults", CONFIG_PATH, exc)
        return {}


def get_section(name: str) -> dict:
    """Return one top-level section of the config as a dict ({} if absent)."""
    section = load_config().get(name)
    return section if isinstance(section, dict) else {}
