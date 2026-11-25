# SPDX-License-Identifier: MIT
"""Bind filesystem paths in generated Cargo vendor configuration."""
from pathlib import Path


def map_local_paths(document, bind):
    """Transform typed Cargo filesystem fields, preserving URLs and source keys."""
    from collections.abc import Mapping

    for source in document.get("source", {}).values():
        for key in ("directory", "local-registry"):
            if key in source:
                source[key] = bind(source[key])
    for patches in document.get("patch", {}).values():
        for dependency in patches.values():
            if isinstance(dependency, Mapping) and "path" in dependency:
                dependency["path"] = bind(dependency["path"])
    if "paths" in document:
        for index, path in enumerate(document["paths"]):
            document["paths"][index] = bind(path)
    return document


def relocate_configuration(text, previous, destination):
    """Relocate vendor-local paths into a private Cargo home."""
    import tomlkit
    from os.path import normpath

    previous, destination = Path(previous).resolve(), Path(destination)

    def relocate(value):
        if not isinstance(value, str):
            return value
        path = Path(normpath(value))
        if path.is_absolute() and path.is_relative_to(previous):
            return str(destination / path.relative_to(previous))
        return value

    return tomlkit.dumps(map_local_paths(tomlkit.parse(text), relocate))


def vendor_configuration(vendor):
    """Bind local source and dependency paths without changing source identities."""
    import tomlkit

    vendor = Path(vendor).resolve()
    document = tomlkit.parse((vendor / ".cargo/config.toml").read_text())

    def bind(path):
        if not isinstance(path, str):
            return path
        if path == "cargo-vendor-dir":
            return str(vendor)
        if path.startswith("cargo-vendor-dir/"):
            return str(vendor / path.removeprefix("cargo-vendor-dir/"))
        return path.replace("@vendor@", str(vendor))

    return tomlkit.dumps(map_local_paths(document, bind))
