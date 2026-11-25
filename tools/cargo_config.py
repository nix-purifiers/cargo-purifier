# SPDX-License-Identifier: MIT
"""Bind filesystem paths in generated Cargo vendor configuration."""
from pathlib import Path


def vendor_configuration(vendor):
    """Bind local source and dependency paths without changing source identities."""
    import tomlkit
    from collections.abc import Mapping

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
    return tomlkit.dumps(document)
