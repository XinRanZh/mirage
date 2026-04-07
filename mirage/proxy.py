"""MIRAGE Proxy — route SUT traffic to mock servers.

Two approaches for directing SUT traffic to mocks:

1. Environment variable injection (recommended):
   Override URL env vars so the SUT connects to mock ports directly.

2. HTTP proxy:
   Run MIRAGE as a forward proxy and configure the SUT's HTTP client
   to use it (via HTTP_PROXY env var or client-level proxy settings).
"""

from __future__ import annotations

from typing import Dict


def env_var_injection(sut_env: Dict[str, str], mock_ports: Dict[str, int]) -> Dict[str, str]:
    """Generate environment variables that point the SUT to mock ports.

    This is the simplest approach — just override URL environment variables.

    Args:
        sut_env: the SUT's current environment variable template
        mock_ports: mapping of dep_name -> mock_port

    Returns:
        Environment variables with URLs pointing to mock ports
    """
    result = dict(sut_env)
    for dep_name, port in mock_ports.items():
        patterns = [
            dep_name.upper().replace("-", "_") + "_URL",
            dep_name.split("-")[0].upper() + "_URL",
        ]
        for key in list(result.keys()):
            for pattern in patterns:
                if key == pattern or key.endswith(pattern):
                    result[key] = f"http://localhost:{port}"
    return result
