"""Stable integration surface for embedding the quota system in another host."""

from __future__ import annotations

from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import __version__
from .forecast_v2.live_m2 import build_live_m2, compact_m2, quote_m2
from .forecast_v2.live_m3 import build_live_m3
from .forecast_v2.observation_view import build_observation_view


STATIC_ASSETS = (
    "index.html",
    "styles.css",
    "app.js",
    "vendor/echarts.min.js",
    "vendor/ECHARTS-LICENSE.txt",
    "vendor/LICENSE-d3",
)


def bundled_bootstrap_path() -> Path:
    """Return the installed safe bootstrap reference asset."""

    resource = files("codex_quota_dashboard").joinpath("data/bootstrap-reference-v1.json")
    path = Path(str(resource))
    if not path.is_file():
        raise FileNotFoundError("bundled bootstrap reference is unavailable")
    return path


def static_asset_root() -> Any:
    """Return the package resource root containing the shared Web interface."""

    return files("codex_quota_dashboard").joinpath("static")


def static_asset_manifest() -> dict[str, Any]:
    """Return deterministic hashes for every public Web asset."""

    root = static_asset_root()
    asset_hashes = {
        relative: sha256(root.joinpath(relative).read_bytes()).hexdigest()
        for relative in STATIC_ASSETS
    }
    identity = sha256(
        "\n".join(f"{name}:{asset_hashes[name]}" for name in STATIC_ASSETS).encode("utf-8")
    ).hexdigest()
    return {
        "package_version": __version__,
        "identity_sha256": identity,
        "files": asset_hashes,
    }


def copy_static_assets(output_dir: str | Path) -> dict[str, Any]:
    """Copy the canonical interface into a host's static output directory."""

    destination = Path(output_dir)
    root = static_asset_root()
    for relative in STATIC_ASSETS:
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".next")
        temporary.write_bytes(root.joinpath(relative).read_bytes())
        temporary.replace(target)
    return static_asset_manifest()


__all__ = [
    "build_live_m2",
    "build_live_m3",
    "build_observation_view",
    "bundled_bootstrap_path",
    "compact_m2",
    "copy_static_assets",
    "quote_m2",
    "static_asset_manifest",
    "static_asset_root",
]
