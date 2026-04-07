"""Service discovery — scan traces + codebase to find services and their dependencies.

Given a trace directory and codebase, automatically:
1. Find all services from trace files
2. Detect which service is the "caller" (makes outgoing calls)
3. Map service names to source files
4. Detect communication patterns (HTTP endpoints, ports)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set


@dataclass
class DiscoveredService:
    name: str
    source_path: Optional[str] = None
    trace_path: Optional[str] = None
    port: int = 0
    endpoints: List[str] = None  # type: ignore
    is_caller: bool = False
    calls_to: List[str] = None  # type: ignore

    def __post_init__(self):
        if self.endpoints is None:
            self.endpoints = []
        if self.calls_to is None:
            self.calls_to = []


def discover_services(
    traces_dir: str,
    codebase_dir: str,
) -> Dict[str, DiscoveredService]:
    """Auto-discover services from traces and codebase."""
    services = {}

    if os.path.isdir(traces_dir):
        for fname in os.listdir(traces_dir):
            if not fname.endswith(".jsonl"):
                continue
            svc_name = fname.replace(".jsonl", "")
            trace_path = os.path.join(traces_dir, fname)

            svc = DiscoveredService(name=svc_name, trace_path=trace_path)

            endpoints = set()
            ports = set()
            outgoing_services = set()

            with open(trace_path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    span = json.loads(line)
                    attrs = span.get("attributes", {})
                    kind = span.get("kind", "")

                    if kind == "SERVER":
                        route = attrs.get("http.route") or attrs.get("http.target", "")
                        if route:
                            endpoints.add(f"{attrs.get('http.method', 'GET')} {route.split('?')[0]}")
                        port = attrs.get("net.host.port")
                        if port:
                            ports.add(int(port))

                    elif kind == "CLIENT":
                        url = attrs.get("http.url", "")
                        port_match = re.search(r':(\d{4,5})/', url)
                        if port_match:
                            outgoing_services.add(int(port_match.group(1)))

            svc.endpoints = sorted(endpoints)
            if ports:
                svc.port = min(ports)
            if outgoing_services:
                svc.is_caller = True

            services[svc_name] = svc

    if os.path.isdir(codebase_dir):
        _match_source_files(services, codebase_dir)

    # Resolve caller->dependency relationships
    port_to_name = {svc.port: svc.name for svc in services.values() if svc.port}
    for svc in services.values():
        if svc.is_caller and svc.trace_path and os.path.exists(svc.trace_path):
            outgoing_ports = set()
            with open(svc.trace_path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    span = json.loads(line)
                    if span.get("kind") == "CLIENT":
                        url = span.get("attributes", {}).get("http.url", "")
                        m = re.search(r':(\d{4,5})/', url)
                        if m:
                            outgoing_ports.add(int(m.group(1)))
            svc.calls_to = [
                port_to_name[p] for p in outgoing_ports
                if p in port_to_name and port_to_name[p] != svc.name
            ]

    return services


def _match_source_files(services: Dict[str, DiscoveredService], codebase_dir: str):
    for root, dirs, files in os.walk(codebase_dir):
        for fname in files:
            if fname != "main.py":
                continue
            fpath = os.path.join(root, fname)
            parent = os.path.basename(root)
            svc_name = parent.replace("_", "-")
            if svc_name in services:
                services[svc_name].source_path = fpath


def auto_generate_config(
    traces_dir: str,
    codebase_dir: str,
    model: Optional[str] = None,
) -> str:
    """Auto-generate mirage.yaml from discovered services."""
    services = discover_services(traces_dir, codebase_dir)
    model = model or "claude-sonnet-4-5-20241022"

    sut = None
    deps = {}
    for name, svc in services.items():
        if svc.is_caller and svc.calls_to:
            sut = svc
        else:
            deps[name] = svc

    if not sut:
        sut = max(services.values(), key=lambda s: len(s.calls_to), default=None)
        if sut:
            deps = {n: s for n, s in services.items() if n != sut.name}

    lines = [
        "# MIRAGE Configuration (auto-generated)",
        f"# Discovered {len(services)} services from traces",
        "",
        "project:",
        f"  name: {os.path.basename(os.path.dirname(traces_dir)) or 'my-project'}",
        f"  codebase: {codebase_dir}",
        f"  traces: {traces_dir}",
        "",
    ]

    if sut:
        sut_module = f"services.{sut.name.replace('-', '_')}.main:app" if sut.source_path else ""
        lines += [
            "sut:",
            f"  name: {sut.name}",
            f"  module: {sut_module}",
            f"  port: {sut.port}",
            "  env:",
        ]
        for dep_name in sut.calls_to:
            env_key = dep_name.upper().replace("-", "_") + "_URL"
            lines.append(f"    {env_key}: http://localhost:${{{dep_name}.mock_port}}")
        lines.append("")

    lines.append("dependencies:")
    mock_port = 9001
    for name, svc in sorted(deps.items()):
        source = svc.source_path or f"services/{name.replace('-', '_')}/main.py"
        trace = svc.trace_path or f"trace-data/{name}.jsonl"
        lines += [
            f"  {name}:",
            f"    source: {source}",
            f"    traces: {trace}",
            f"    port: {svc.port}",
            f"    mock_port: {mock_port}",
            "",
        ]
        mock_port += 1

    lines += [
        "mock:",
        f"  backend: llm",
        f"  model: {model}",
        "  temperature: 0.1",
        "  max_tokens: 2048",
        "  timeout: 120",
    ]

    return "\n".join(lines)
