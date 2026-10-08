"""Compatibility imports for capability artifacts saved before the core rename."""

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys

from core import __path__ as __path__


class _CoreAliasLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return importlib.import_module("core" + spec.name[len("rebuild"):])

    def exec_module(self, module):
        pass


class _CoreAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("rebuild."):
            return None
        core_name = "core" + fullname[len("rebuild"):]
        core_spec = importlib.util.find_spec(core_name)
        if core_spec is None:
            return None
        return importlib.machinery.ModuleSpec(
            fullname,
            _CoreAliasLoader(),
            is_package=core_spec.submodule_search_locations is not None,
        )


if not any(isinstance(finder, _CoreAliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _CoreAliasFinder())
