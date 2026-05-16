#!/usr/bin/env python3
"""Package this AstrBot plugin into an installable zip archive."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - PyYAML exists in AstrBot envs.
    raise SystemExit("PyYAML is required to read metadata.yaml") from exc


ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"

PACKAGE_FILES = [
    "main.py",
    "client.py",
    "image_utils.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
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


def build_archive(output: Path, *, flat: bool) -> Path:
    plugin_name = read_plugin_name()
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
            archive.write(source, archive_name)

    return output


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    output = build_archive(args.output, flat=args.flat)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
