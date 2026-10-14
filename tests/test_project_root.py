"""Resolver precedence for tools/*.py project root.

Explicit override, no filesystem scan, no silent fallback:
override global > HIMX_CAM_ROOT env > truthful __file__ marker walk,
else panic.
"""

import importlib
import os
import pathlib
import sys

import pytest


def _load_tool(name):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module(name)
    return importlib.reload(mod)


def test_tools_expose_resolver():
    for name in ("make_field_cube", "check_live_mapping", "measure_now"):
        mod = _load_tool(name)
        assert hasattr(mod, "_resolve_project_root"), f"{name} has no resolver"


def test_resolver_prefers_override_global(tmp_path):
    mod = _load_tool("make_field_cube")
    marker = tmp_path / "frame.json"
    marker.write_text("{}")
    (tmp_path / "targeting").mkdir(exist_ok=True)
    (tmp_path / "targeting" / "converter.py").write_text("#")
    mod_globals = {"PROJECT_ROOT_OVERRIDE": str(tmp_path)}
    root = mod._resolve_project_root(_globals=mod_globals, _env={})
    assert pathlib.Path(root) == tmp_path


def test_resolver_prefers_env_over_file(tmp_path, monkeypatch):
    mod = _load_tool("make_field_cube")
    marker = tmp_path / "frame.json"
    marker.write_text("{}")
    (tmp_path / "targeting").mkdir(exist_ok=True)
    (tmp_path / "targeting" / "converter.py").write_text("#")
    monkeypatch.setenv("HIMX_CAM_ROOT", str(tmp_path))
    root = mod._resolve_project_root(_globals={}, _env=dict(os.environ))
    assert pathlib.Path(root) == tmp_path


def test_resolver_falls_back_to_file_location():
    mod = _load_tool("make_field_cube")
    env = {}
    root = mod._resolve_project_root(_globals={}, _env=env)
    assert (pathlib.Path(root) / "frame.json").is_file()
    assert (pathlib.Path(root) / "targeting" / "converter.py").is_file()


def test_resolver_panics_without_any_source(monkeypatch):
    mod = _load_tool("make_field_cube")
    monkeypatch.setattr(mod, "__file__", "/nonexistent/tools/make_field_cube.py")
    monkeypatch.delenv("HIMX_CAM_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="HIMX_CAM_ROOT|PROJECT_ROOT_OVERRIDE"):
        mod._resolve_project_root(_globals={}, _env={})
