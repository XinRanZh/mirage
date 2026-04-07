"""Configuration loader for MIRAGE.

Loads mirage.yaml, validates structure, resolves paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

import yaml


@dataclass
class ServiceConfig:
    name: str
    source: str
    traces: Optional[str] = None
    port: int = 0           # real service port
    mock_port: int = 0      # mock server port


@dataclass
class SUTConfig:
    name: str
    module: str             # e.g., "services.order_service.main:app"
    port: int = 8000
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class MockConfig:
    backend: str = "llm"    # "llm" (online per-request) or "contract" (pre-generated)
    model: str = "claude-sonnet-4-5-20241022"
    api_base: Optional[str] = None   # custom API base URL (OpenAI-compatible)
    api_key: Optional[str] = None    # API key (overrides env var)
    temperature: float = 0.1
    max_tokens: int = 2048
    timeout: int = 120


@dataclass
class MirageConfig:
    project_name: str
    codebase: str
    traces_dir: str
    sut: SUTConfig
    dependencies: Dict[str, ServiceConfig]
    mock: MockConfig
    _base_dir: str = ""     # directory containing mirage.yaml

    def resolve_path(self, path: str) -> str:
        """Resolve a path relative to the config file location."""
        if os.path.isabs(path):
            return path
        return os.path.normpath(os.path.join(self._base_dir, path))


def load_config(path: str = "mirage.yaml") -> MirageConfig:
    """Load and validate mirage.yaml."""
    path = os.path.abspath(path)
    base_dir = os.path.dirname(path)

    with open(path) as f:
        raw = yaml.safe_load(f)

    project = raw.get("project", {})
    sut_raw = raw.get("sut", {})
    deps_raw = raw.get("dependencies", {})
    mock_raw = raw.get("mock", {})

    sut = SUTConfig(
        name=sut_raw.get("name", "sut"),
        module=sut_raw.get("module", ""),
        port=sut_raw.get("port", 8000),
        env=sut_raw.get("env", {}),
    )

    dependencies = {}
    mock_port_start = 9001
    for name, dep in deps_raw.items():
        dependencies[name] = ServiceConfig(
            name=name,
            source=dep.get("source", ""),
            traces=dep.get("traces"),
            port=dep.get("port", 0),
            mock_port=dep.get("mock_port", mock_port_start),
        )
        mock_port_start += 1

    mock = MockConfig(
        backend=mock_raw.get("backend", "llm"),
        model=mock_raw.get("model", "claude-sonnet-4-5-20241022"),
        api_base=mock_raw.get("api_base"),
        api_key=mock_raw.get("api_key"),
        temperature=mock_raw.get("temperature", 0.1),
        max_tokens=mock_raw.get("max_tokens", 2048),
        timeout=mock_raw.get("timeout", 120),
    )

    return MirageConfig(
        project_name=project.get("name", "mirage-project"),
        codebase=project.get("codebase", "."),
        traces_dir=project.get("traces", "trace-data"),
        sut=sut,
        dependencies=dependencies,
        mock=mock,
        _base_dir=base_dir,
    )


def generate_config_template() -> str:
    """Generate a mirage.yaml template."""
    return """# MIRAGE Configuration
# Run: mirage init --codebase ./src --traces ./traces/ to auto-generate

project:
  name: my-project
  codebase: ./services          # path to service source code
  traces: ./trace-data          # path to OTel trace JSONL files

sut:
  name: order-service           # the service you want to test
  module: services.order_service.main:app
  port: 8000
  env:                          # env vars for SUT (mock URLs injected automatically)
    INVENTORY_URL: http://localhost:${inventory-service.mock_port}
    PAYMENT_URL: http://localhost:${payment-service.mock_port}

dependencies:                   # services to mock
  inventory-service:
    source: services/inventory_service/main.py
    traces: trace-data/inventory-service.jsonl
    port: 8001                  # real service port (for validation)
    mock_port: 9001             # mock server port

  payment-service:
    source: services/payment_service/main.py
    traces: trace-data/payment-service.jsonl
    port: 8002
    mock_port: 9002

mock:
  backend: llm                  # llm (online per-request) | contract (pre-generated)
  model: claude-sonnet-4-5-20241022   # any model supported by litellm
  # api_base: https://api.openai.com/v1  # optional: custom API endpoint
  # api_key: sk-...              # optional: overrides env var
  temperature: 0.1
  max_tokens: 2048
  timeout: 120
"""
