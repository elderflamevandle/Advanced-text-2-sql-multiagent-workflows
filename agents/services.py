"""Access to the schema catalog and query memory services for graph nodes and the API.

``catalog_service()`` and ``memory_service()`` return the service object, or ``None`` when
the module is not installed, is still an interface stub, or cannot be built. Callers treat
``None`` as "feature unavailable" and keep their previous behaviour.

``api.main.create_app`` injects its collaborators here (the same way it calls
``database.registry.set_registry``); tests inject fakes with the ``set_*_for_tests``
functions and undo that with ``reset_services_for_tests``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_UNSET = object()
_catalog_override = _UNSET
_memory_override = _UNSET


def catalog_service():
    """The catalog service (catalog.service.CatalogService), or None when unavailable."""
    if _catalog_override is not _UNSET:
        return _catalog_override
    try:
        from catalog.service import get_catalog_service

        return get_catalog_service()
    except (ImportError, NotImplementedError):
        return None
    except Exception as exc:  # noqa: BLE001 - a broken catalog must never break a query
        logger.warning("Schema catalog service unavailable: %s", type(exc).__name__)
        return None


def memory_service():
    """The query memory service (memory.service.MemoryService), or None when unavailable."""
    if _memory_override is not _UNSET:
        return _memory_override
    try:
        from memory.service import get_memory_service

        return get_memory_service()
    except (ImportError, NotImplementedError):
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Query memory service unavailable: %s", type(exc).__name__)
        return None


def use_catalog_service(service) -> None:
    """Use ``service`` instead of the real singleton. ``None`` means "no catalog"."""
    global _catalog_override
    _catalog_override = service


def use_memory_service(service) -> None:
    """Use ``service`` instead of the real singleton. ``None`` means "no memory"."""
    global _memory_override
    _memory_override = service


set_catalog_service_for_tests = use_catalog_service
set_memory_service_for_tests = use_memory_service


def reset_services_for_tests() -> None:
    """Drop both overrides: the real singletons are resolved again on next use."""
    global _catalog_override, _memory_override
    _catalog_override = _UNSET
    _memory_override = _UNSET
