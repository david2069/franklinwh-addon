#!/usr/bin/env python3
"""Check that every compiled dependency has a musllinux wheel for each
architecture the add-on claims to support.

The add-on base image is Alpine, so musl — manylinux wheels do not apply, and
the Dockerfile installs no toolchain (no build-base, no python3-dev, no cargo).
A dependency without a musllinux wheel for the target architecture therefore
fails the `pip3 install` layer outright.

This is not hypothetical: config.yaml declared armhf, armv7, aarch64, amd64 and
i386, and eleven packages had no musllinux wheel for three of them. It had
never surfaced because only amd64 had ever been built.

Run it after changing requirements.txt or widening `arch:`:

    python3 scripts/check_addon_wheels.py

Exits non-zero if any declared architecture is unbuildable.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "franklinwh_ha_integrator" / "config.yaml"

# HA add-on arch -> the platform fragment its wheels carry.
# armhf and armv7 are both 32-bit ARM and share the armv7l wheel tag.
ARCH_TAG = {
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "armv7": "armv7l",
    "armhf": "armv7l",
    "i386": "i686",
}

TIMEOUT = 30


def declared_arches(config: Path) -> list[str]:
    import yaml

    return list(yaml.safe_load(config.read_text())["arch"])


def installed_distributions() -> dict[str, str]:
    """name -> version for everything importable in this environment.

    Run this where requirements.txt was installed — the add-on image, or the
    compose container. A developer venv carries extras the add-on never
    installs (playwright, for one), and reporting those as build blockers sends
    you chasing a dependency that is not in the image.
    """
    import importlib.metadata as md

    out = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name:
            out[name] = dist.version
    return out


def wheel_filenames(package: str, version: str) -> list[str]:
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return [f["filename"] for f in json.load(resp)["urls"]]


def is_pure(filenames: list[str]) -> bool:
    """Any `none-any` wheel is portable. Matching only `py3-none-any` missed
    the `py2.py3-none-any` universal tag, which reported passlib, six and
    python-dateutil as needing compilation on every architecture."""
    return any(n.endswith("-none-any.whl") for n in filenames)


def has_musl_wheel(filenames: list[str], tag: str) -> bool:
    return any("musllinux" in n and tag in n for n in filenames)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    # --config exists so this can run inside the image, where only the app is
    # mounted and the repo checkout is not.
    parser.add_argument("--config", type=Path, default=CONFIG,
                        help="path to the add-on config.yaml")
    args = parser.parse_args(argv)

    arches = declared_arches(args.config)
    print(f"config.yaml declares: {', '.join(arches)}\n")

    unknown = [a for a in arches if a not in ARCH_TAG]
    if unknown:
        print(f"!! unknown architecture(s): {', '.join(unknown)}")
        return 2

    missing: dict[str, list[str]] = {a: [] for a in arches}
    unchecked: list[str] = []

    for name, version in sorted(installed_distributions().items()):
        try:
            files = wheel_filenames(name, version)
        except Exception as exc:
            unchecked.append(f"{name}=={version} ({exc})")
            continue
        if not files or is_pure(files):
            continue
        for arch in arches:
            if not has_musl_wheel(files, ARCH_TAG[arch]):
                missing[arch].append(f"{name}=={version}")

    ok = True
    for arch in arches:
        pkgs = missing[arch]
        if pkgs:
            ok = False
            print(f"{arch}: {len(pkgs)} package(s) with no musllinux wheel — "
                  f"pip will try to compile, and no toolchain is installed:")
            for pkg in pkgs:
                print(f"    {pkg}")
        else:
            print(f"{arch}: OK — every compiled dependency has a musllinux wheel")

    if unchecked:
        print(f"\nnot checked ({len(unchecked)}) — local or unpublished builds:")
        for item in unchecked[:10]:
            print(f"    {item}")

    if not ok:
        print("\nEither narrow `arch:` in config.yaml, or add the toolchain those "
              "packages need (several are Rust, so cargo and rustup, not just gcc).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
