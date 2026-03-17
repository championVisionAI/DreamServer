#!/usr/bin/env python3
"""
Dream Server Extension Governance Toolkit.

Purpose:
  - Detect extension integration issues early (before docker compose runs)
  - Enforce manifest/compose quality gates for large extension libraries
  - Provide visibility into backend compatibility and port allocations

Commands:
  audit   Validate manifests, dependencies, compose files, and backend support
  matrix  Print backend compatibility matrix for all discovered services
  ports   Print host-port allocations and detect collisions
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    yaml = None


SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
KNOWN_GPU_BACKENDS = {"amd", "nvidia", "apple", "none", "all"}
KNOWN_CATEGORIES = {"core", "recommended", "optional"}
SERVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    subject: str = ""
    path: str = ""
    hint: str = ""

    def to_json(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class PortBinding:
    service_id: str
    source: str
    path: str
    host_port: int | None = None
    env_var: str = ""
    default_port: int | None = None
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
class ExtensionRecord:
    service_id: str
    name: str
    directory: str
    manifest_path: Path
    schema_version: str
    category: str
    aliases: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    container_name: str = ""
    health: str = ""
    gpu_backends: set[str] = field(default_factory=set)
    compose_file: str = ""
    compose_path: Path | None = None
    external_port_default: int | None = None
    external_port_env: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.compose_path and self.compose_path.exists())

    def supports(self, backend: str) -> bool:
        if "all" in self.gpu_backends:
            return True
        if "none" in self.gpu_backends:
            return True
        return backend in self.gpu_backends


def read_yaml_or_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8")), ""
        if yaml is None:
            return None, "PyYAML is required to parse YAML manifests"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            return {}, ""
        if not isinstance(data, dict):
            return None, "manifest root must be an object"
        return data, ""
    except Exception as exc:  # pragma: no cover - broad parse protection
        return None, str(exc)


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    result.append(text)
        return result
    return []


def parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def is_valid_port(port: int | None) -> bool:
    if port is None:
        return False
    return 1 <= port <= 65535


def parse_port_token(token: str) -> tuple[int | None, str, int | None]:
    """
    Parse a host port token.

    Supported examples:
      3000
      ${WEBUI_PORT:-3000}
      ${WEBUI_PORT}
    Returns:
      (literal_port, env_var, env_default_port)
    """
    token = token.strip().strip('"').strip("'")
    if not token:
        return None, "", None

    if token.isdigit():
        return int(token), "", None

    env_with_default = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*):-([0-9]+)\}", token)
    if env_with_default:
        env_var = env_with_default.group(1)
        default_text = env_with_default.group(2)
        default_port = int(default_text) if default_text.isdigit() else None
        return None, env_var, default_port

    env_only = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*)\}", token)
    if env_only:
        return None, env_only.group(1), None

    return None, "", None


def parse_compose_ports(value: Any) -> list[tuple[int | None, str, int | None, str]]:
    """
    Returns tuples:
      (literal_port, env_var, env_default_port, raw)
    """
    bindings: list[tuple[int | None, str, int | None, str]] = []
    if not isinstance(value, list):
        return bindings

    for item in value:
        raw = str(item)
        if isinstance(item, dict):
            published = item.get("published")
            literal = parse_optional_int(published)
            env_var = ""
            default_port = None
            if literal is None and isinstance(published, str):
                literal, env_var, default_port = parse_port_token(published)
            bindings.append((literal, env_var, default_port, raw))
            continue

        if not isinstance(item, str):
            bindings.append((None, "", None, raw))
            continue

        token = item.strip()
        # Prefer the host port token from "127.0.0.1:${WEBUI_PORT:-3000}:8080"
        parts = token.split(":")
        host_token = ""
        if len(parts) >= 3:
            host_token = parts[-2]
        elif len(parts) == 2:
            host_token = parts[0]
        else:
            host_token = parts[0]
        literal, env_var, default_port = parse_port_token(host_token)
        bindings.append((literal, env_var, default_port, raw))
    return bindings


class GovernanceToolkit:
    def __init__(self, root: Path, gpu_backend: str):
        self.root = root
        self.extensions_dir = self.root / "extensions" / "services"
        self.gpu_backend = gpu_backend
        self.records: list[ExtensionRecord] = []
        self.issues: list[Issue] = []
        self.port_bindings: list[PortBinding] = []

    def add_issue(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        subject: str = "",
        path: str = "",
        hint: str = "",
    ) -> None:
        self.issues.append(
            Issue(severity=severity, code=code, message=message, subject=subject, path=path, hint=hint)
        )

    def discover_manifests(self) -> list[Path]:
        if not self.extensions_dir.exists():
            self.add_issue(
                "error",
                "extensions.missing_dir",
                f"extensions/services directory not found: {self.extensions_dir}",
                path=str(self.extensions_dir),
                hint="Run from dream-server root or pass --root.",
            )
            return []

        manifests: list[Path] = []
        for service_dir in sorted(self.extensions_dir.iterdir()):
            if not service_dir.is_dir():
                continue
            for name in ("manifest.yaml", "manifest.yml", "manifest.json"):
                candidate = service_dir / name
                if candidate.exists():
                    manifests.append(candidate)
                    break
            else:
                self.add_issue(
                    "warning",
                    "manifest.missing",
                    "Service directory has no manifest file.",
                    subject=service_dir.name,
                    path=str(service_dir),
                )
        return manifests

    def load(self) -> None:
        manifests = self.discover_manifests()
        for manifest_path in manifests:
            data, err = read_yaml_or_json(manifest_path)
            service_dir = manifest_path.parent
            if data is None:
                self.add_issue(
                    "error",
                    "manifest.parse_error",
                    f"Could not parse manifest: {err}",
                    subject=service_dir.name,
                    path=str(manifest_path),
                )
                continue

            schema_version = str(data.get("schema_version", ""))
            service = data.get("service")
            if not isinstance(service, dict):
                self.add_issue(
                    "error",
                    "manifest.service_missing",
                    "Manifest must contain a service object.",
                    subject=service_dir.name,
                    path=str(manifest_path),
                )
                continue

            service_id = str(service.get("id", "")).strip()
            if not service_id:
                self.add_issue(
                    "error",
                    "service.id_missing",
                    "service.id is required.",
                    subject=service_dir.name,
                    path=str(manifest_path),
                )
                continue

            name = str(service.get("name", service_id)).strip() or service_id
            category = str(service.get("category", "optional")).strip()
            aliases = normalize_string_list(service.get("aliases"))
            depends_on = normalize_string_list(service.get("depends_on"))
            container_name = str(service.get("container_name", f"dream-{service_id}")).strip()
            health = str(service.get("health", "/health")).strip()
            compose_file = str(service.get("compose_file", "")).strip()
            external_port_default = parse_optional_int(
                service.get("external_port_default", service.get("port"))
            )
            external_port_env = str(service.get("external_port_env", "")).strip()
            gpu_backends = set(normalize_string_list(service.get("gpu_backends")))
            if not gpu_backends:
                gpu_backends = {"amd", "nvidia"}

            compose_path = service_dir / compose_file if compose_file else None
            record = ExtensionRecord(
                service_id=service_id,
                name=name,
                directory=service_dir.name,
                manifest_path=manifest_path,
                schema_version=schema_version,
                category=category,
                aliases=aliases,
                depends_on=depends_on,
                container_name=container_name,
                health=health,
                gpu_backends=gpu_backends,
                compose_file=compose_file,
                compose_path=compose_path,
                external_port_default=external_port_default,
                external_port_env=external_port_env,
            )
            self.records.append(record)
            self.validate_record(record)

        self.validate_uniqueness()
        self.validate_dependencies()
        self.validate_compose_files()
        self.validate_backend_dependency_compatibility()
        self.collect_manifest_ports()

    def validate_record(self, record: ExtensionRecord) -> None:
        manifest_path = str(record.manifest_path)
        sid = record.service_id

        if record.schema_version != "dream.services.v1":
            self.add_issue(
                "error",
                "manifest.schema_version",
                f"schema_version must be dream.services.v1 (got {record.schema_version or '<empty>'}).",
                subject=sid,
                path=manifest_path,
            )

        if not SERVICE_ID_RE.match(sid):
            self.add_issue(
                "error",
                "service.id_format",
                "service.id must use lowercase letters, digits, and dashes.",
                subject=sid,
                path=manifest_path,
            )

        if record.directory != sid:
            self.add_issue(
                "warning",
                "service.id_dir_mismatch",
                f"Directory name ({record.directory}) does not match service.id ({sid}).",
                subject=sid,
                path=manifest_path,
                hint="Keep directory and service.id aligned for tooling consistency.",
            )

        if record.category not in KNOWN_CATEGORIES:
            self.add_issue(
                "error",
                "service.category_invalid",
                f"Unknown category: {record.category}",
                subject=sid,
                path=manifest_path,
                hint=f"Use one of: {', '.join(sorted(KNOWN_CATEGORIES))}.",
            )

        if not record.container_name:
            self.add_issue(
                "error",
                "service.container_name_missing",
                "service.container_name is required.",
                subject=sid,
                path=manifest_path,
            )

        if record.health and not record.health.startswith("/"):
            self.add_issue(
                "warning",
                "service.health_path",
                f"Health endpoint should start with '/': {record.health}",
                subject=sid,
                path=manifest_path,
            )

        for alias in record.aliases:
            if not ALIAS_RE.match(alias):
                self.add_issue(
                    "error",
                    "service.alias_format",
                    f"Invalid alias: {alias}",
                    subject=sid,
                    path=manifest_path,
                )
            if alias == sid:
                self.add_issue(
                    "warning",
                    "service.alias_redundant",
                    f"Alias duplicates service id: {alias}",
                    subject=sid,
                    path=manifest_path,
                )

        unknown_backends = sorted(b for b in record.gpu_backends if b not in KNOWN_GPU_BACKENDS)
        if unknown_backends:
            self.add_issue(
                "error",
                "service.backend_invalid",
                f"Unknown gpu_backends values: {', '.join(unknown_backends)}",
                subject=sid,
                path=manifest_path,
                hint=f"Use values from: {', '.join(sorted(KNOWN_GPU_BACKENDS))}.",
            )

        if record.external_port_env and not ENV_KEY_RE.match(record.external_port_env):
            self.add_issue(
                "error",
                "service.external_port_env_invalid",
                f"Invalid external_port_env: {record.external_port_env}",
                subject=sid,
                path=manifest_path,
            )

        if record.external_port_default is not None and not is_valid_port(record.external_port_default):
            self.add_issue(
                "error",
                "service.external_port_default_invalid",
                f"Invalid external_port_default: {record.external_port_default}",
                subject=sid,
                path=manifest_path,
            )

        if record.category != "core" and not record.compose_file:
            self.add_issue(
                "error",
                "compose.file_missing",
                "Non-core services must declare service.compose_file.",
                subject=sid,
                path=manifest_path,
            )

    def validate_uniqueness(self) -> None:
        id_seen: dict[str, ExtensionRecord] = {}
        container_seen: dict[str, ExtensionRecord] = {}
        alias_owner: dict[str, ExtensionRecord] = {}

        for record in self.records:
            sid = record.service_id
            if sid in id_seen:
                self.add_issue(
                    "error",
                    "service.id_duplicate",
                    f"Duplicate service.id found in {record.manifest_path} and {id_seen[sid].manifest_path}",
                    subject=sid,
                    path=str(record.manifest_path),
                )
            else:
                id_seen[sid] = record

            cname = record.container_name
            if cname:
                if cname in container_seen and container_seen[cname].service_id != sid:
                    self.add_issue(
                        "error",
                        "service.container_name_duplicate",
                        f"container_name collision with {container_seen[cname].service_id}: {cname}",
                        subject=sid,
                        path=str(record.manifest_path),
                    )
                else:
                    container_seen[cname] = record

            for alias in record.aliases:
                prior = alias_owner.get(alias)
                if prior and prior.service_id != sid:
                    self.add_issue(
                        "error",
                        "service.alias_collision",
                        f"Alias '{alias}' is used by both {prior.service_id} and {sid}",
                        subject=sid,
                        path=str(record.manifest_path),
                    )
                else:
                    alias_owner[alias] = record

    def validate_dependencies(self) -> None:
        by_id = {record.service_id: record for record in self.records}

        for record in self.records:
            for dep in record.depends_on:
                if dep not in by_id:
                    self.add_issue(
                        "error",
                        "service.dep_missing",
                        f"Dependency '{dep}' not found.",
                        subject=record.service_id,
                        path=str(record.manifest_path),
                    )
                elif dep == record.service_id:
                    self.add_issue(
                        "error",
                        "service.dep_self",
                        "Service cannot depend on itself.",
                        subject=record.service_id,
                        path=str(record.manifest_path),
                    )

        # Cycle detection with DFS coloring
        graph: dict[str, list[str]] = {r.service_id: list(r.depends_on) for r in self.records}
        color: dict[str, int] = {sid: 0 for sid in graph}  # 0=unvisited,1=visiting,2=done
        stack: list[str] = []

        def dfs(node: str) -> None:
            color[node] = 1
            stack.append(node)
            for nxt in graph.get(node, []):
                if nxt not in graph:
                    continue
                if color[nxt] == 0:
                    dfs(nxt)
                elif color[nxt] == 1:
                    loop = stack[stack.index(nxt) :] + [nxt]
                    self.add_issue(
                        "error",
                        "service.dep_cycle",
                        "Dependency cycle detected: " + " -> ".join(loop),
                        subject=node,
                    )
            stack.pop()
            color[node] = 2

        for sid in graph:
            if color[sid] == 0:
                dfs(sid)

    def validate_compose_files(self) -> None:
        for record in self.records:
            if not record.compose_file:
                continue
            compose_path = record.compose_path
            if compose_path is None:
                continue
            if not compose_path.exists():
                self.add_issue(
                    "error",
                    "compose.missing",
                    f"compose_file does not exist: {record.compose_file}",
                    subject=record.service_id,
                    path=str(record.manifest_path),
                )
                continue
            if compose_path.suffix == ".disabled":
                self.add_issue(
                    "warning",
                    "compose.disabled_suffix",
                    "compose_file points to a disabled compose fragment.",
                    subject=record.service_id,
                    path=str(compose_path),
                )
                continue

            data, err = read_yaml_or_json(compose_path)
            if data is None:
                self.add_issue(
                    "error",
                    "compose.parse_error",
                    f"Could not parse compose file: {err}",
                    subject=record.service_id,
                    path=str(compose_path),
                )
                continue

            services = data.get("services")
            if services is None:
                self.add_issue(
                    "error",
                    "compose.services_missing",
                    "Compose file must contain a top-level 'services' map.",
                    subject=record.service_id,
                    path=str(compose_path),
                )
                continue
            if not isinstance(services, dict):
                self.add_issue(
                    "error",
                    "compose.services_type",
                    "Compose 'services' must be a map/object.",
                    subject=record.service_id,
                    path=str(compose_path),
                )
                continue

            if not services:
                overlay_amd = compose_path.parent / "compose.amd.yaml"
                overlay_nvidia = compose_path.parent / "compose.nvidia.yaml"
                if not overlay_amd.exists() and not overlay_nvidia.exists():
                    self.add_issue(
                        "warning",
                        "compose.services_empty",
                        "Compose services map is empty and no GPU overlay was found.",
                        subject=record.service_id,
                        path=str(compose_path),
                    )

            if record.service_id not in services and services:
                names = ", ".join(sorted(services.keys()))
                self.add_issue(
                    "warning",
                    "compose.service_name_mismatch",
                    f"Service id '{record.service_id}' not found in compose services ({names}).",
                    subject=record.service_id,
                    path=str(compose_path),
                    hint="This is allowed but can break tooling assumptions.",
                )

            for compose_service, compose_def in services.items():
                if not isinstance(compose_def, dict):
                    self.add_issue(
                        "warning",
                        "compose.service_def_type",
                        f"Service '{compose_service}' definition should be an object.",
                        subject=record.service_id,
                        path=str(compose_path),
                    )
                    continue
                if "container_name" not in compose_def:
                    self.add_issue(
                        "info",
                        "compose.container_name_missing",
                        f"Service '{compose_service}' has no container_name.",
                        subject=record.service_id,
                        path=str(compose_path),
                    )

                for literal, env_var, default_port, raw in parse_compose_ports(compose_def.get("ports")):
                    self.port_bindings.append(
                        PortBinding(
                            service_id=record.service_id,
                            source=f"compose:{compose_service}",
                            path=str(compose_path),
                            host_port=literal if is_valid_port(literal) else None,
                            env_var=env_var,
                            default_port=default_port if is_valid_port(default_port) else None,
                            raw=raw,
                        )
                    )

    def validate_backend_dependency_compatibility(self) -> None:
        by_id = {record.service_id: record for record in self.records}
        for record in self.records:
            if not record.supports(self.gpu_backend):
                self.add_issue(
                    "warning",
                    "service.backend_unsupported",
                    f"Service does not list backend '{self.gpu_backend}'.",
                    subject=record.service_id,
                    path=str(record.manifest_path),
                    hint="This may be intentional if the service is disabled on this backend.",
                )
            for dep in record.depends_on:
                target = by_id.get(dep)
                if target is None:
                    continue
                shared = record.gpu_backends.intersection(target.gpu_backends)
                if not shared and "all" not in record.gpu_backends and "all" not in target.gpu_backends:
                    self.add_issue(
                        "warning",
                        "service.dep_backend_mismatch",
                        f"Dependency '{dep}' has no overlapping gpu_backends with '{record.service_id}'.",
                        subject=record.service_id,
                        path=str(record.manifest_path),
                    )

    def collect_manifest_ports(self) -> None:
        for record in self.records:
            if record.external_port_default is None and not record.external_port_env:
                continue
            self.port_bindings.append(
                PortBinding(
                    service_id=record.service_id,
                    source="manifest",
                    path=str(record.manifest_path),
                    host_port=record.external_port_default if is_valid_port(record.external_port_default) else None,
                    env_var=record.external_port_env,
                    default_port=record.external_port_default if is_valid_port(record.external_port_default) else None,
                    raw="external_port_default",
                )
            )

    def detect_port_collisions(self) -> list[Issue]:
        issues: list[Issue] = []

        # Detect collisions by literal/default host port
        by_port: dict[int, list[PortBinding]] = {}
        for binding in self.port_bindings:
            if binding.host_port is not None:
                by_port.setdefault(binding.host_port, []).append(binding)
            elif binding.default_port is not None:
                by_port.setdefault(binding.default_port, []).append(binding)

        for port, bindings in sorted(by_port.items()):
            owners = sorted({b.service_id for b in bindings})
            if len(owners) <= 1:
                continue
            detail = ", ".join(
                f"{b.service_id} ({b.source}, {Path(b.path).name})"
                for b in sorted(bindings, key=lambda x: (x.service_id, x.source))
            )
            issues.append(
                Issue(
                    severity="error",
                    code="ports.collision",
                    message=f"Host port {port} is declared by multiple services: {', '.join(owners)}",
                    subject=str(port),
                    hint=detail,
                )
            )

        # Detect env var reuse collisions with different defaults
        by_env: dict[str, list[PortBinding]] = {}
        for binding in self.port_bindings:
            if binding.env_var:
                by_env.setdefault(binding.env_var, []).append(binding)

        for env_var, bindings in sorted(by_env.items()):
            owners = sorted({b.service_id for b in bindings})
            defaults = sorted({b.default_port for b in bindings if b.default_port is not None})
            if len(owners) > 1 and len(defaults) > 1:
                issues.append(
                    Issue(
                        severity="warning",
                        code="ports.env_var_reused",
                        message=f"Port env var {env_var} is shared by multiple services with different defaults.",
                        subject=env_var,
                        hint=", ".join(
                            f"{b.service_id}:{b.default_port if b.default_port else '?'}"
                            for b in sorted(bindings, key=lambda x: x.service_id)
                        ),
                    )
                )

        return issues

    def summary(self) -> dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return counts

    def sorted_issues(self) -> list[Issue]:
        return sorted(
            self.issues,
            key=lambda i: (
                SEVERITY_ORDER.get(i.severity, 3),
                i.code,
                i.subject,
                i.path,
                i.message,
            ),
        )

    def print_audit_text(self) -> None:
        print("Dream Server Extension Governance Audit")
        print(f"Root: {self.root}")
        print(f"GPU Backend: {self.gpu_backend}")
        print(f"Services discovered: {len(self.records)}")
        print("")

        issues = self.sorted_issues()
        if not issues:
            print("No issues found.")
            return

        for issue in issues:
            header = f"[{issue.severity.upper()}] {issue.code}"
            if issue.subject:
                header += f" ({issue.subject})"
            print(header)
            print(f"  {issue.message}")
            if issue.path:
                print(f"  path: {issue.path}")
            if issue.hint:
                print(f"  hint: {issue.hint}")
            print("")

        summary = self.summary()
        print(
            "Summary: "
            f"errors={summary['error']} warnings={summary['warning']} info={summary['info']}"
        )

    def print_matrix_text(self) -> None:
        print("Dream Server Extension Backend Matrix")
        print(f"Root: {self.root}")
        print("")
        header = f"{'SERVICE':<24} {'CATEGORY':<12} {'ENABLED':<8} {'AMD':<5} {'NVIDIA':<8} {'APPLE':<6} DEPENDS_ON"
        print(header)
        print("-" * len(header))
        for record in sorted(self.records, key=lambda r: r.service_id):
            row = (
                f"{record.service_id:<24} "
                f"{record.category:<12} "
                f"{('yes' if record.enabled else 'no'):<8} "
                f"{('yes' if record.supports('amd') else 'no'):<5} "
                f"{('yes' if record.supports('nvidia') else 'no'):<8} "
                f"{('yes' if record.supports('apple') else 'no'):<6} "
                f"{', '.join(record.depends_on) if record.depends_on else '-'}"
            )
            print(row)

    def print_ports_text(self) -> None:
        print("Dream Server Extension Port Allocation")
        print(f"Root: {self.root}")
        print("")
        header = f"{'SERVICE':<22} {'SOURCE':<20} {'HOST PORT':<16} {'ENV':<20} PATH"
        print(header)
        print("-" * len(header))
        for binding in sorted(self.port_bindings, key=lambda b: (b.service_id, b.source, b.display_port())):
            print(
                f"{binding.service_id:<22} "
                f"{binding.source:<20} "
                f"{binding.display_port():<16} "
                f"{(binding.env_var or '-'): <20} "
                f"{binding.path}"
            )


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Dream Server root directory (default: project root).",
    )
    parser.add_argument(
        "--gpu-backend",
        default="nvidia",
        choices=sorted(KNOWN_GPU_BACKENDS - {"all"}),
        help="Backend profile for compatibility checks.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dream Server extension governance checks.",
    )
    add_common_options(parser)

    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="Run governance checks.")
    add_common_options(audit)
    audit.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures.",
    )

    matrix = sub.add_parser("matrix", help="Print backend support matrix.")
    add_common_options(matrix)

    ports = sub.add_parser("ports", help="Print host-port allocations and collisions.")
    add_common_options(ports)
    return parser


def command_audit(toolkit: GovernanceToolkit, strict: bool, output_format: str) -> int:
    toolkit.load()
    toolkit.issues.extend(toolkit.detect_port_collisions())

    if output_format == "json":
        payload = {
            "root": str(toolkit.root),
            "gpu_backend": toolkit.gpu_backend,
            "service_count": len(toolkit.records),
            "summary": toolkit.summary(),
            "issues": [issue.to_json() for issue in toolkit.sorted_issues()],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        toolkit.print_audit_text()

    summary = toolkit.summary()
    if summary["error"] > 0:
        return 1
    if strict and summary["warning"] > 0:
        return 1
    return 0


def command_matrix(toolkit: GovernanceToolkit, output_format: str) -> int:
    toolkit.load()

    if output_format == "json":
        rows = []
        for record in sorted(toolkit.records, key=lambda r: r.service_id):
            rows.append(
                {
                    "service_id": record.service_id,
                    "name": record.name,
                    "category": record.category,
                    "enabled": record.enabled,
                    "depends_on": record.depends_on,
                    "gpu_backends": sorted(record.gpu_backends),
                    "supports": {
                        "amd": record.supports("amd"),
                        "nvidia": record.supports("nvidia"),
                        "apple": record.supports("apple"),
                    },
                }
            )
        print(json.dumps({"rows": rows, "service_count": len(rows)}, indent=2, sort_keys=True))
    else:
        toolkit.print_matrix_text()
    return 0


def command_ports(toolkit: GovernanceToolkit, output_format: str) -> int:
    toolkit.load()
    collisions = toolkit.detect_port_collisions()

    if output_format == "json":
        payload = {
            "root": str(toolkit.root),
            "bindings": [asdict(binding) for binding in toolkit.port_bindings],
            "collisions": [issue.to_json() for issue in collisions],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        toolkit.print_ports_text()
        if collisions:
            print("")
            print("Port collisions:")
            for issue in collisions:
                print(f"- {issue.message}")
                if issue.hint:
                    print(f"  {issue.hint}")

    return 1 if collisions else 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root).expanduser().resolve()
    toolkit = GovernanceToolkit(root=root, gpu_backend=args.gpu_backend)

    if args.command == "audit":
        return command_audit(toolkit, strict=args.strict, output_format=args.format)
    if args.command == "matrix":
        return command_matrix(toolkit, output_format=args.format)
    if args.command == "ports":
        return command_ports(toolkit, output_format=args.format)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
