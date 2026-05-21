#!/usr/bin/env python3
"""Package this AstrBot plugin into an installable zip archive."""

from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - PyYAML exists in AstrBot envs.
    raise SystemExit("PyYAML is required to read metadata.yaml") from exc


ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"

PACKAGE_FILES = [
    "main.py",
    "card_renderer.py",
    "client.py",
    "config_redact.py",
    "image_utils.py",
    "plan.py",
    "text_image.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    "LICENSE",
]


def read_metadata() -> dict:
    metadata_path = ROOT / "metadata.yaml"
    with metadata_path.open("r", encoding="utf-8") as file:
        metadata = yaml.safe_load(file)
    if not isinstance(metadata, dict):
        raise ValueError("metadata.yaml must contain a YAML object")
    return metadata


def read_plugin_name() -> str:
    metadata = read_metadata()
    plugin_name = metadata.get("name")
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        raise ValueError("metadata.yaml must define a non-empty name")
    return plugin_name.strip()


def read_plugin_version() -> str:
    metadata = read_metadata()
    version = metadata.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("metadata.yaml must define a non-empty version")
    return version.strip()


def build_archive(
    output: Path, *, flat: bool, package_version: str | None = None
) -> Path:
    plugin_name = read_plugin_name()
    source_version = read_plugin_version()
    package_version = package_version or source_version
    patch_versions = package_version != source_version
    output.parent.mkdir(parents=True, exist_ok=True)

    missing = [path for path in PACKAGE_FILES if not (ROOT / path).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing package file(s): {', '.join(missing)}")

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        if not flat:
            # AstrBot v4.24.2 WebUI upload installation treats the first zip entry
            # as the extracted root directory, so keep an explicit directory entry
            # before any file entries.
            archive.writestr(f"{plugin_name}/", "")

        for relative in PACKAGE_FILES:
            source = ROOT / relative
            archive_name = relative if flat else f"{plugin_name}/{relative}"
            content = (
                _patched_file_content(relative, package_version)
                if patch_versions
                else None
            )
            if content is None:
                archive.write(source, archive_name)
            else:
                archive.writestr(archive_name, content)

    return output


def _patched_file_content(relative: str, package_version: str) -> str | None:
    """Patch version-bearing files inside the zip without touching the workspace."""
    source = ROOT / relative
    if relative == "metadata.yaml":
        metadata = read_metadata()
        metadata["version"] = package_version
        return yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)

    if relative == "main.py":
        text = source.read_text(encoding="utf-8")
        source_version = read_plugin_version().removeprefix("v")
        target_version = package_version.removeprefix("v")
        old = f'"{source_version}"'
        new = f'"{target_version}"'
        if old not in text:
            raise ValueError(
                f"Cannot patch plugin version in {relative}: {old} not found"
            )
        return text.replace(old, new, 1)

    if relative == "pyproject.toml":
        text = source.read_text(encoding="utf-8")
        source_version = read_plugin_version().removeprefix("v")
        target_version = package_version.removeprefix("v")
        old = f'version = "{source_version}"'
        new = f'version = "{target_version}"'
        if old not in text:
            raise ValueError(
                f"Cannot patch project version in {relative}: {old} not found"
            )
        return text.replace(old, new, 1)

    return None


def build_dev_version(base_version: str) -> str:
    """Return a SemVer-compatible temporary version based on local time.

    Use `test` instead of `dev` because AstrBot's current version comparator
    strips all `v` characters before comparing versions.
    """
    stamp = datetime.now().strftime("%Y%m%d.%H%M")
    return f"{base_version}-test.{stamp}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    plugin_name = read_plugin_name()
    plugin_version = read_plugin_version()
    default_output = DIST_DIR / f"{plugin_name}-{plugin_version}.zip"
    parser = argparse.ArgumentParser(
        description="Package the AstrBot GPT Image2 plugin into a zip archive.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_output,
        help=f"Output zip path. Defaults to {default_output}",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help=(
            "Build a legacy flat archive without the top-level plugin directory. "
            "Do not use this for AstrBot WebUI upload installation on v4.24.2."
        ),
    )
    parser.add_argument(
        "--dev-version",
        action="store_true",
        help=(
            "Build a temporary test package with a SemVer prerelease timestamp. "
            "Zip-internal version-bearing files are patched, but workspace files "
            "are not modified."
        ),
    )
    parser.add_argument(
        "--package-version",
        type=str,
        default=None,
        help=(
            "Override the version written into the zip package, e.g. "
            "v0.4.0-test.20260517.1548. Workspace files are not modified."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    package_version = args.package_version
    if args.dev_version:
        if package_version:
            raise ValueError(
                "--dev-version and --package-version cannot be used together"
            )
        package_version = build_dev_version(read_plugin_version())

    if (
        package_version
        and args.output
        == DIST_DIR / f"{read_plugin_name()}-{read_plugin_version()}.zip"
    ):
        args.output = DIST_DIR / f"{read_plugin_name()}-{package_version}.zip"

    output = build_archive(args.output, flat=args.flat, package_version=package_version)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
