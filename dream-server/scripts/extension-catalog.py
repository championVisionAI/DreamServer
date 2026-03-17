#!/usr/bin/env python3
"""Export Dream Server extension metadata as JSON or Markdown."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


MANIFEST_NAMES = ("manifest.yaml", "manifest.yml", "manifest.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a catalog of Dream Server extensions."
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Dream Server project directory (defaults to this repo).",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Output format (default: json).",
    )
    return parser.parse_args(argv)


def load_document(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix == ".json":
            return json.load(handle)
        return yaml.safe_load(handle)


def find_manifest(service_dir: Path) -> Path | None:
    for name in MANIFEST_NAMES:
        candidate = service_dir / name
        if candidate.exists():
            return candidate
    return None


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def service_enabled(service_dir: Path, compose_file: str) -> str:
    if not compose_file:
        return "always-on"
    enabled = service_dir / compose_file
    disabled = service_dir / f"{compose_file}.disabled"
    if enabled.exists():
        return "enabled"
    if disabled.exists():
        return "disabled"
    return "missing"


def discover_catalog(project_dir: Path) -> dict[str, Any]:
    services_dir = project_dir / "extensions" / "services"
    services: list[dict[str, Any]] = []

    for service_dir in sorted(services_dir.iterdir()):
        if not service_dir.is_dir():
            continue

        manifest_path = find_manifest(service_dir)
        if manifest_path is None:
            continue

        document = load_document(manifest_path)
        if not isinstance(document, dict):
            continue

        service = document.get("service")
        if not isinstance(service, dict):
            continue

        service_id = str(service.get("id") or service_dir.name)
        compose_file = str(service.get("compose_file") or "")
        features = document.get("features") if isinstance(document.get("features"), list) else []

        services.append(
            {
                "id": service_id,
                "name": str(service.get("name") or service_id),
                "category": str(service.get("category") or "optional"),
                "type": str(service.get("type") or "docker"),
                "status": service_enabled(service_dir, compose_file),
                "aliases": as_string_list(service.get("aliases")),
                "depends_on": as_string_list(service.get("depends_on")),
                "gpu_backends": as_string_list(service.get("gpu_backends") or ["amd", "nvidia"]),
                "feature_count": len(features),
                "path": str(service_dir.relative_to(project_dir)),
            }
        )

    counts = Counter(service["category"] for service in services)
    status_counts = Counter(service["status"] for service in services)

    return {
        "summary": {
            "service_count": len(services),
            "categories": dict(sorted(counts.items())),
            "statuses": dict(sorted(status_counts.items())),
        },
        "services": services,
    }


def render_markdown(catalog: dict[str, Any]) -> str:
    summary = catalog["summary"]
    lines = [
        "# Dream Server Extension Catalog",
        "",
        f"- Services: {summary['service_count']}",
        f"- Categories: {json.dumps(summary['categories'], sort_keys=True)}",
        f"- Statuses: {json.dumps(summary['statuses'], sort_keys=True)}",
        "",
        "| ID | Category | Status | Type | Features | Aliases | Depends On |",
        "|---|---|---|---|---:|---|---|",
    ]

    for service in catalog["services"]:
        alias_text = ", ".join(service["aliases"]) or "-"
        depends_text = ", ".join(service["depends_on"]) or "-"
        lines.append(
            "| {id} | {category} | {status} | {type} | {feature_count} | {aliases} | {depends_on} |".format(
                id=service["id"],
                category=service["category"],
                status=service["status"],
                type=service["type"],
                feature_count=service["feature_count"],
                aliases=alias_text,
                depends_on=depends_text,
            )
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir.resolve()
    catalog = discover_catalog(project_dir)

    if args.format == "markdown":
        print(render_markdown(catalog), end="")
    else:
        print(json.dumps(catalog, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
