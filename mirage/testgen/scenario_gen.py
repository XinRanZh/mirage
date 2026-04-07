"""Test Scenario Generator — LLM analyzes SUT code and generates E2E test scenarios.

Reads YOUR service code + trace analysis, then asks:
  "What could go wrong? What edge cases should we test?"

Generates scenarios that tell the mock backend what to simulate:
  - "Return 503 on the 2nd charge request"
  - "Return 401 on all charge requests (token expired)"
  - "Return 409 on confirm (version conflict)"
  - "Never transition shipment to SHIPPED (timeout test)"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TestScenario:
    """A generated test scenario with mock configuration."""
    name: str
    description: str
    category: str       # happy_path, error_handling, timeout, state_conflict, etc.
    sut_action: dict    # e.g., {"method": "POST", "path": "/orders", "body": {...}}
    mock_behaviors: Dict[str, List[dict]]  # dep_name -> list of behavior overrides
    expected_status: Optional[int] = None
    expected_behavior: str = ""


def generate_scenarios(
    sut_code: str,
    sut_analysis: str,
    trace_analysis: str,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
) -> List[TestScenario]:
    """Use LLM to generate test scenarios from SUT code analysis.

    The LLM reads the SUT code and identifies:
    1. Happy paths that should work
    2. Error handling paths that need to be exercised
    3. Timeout/retry paths
    4. State conflict scenarios
    5. Edge cases from business logic
    """
    prompt = f"""You are a senior QA engineer generating E2E test scenarios for a microservice.

## The Service Under Test (SUT)
```python
{sut_code[:6000]}
```

## SUT's Dependencies and How It Uses Them
{sut_analysis}

## What Traces Show About Dependency Behavior
{trace_analysis}

## Your Task

Generate 10-15 concrete test scenarios. For each scenario:
1. What action does the user take? (e.g., POST /orders with specific body)
2. How should each mock dependency behave? (specific status codes, delays, state)
3. What should the SUT do? (expected status code, expected behavior)

Focus on:
- **Happy path**: everything works
- **Retry resilience**: dependency returns 503, does SUT retry correctly?
- **Auth failures**: dependency returns 401, does SUT refresh token?
- **State conflicts**: dependency returns 409, does SUT handle it?
- **Partial failures**: some deps succeed, some fail — does SUT compensate?
- **Timeout**: dependency is slow or never responds — does SUT timeout gracefully?
- **Edge cases**: empty data, missing fields, concurrent operations

## Output Format

Return a JSON array:
```json
[
  {{
    "name": "happy_path_order",
    "description": "Normal order flow — all dependencies respond successfully",
    "category": "happy_path",
    "sut_action": {{
      "method": "POST",
      "path": "/orders",
      "body": {{"item_id": "item-001", "quantity": 1}}
    }},
    "mock_behaviors": {{
      "inventory-service": [
        {{"endpoint": "POST /items/*/reserve", "respond": {{"status": 201, "body": {{"reservation_id": "rsv-test"}}}}}}
      ]
    }},
    "expected_status": 201,
    "expected_behavior": "Order created successfully"
  }}
]
```

Return ONLY the JSON array, no markdown fences.
"""

    from mirage.llm import completion
    raw = completion(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        temperature=0.3,
        max_tokens=8192,
        api_key=api_key,
        api_base=api_base,
    )

    clean = raw.strip()
    if clean.startswith("```"):
        clean = re.sub(r'^```\w*\n?', '', clean)
        clean = re.sub(r'\n?```$', '', clean)

    scenarios_raw = json.loads(clean)

    scenarios = []
    for s in scenarios_raw:
        scenarios.append(TestScenario(
            name=s.get("name", "unnamed"),
            description=s.get("description", ""),
            category=s.get("category", "unknown"),
            sut_action=s.get("sut_action", {}),
            mock_behaviors=s.get("mock_behaviors", {}),
            expected_status=s.get("expected_status"),
            expected_behavior=s.get("expected_behavior", ""),
        ))

    return scenarios


def format_scenarios(scenarios: List[TestScenario]) -> str:
    """Format scenarios as human-readable text."""
    lines = [f"Generated {len(scenarios)} test scenarios:\n"]

    by_category = {}
    for s in scenarios:
        by_category.setdefault(s.category, []).append(s)

    for cat, cat_scenarios in sorted(by_category.items()):
        lines.append(f"  [{cat}] ({len(cat_scenarios)} scenarios)")
        for s in cat_scenarios:
            status = f" -> expect {s.expected_status}" if s.expected_status else ""
            lines.append(f"    - {s.name}: {s.description}{status}")
        lines.append("")

    return "\n".join(lines)
