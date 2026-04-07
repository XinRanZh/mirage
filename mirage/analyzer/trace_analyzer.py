"""Trace Analyzer — learn dependency behavior from observed OTel traces.

Extracts from traces (without needing dependency source code):
  1. Per-endpoint: request patterns, response patterns, status distributions
  2. Temporal: call ordering, retry sequences, polling patterns
  3. Data: request/response field correlations
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class EndpointBehavior:
    """Observed behavior of a single dependency endpoint."""
    method: str
    path_pattern: str
    call_count: int = 0
    status_distribution: Dict[int, int] = field(default_factory=dict)
    examples: List[dict] = field(default_factory=list)
    is_async: bool = False          # returns 202, needs polling
    has_auth: bool = False          # sees 401 responses
    has_idempotency: bool = False   # sees Idempotency-Key header
    has_state: bool = False         # different responses for same endpoint
    avg_duration_ms: float = 0.0


@dataclass
class DependencyBehaviorProfile:
    """Everything we learned about a dependency from traces."""
    service_name: str
    port: int = 0
    endpoints: Dict[str, EndpointBehavior] = field(default_factory=dict)
    call_sequences: List[List[str]] = field(default_factory=list)
    retry_patterns: List[dict] = field(default_factory=list)
    polling_patterns: List[dict] = field(default_factory=list)


_ID_PATTERNS = [
    (r'/(item-\w+)', '{item_id}'),
    (r'/(rsv-\w+)', '{reservation_id}'),
    (r'/(shp-\w+)', '{shipment_id}'),
    (r'/(ch-\w+)', '{charge_id}'),
    (r'/(ord-\w+)', '{order_id}'),
    (r'/(\d+)/', '{id}/'),
    (r'/(\d+)$', '{id}'),
    (r'/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', '{uuid}'),
]


def _normalize_path(path: str) -> str:
    normalized = path.split("?")[0]
    for pattern, replacement in _ID_PATTERNS:
        normalized = re.sub(pattern, f'/{replacement}', normalized)
    return normalized


def analyze_traces(
    traces_dir: str,
    sut_service: str = "order-service",
) -> Dict[str, DependencyBehaviorProfile]:
    """Analyze traces to build behavior profiles for each dependency.

    Reads ALL trace files, but focuses on CLIENT spans from the SUT
    (outgoing calls to dependencies).
    """
    all_spans = []
    for fname in os.listdir(traces_dir):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(traces_dir, fname)) as f:
            for line in f:
                if line.strip():
                    all_spans.append(json.loads(line))

    traces = defaultdict(list)
    for span in all_spans:
        traces[span["trace_id"]].append(span)

    deps = {}

    for span in all_spans:
        if span.get("service") != sut_service or span.get("kind") != "CLIENT":
            continue

        attrs = span.get("attributes", {})
        url = attrs.get("http.url", "")
        method = attrs.get("http.method", "GET")
        status = attrs.get("http.status_code")
        duration = span.get("duration_ns", 0)

        if not url:
            continue

        port_match = re.search(r':(\d{4,5})', url)
        if not port_match:
            continue
        port = int(port_match.group(1))

        path_match = re.search(r':\d+(/[^\s]*)', url)
        path = path_match.group(1) if path_match else "/"
        path_pattern = _normalize_path(path)

        svc_name = _port_to_service(port, all_spans)
        if svc_name not in deps:
            deps[svc_name] = DependencyBehaviorProfile(service_name=svc_name, port=port)

        dep = deps[svc_name]
        ep_key = f"{method} {path_pattern}"

        if ep_key not in dep.endpoints:
            dep.endpoints[ep_key] = EndpointBehavior(method=method, path_pattern=path_pattern)

        ep = dep.endpoints[ep_key]
        ep.call_count += 1
        ep.status_distribution[status] = ep.status_distribution.get(status, 0) + 1

        if len(ep.examples) < 5:
            ep.examples.append({
                "url": url, "path": path, "status": status,
                "duration_ms": round(duration / 1e6, 1) if duration else 0,
            })

        if status == 202:
            ep.is_async = True
        if status == 401:
            ep.has_auth = True

    # Analyze call sequences per trace
    for trace_id, trace_spans in traces.items():
        sut_client_spans = [
            s for s in trace_spans
            if s.get("service") == sut_service and s.get("kind") == "CLIENT"
        ]
        sut_client_spans.sort(key=lambda s: s.get("start_time", 0))

        if len(sut_client_spans) > 1:
            seq = []
            for s in sut_client_spans:
                attrs = s.get("attributes", {})
                url = attrs.get("http.url", "")
                method = attrs.get("http.method", "")
                status = attrs.get("http.status_code", "")
                path_match = re.search(r':\d+(/[^\s]*)', url)
                path = _normalize_path(path_match.group(1)) if path_match else "?"
                seq.append(f"{method} {path} -> {status}")

            _detect_retries(seq, deps)
            _detect_polling(seq, deps)

    for dep in deps.values():
        for ep in dep.endpoints.values():
            total_dur = sum(ex["duration_ms"] for ex in ep.examples)
            ep.avg_duration_ms = total_dur / len(ep.examples) if ep.examples else 0

    return deps


def _port_to_service(port: int, spans: List[dict]) -> str:
    for span in spans:
        if span.get("kind") == "SERVER":
            attrs = span.get("attributes", {})
            if attrs.get("net.host.port") == port:
                return span.get("service", f"service-{port}")
    return f"service-{port}"


def _detect_retries(seq: List[str], deps: Dict[str, DependencyBehaviorProfile]):
    for i in range(len(seq) - 1):
        if "503" in seq[i] or "401" in seq[i]:
            ep1 = seq[i].split(" -> ")[0]
            ep2 = seq[i + 1].split(" -> ")[0]
            if ep1 == ep2:
                for dep in deps.values():
                    for ep in dep.endpoints.values():
                        if ep1.startswith(f"{ep.method} {ep.path_pattern}"):
                            dep.retry_patterns.append({
                                "endpoint": ep1,
                                "error_status": seq[i].split("->")[-1].strip(),
                                "retry_status": seq[i + 1].split("->")[-1].strip(),
                            })


def _detect_polling(seq: List[str], deps: Dict[str, DependencyBehaviorProfile]):
    for i in range(len(seq) - 2):
        if seq[i].startswith("GET") and seq[i + 1].startswith("GET"):
            ep = seq[i].split(" -> ")[0]
            ep_next = seq[i + 1].split(" -> ")[0]
            if ep == ep_next:
                for dep in deps.values():
                    for endpoint in dep.endpoints.values():
                        if ep.startswith(f"GET {endpoint.path_pattern}"):
                            dep.polling_patterns.append({"endpoint": ep, "count": 2})
                            break


def format_trace_analysis(deps: Dict[str, DependencyBehaviorProfile]) -> str:
    """Format trace analysis as readable text."""
    lines = [f"Analyzed {len(deps)} dependencies from traces:\n"]

    for name, dep in sorted(deps.items()):
        lines.append(f"  {name} (port {dep.port}):")
        for key, ep in sorted(dep.endpoints.items()):
            status_str = ", ".join(f"{s}:{c}" for s, c in sorted(ep.status_distribution.items()))
            flags = []
            if ep.is_async:
                flags.append("ASYNC")
            if ep.has_auth:
                flags.append("AUTH")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"    {key}: {ep.call_count}x ({status_str}){flag_str}")
        if dep.retry_patterns:
            lines.append(f"    Retry patterns: {len(dep.retry_patterns)}x")
        if dep.polling_patterns:
            lines.append(f"    Polling patterns: {len(dep.polling_patterns)}x")
        lines.append("")

    return "\n".join(lines)
