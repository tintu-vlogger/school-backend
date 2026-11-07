"""Business modules. Each folder is one module with models.py, schemas.py, service.py,
router.py and optionally hooks.py.

`import_all_models()` imports every module's models so SQLAlchemy and Alembic see all
tables; `all_routers()` returns every module's `router` (and `extra_routers`) so the app
mounts them under /api/v1 without a hand-written list. `root_routers()` returns routers a
module wants at the server root instead (`root_routers` in its router.py), for callers with
fixed paths such as attendance devices (/iclock/...).
"""
import importlib
import pkgutil
from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent


def module_names() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules([str(_PACKAGE_DIR)]) if m.ispkg)


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise


def import_all_models() -> None:
    import core.models  # noqa: F401  (tables owned by the platform core)
    for name in module_names():
        _try_import(f"modules.{name}.models")


def import_all_hooks() -> None:
    for name in module_names():
        _try_import(f"modules.{name}.hooks")


def all_routers() -> list:
    out = []
    for name in module_names():
        mod = _try_import(f"modules.{name}.router")
        if mod is None:
            continue
        if getattr(mod, "router", None) is not None:
            out.append(mod.router)
        out.extend(getattr(mod, "extra_routers", []) or [])
    return out


def root_routers() -> list:
    out = []
    for name in module_names():
        mod = _try_import(f"modules.{name}.router")
        if mod is not None:
            out.extend(getattr(mod, "root_routers", []) or [])
    return out
