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


def read_plugin_name() -> str:
    metadata_path = ROOT / "metadata.yaml"
    with metadata_path.open("r", encoding="utf-8") as file:
        metadata = yaml.safe_load(file)
    if not isinstance(metadata, dict):
        raise ValueError("metadata.yaml must contain a YAML object")
    plugin_name = metadata.get("name")
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        raise ValueError("metadata.yaml must define a non-empty name")
    return plugin_name.strip()


def build_archive(output: Path, *, include_root_dir: bool) -> Path:
    plugin_name = read_plugin_name()
    output.parent.mkdir(parents=True, exist_ok=True)

    missing = [path for path in PACKAGE_FILES if not (ROOT / path).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing package file(s): {', '.join(missing)}")

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in PACKAGE_FILES:
            source = ROOT / relative
            archive_name = f"{plugin_name}/{relative}" if include_root_dir else relative
            archive.write(source, archive_name)

    return output


def parse_args(argv: list[str]) -> argparse.Namespace:
    plugin_name = read_plugin_name()
    default_output = DIST_DIR / f"{plugin_name}.zip"
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
        "--include-root-dir",
        action="store_true",
        help=(
            "Put files under a top-level directory named after metadata.name. "
            "AstrBot supports both flat and rooted archives."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    output = build_archive(args.output, include_root_dir=args.include_root_dir)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
