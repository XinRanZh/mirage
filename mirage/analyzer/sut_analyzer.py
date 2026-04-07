"""SUT Analyzer — reads YOUR service code to find all outgoing dependency calls.

Extracts:
  1. Which URLs/services your code calls (httpx, requests, aiohttp)
  2. What HTTP methods it uses
  3. What request bodies it sends
  4. How it handles responses (status code checks, error handling, retries)
  5. What response fields it actually reads
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Set


@dataclass
class OutgoingCall:
    """A detected outgoing HTTP call from SUT to a dependency."""
    url_pattern: str          # e.g., "{PAYMENT_URL}/charges"
    http_method: str          # GET, POST, etc.
    base_url_var: str         # e.g., "PAYMENT_URL"
    path: str                 # e.g., "/charges"
    sends_body: bool = False
    sends_headers: bool = False
    header_keys: List[str] = field(default_factory=list)
    handles_401: bool = False
    handles_403: bool = False
    handles_404: bool = False
    handles_409: bool = False
    handles_422: bool = False
    handles_503: bool = False
    has_retry: bool = False
    response_fields: List[str] = field(default_factory=list)
    source_file: str = ""
    line_number: int = 0


@dataclass
class DependencyProfile:
    """Everything we know about a dependency from SUT code analysis."""
    name: str
    base_url_var: str
    calls: List[OutgoingCall] = field(default_factory=list)
    all_paths: Set[str] = field(default_factory=set)
    all_methods: Set[str] = field(default_factory=set)
    handled_errors: Set[int] = field(default_factory=set)
    has_retry_logic: bool = False
    has_compensation: bool = False


def analyze_sut(sut_dir: str) -> Dict[str, DependencyProfile]:
    """Analyze SUT source code to discover all dependencies.

    Args:
        sut_dir: Path to SUT source directory (or single file)

    Returns:
        Dict of dependency_name -> DependencyProfile
    """
    py_files = []
    if os.path.isfile(sut_dir):
        py_files = [sut_dir]
    else:
        for root, dirs, files in os.walk(sut_dir):
            for f in files:
                if f.endswith(".py"):
                    py_files.append(os.path.join(root, f))

    all_calls = []
    url_vars = {}

    for fpath in py_files:
        with open(fpath) as f:
            source = f.read()

        # Find URL configuration variables
        for match in re.finditer(
            r'(\w+_URL)\s*=\s*os\.environ\.get\(\s*["\'](\w+)["\'].*?["\']http://.*?:(\d+)["\']',
            source
        ):
            var_name = match.group(1)
            svc_name = _var_to_service_name(var_name)
            url_vars[var_name] = svc_name

        for match in re.finditer(r'(\w+_URL)\s*=\s*["\']http://', source):
            var_name = match.group(1)
            if var_name not in url_vars:
                url_vars[var_name] = _var_to_service_name(var_name)

        # Find outgoing HTTP calls
        for match in re.finditer(
            r'(?:await\s+)?(?:client|self|httpx|requests)\.(get|post|put|delete|patch)\(\s*'
            r'f?["\']?\{?(\w+(?:_URL)?)\}?/([^"\')\s,]+)',
            source, re.IGNORECASE
        ):
            method = match.group(1).upper()
            base_var = match.group(2)
            path = "/" + match.group(3).rstrip('"\')')
            path = re.sub(r'\{[^}]+\}', '{param}', path)

            call = OutgoingCall(
                url_pattern=f"{{{base_var}}}{path}",
                http_method=method,
                base_url_var=base_var,
                path=path,
                source_file=fpath,
                line_number=match.start(),
            )
            all_calls.append(call)

        _detect_error_handling(source, all_calls)
        _detect_response_fields(source, all_calls)
        _detect_retry_compensation(source, all_calls)

    deps = {}
    for call in all_calls:
        base = call.base_url_var
        if base not in url_vars:
            url_vars[base] = _var_to_service_name(base)

        svc_name = url_vars[base]
        if svc_name not in deps:
            deps[svc_name] = DependencyProfile(name=svc_name, base_url_var=base)

        dep = deps[svc_name]
        dep.calls.append(call)
        dep.all_paths.add(call.path)
        dep.all_methods.add(call.http_method)

        for status in [401, 403, 404, 409, 422, 503]:
            if getattr(call, f"handles_{status}", False):
                dep.handled_errors.add(status)

        if call.has_retry:
            dep.has_retry_logic = True

    return deps


def _var_to_service_name(var: str) -> str:
    """Convert URL var name to service name: PAYMENT_URL -> payment-service"""
    name = var.replace("_URL", "").replace("_", "-").lower()
    if not name.endswith("-service"):
        name += "-service"
    return name


def _detect_error_handling(source: str, calls: List[OutgoingCall]):
    for status in [401, 403, 404, 409, 422, 503]:
        if re.search(rf'status_code\s*==\s*{status}', source):
            for call in calls:
                setattr(call, f"handles_{status}", True)


def _detect_response_fields(source: str, calls: List[OutgoingCall]):
    fields = set()
    for match in re.finditer(r'\.json\(\)\s*(?:\[|\.get\()["\'](\w+)', source):
        fields.add(match.group(1))
    for match in re.finditer(r'data\s*(?:\[|\.get\()["\'](\w+)', source):
        fields.add(match.group(1))

    for call in calls:
        call.response_fields = list(fields)


def _detect_retry_compensation(source: str, calls: List[OutgoingCall]):
    has_retry = bool(re.search(r'for\s+attempt\s+in\s+range|retry|backoff', source, re.IGNORECASE))
    for call in calls:
        call.has_retry = has_retry


def format_analysis(deps: Dict[str, DependencyProfile]) -> str:
    """Format analysis results as human-readable text."""
    lines = [f"Found {len(deps)} dependencies:\n"]

    for name, dep in sorted(deps.items()):
        lines.append(f"  {name} (via ${dep.base_url_var}):")
        lines.append(f"    Endpoints: {len(dep.all_paths)}")
        for call in dep.calls:
            lines.append(f"      {call.http_method:6s} {call.path}")
        lines.append(f"    Error handling: {sorted(dep.handled_errors) or 'none detected'}")
        lines.append(f"    Retry logic: {'yes' if dep.has_retry_logic else 'no'}")
        if any(c.response_fields for c in dep.calls):
            all_fields = set()
            for c in dep.calls:
                all_fields.update(c.response_fields)
            lines.append(f"    Response fields used: {sorted(all_fields)}")
        lines.append("")

    return "\n".join(lines)
