#!/usr/bin/env python3
"""Audit extension host ports for collisions and manifest/compose drift."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


PORT_EXPR_WITH_DEFAULT = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::-|-)([0-9]+)\}$")
PORT_EXPR = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass
class PortBinding:
    service_id: str
    source: str
    path: str
    env_var: str = ""
    default_port: int | None = None
    host_port: int | None = None
    raw: str = ""

    def display_port(self) -> str:
        if self.host_port is not None:
            return str(self.host_port)
        if self.env_var and self.default_port is not None:
            return f"${{{self.env_var}:-{self.default_port}}}"
        if self.env_var:
            return f"${{{self.env_var}}}"
        return "?"


@dataclass
class AuditIssue:
    level: str
    code: str
    service_id: str
    message: str
    path: str = ""


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return data


def parse_port_token(token: str) -> tuple[int | None, str, int | None]:
    token = token.strip().strip("'").strip('"')
    token = token.replace('\\${', '${')
    if token.isdigit():
        return int(token), "", None

    match = PORT_EXPR_WITH_DEFAULT.match(token)
    if match:
        return None, match.group(1), int(match.group(2))

    match = PORT_EXPR.match(token)
    if match:
        return None, match.group(1), None

    return None, "", None


def parse_compose_port(value) -> tuple[int | None, str, int | None]:
    if isinstance(value, dict):
        published = value.get("published")
        if isinstance(published, int):
            return published, "", None
        if isinstance(published, str):
            return parse_port_token(published)
        return None, "", None

    if not isinstance(value, str):
        return None, "", None

    token = value.strip().strip("'").strip('"')
    token = token.replace('\\${', '${')
    expr_match = re.search(r"\$\{[^}]+\}", token)
    if expr_match:
        return parse_port_token(expr_match.group(0))

    if ":" not in token:
        return parse_port_token(token)

    parts = token.rsplit(":", 2)
    if len(parts) == 3:
        return parse_port_token(parts[1])

    parts = token.rsplit(":", 1)
    if len(parts) == 2:
        return parse_port_token(parts[0])

    return parse_port_token(token)


def build_manifest_binding(service_id: str, manifest_path: Path, service: dict) -> PortBinding | None:
    env_var = str(service.get("external_port_env", "")).strip()
    default_port = service.get("external_port_default", service.get("port"))
    if env_var and not ENV_KEY.match(env_var):
        raise ValueError(f"invalid external_port_env: {env_var}")
    if default_port is not None and not isinstance(default_port, int):
        raise ValueError(f"external_port_default must be an integer: {default_port}")
    if not env_var and default_port in (None, 0):
        return None
    return PortBinding(
        service_id=service_id,
        source="manifest",
        path=str(manifest_path),
        env_var=env_var,
        default_port=default_port,
        host_port=default_port if isinstance(default_port, int) and default_port > 0 else None,
        raw="external_port_default",
    )


def collect_bindings(root: Path) -> tuple[list[PortBinding], list[AuditIssue]]:
    bindings: list[PortBinding] = []
    issues: list[AuditIssue] = []
    ext_dir = root / "extensions" / "services"

    for service_dir in sorted(ext_dir.iterdir()):
        if not service_dir.is_dir():
            continue

        manifest_path = service_dir / "manifest.yaml"
        if not manifest_path.exists():
            issues.append(
                AuditIssue("warning", "manifest.missing", service_dir.name, "manifest.yaml not found", str(service_dir))
            )
            continue

        try:
            manifest = load_yaml(manifest_path)
        except Exception as exc:
            issues.append(
                AuditIssue("error", "manifest.invalid", service_dir.name, f"Could not parse manifest: {exc}", str(manifest_path))
            )
            continue

        service = manifest.get("service")
        if not isinstance(service, dict):
            issues.append(
                AuditIssue("error", "manifest.service_missing", service_dir.name, "Manifest must contain a service object", str(manifest_path))
            )
            continue

        service_id = str(service.get("id", service_dir.name)).strip() or service_dir.name
        manifest_binding: PortBinding | None = None

        try:
            manifest_binding = build_manifest_binding(service_id, manifest_path, service)
            if manifest_binding:
                bindings.append(manifest_binding)
        except ValueError as exc:
            issues.append(AuditIssue("error", "manifest.port_invalid", service_id, str(exc), str(manifest_path)))

        compose_ref = str(service.get("compose_file", "")).strip()
        if not compose_ref:
            continue

        compose_path = service_dir / compose_ref
        if not compose_path.exists():
            issues.append(AuditIssue("error", "compose.missing", service_id, f"compose_file not found: {compose_ref}", str(compose_path)))
            continue

        try:
            compose = load_yaml(compose_path)
        except Exception as exc:
            issues.append(AuditIssue("error", "compose.invalid", service_id, f"Could not parse compose file: {exc}", str(compose_path)))
            continue

        services = compose.get("services", {})
        if not isinstance(services, dict):
            issues.append(AuditIssue("error", "compose.services_invalid", service_id, "Compose services must be a map", str(compose_path)))
            continue

        for compose_service, definition in services.items():
            if not isinstance(definition, dict):
                continue
            for item in definition.get("ports", []) or []:
                host_port, env_var, default_port = parse_compose_port(item)
                bindings.append(
                    PortBinding(
                        service_id=service_id,
                        source=f"compose:{compose_service}",
                        path=str(compose_path),
                        env_var=env_var,
                        default_port=default_port,
                        host_port=host_port,
                        raw=str(item),
                    )
                )

                observed_port = host_port or default_port
                if (
                    manifest_binding
                    and manifest_binding.default_port
                    and observed_port
                    and manifest_binding.default_port != observed_port
                ):
                    issues.append(
                        AuditIssue(
                            "error",
                            "compose.port_mismatch",
                            service_id,
                            f"Manifest port {manifest_binding.default_port} disagrees with compose port {observed_port}",
                            str(compose_path),
                        )
                    )
                if manifest_binding and manifest_binding.env_var and env_var and manifest_binding.env_var != env_var:
                    issues.append(
                        AuditIssue(
                            "warning",
                            "compose.env_var_mismatch",
                            service_id,
                            f"Manifest env var {manifest_binding.env_var} disagrees with compose env var {env_var}",
                            str(compose_path),
                        )
                    )

    return bindings, issues


def detect_collisions(bindings: list[PortBinding]) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    by_port: dict[int, list[PortBinding]] = defaultdict(list)

    for binding in bindings:
        port = binding.host_port or binding.default_port
        if port:
            by_port[port].append(binding)

    for port, owners in sorted(by_port.items()):
        service_ids = sorted({binding.service_id for binding in owners})
        if len(service_ids) < 2:
            continue
        issues.append(
            AuditIssue(
                "error",
                "port.collision",
                ",".join(service_ids),
                f"Host port {port} is declared by multiple services: {', '.join(service_ids)}",
            )
        )

    return issues


def render_text(bindings: list[PortBinding], issues: list[AuditIssue]) -> None:
    print("Dream Server Extension Port Audit")
    print("")
    print(f"{'SERVICE':<22} {'SOURCE':<20} {'HOST PORT':<18} PATH")
    print("-" * 88)
    for binding in sorted(bindings, key=lambda item: (item.service_id, item.source, item.display_port())):
        print(f"{binding.service_id:<22} {binding.source:<20} {binding.display_port():<18} {binding.path}")

    print("")
    if not issues:
        print("No audit issues found.")
        return

    for issue in issues:
        prefix = f"[{issue.level.upper()}] {issue.code}"
        suffix = f" ({issue.service_id})" if issue.service_id else ""
        print(f"{prefix}{suffix}")
        print(f"  {issue.message}")
        if issue.path:
            print(f"  path: {issue.path}")


def render_json(bindings: list[PortBinding], issues: list[AuditIssue]) -> None:
    print(
        json.dumps(
            {
                "bindings": [asdict(binding) for binding in bindings],
                "issues": [asdict(issue) for issue in issues],
            },
            indent=2,
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit extension host port declarations.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    bindings, issues = collect_bindings(root)
    issues.extend(detect_collisions(bindings))

    if args.format == "json":
        render_json(bindings, issues)
    else:
        render_text(bindings, issues)

    return 1 if any(issue.level == "error" for issue in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
