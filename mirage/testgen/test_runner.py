"""Test Runner — execute auto-generated scenarios against mocked dependencies.

For each scenario:
  1. Configure mocks to simulate the scenario's conditions
  2. Send the SUT action (e.g., POST /orders)
  3. Check if the SUT's response matches expectations
  4. Report pass/fail with details
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from .scenario_gen import TestScenario


@dataclass
class ScenarioResult:
    name: str
    category: str
    passed: bool
    expected_status: Optional[int]
    actual_status: int
    duration_ms: float
    detail: str = ""
    mock_log: Optional[dict] = None


@dataclass
class TestReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    results: List[ScenarioResult] = field(default_factory=list)
    by_category: Dict[str, dict] = field(default_factory=dict)

    def add(self, result: ScenarioResult):
        self.total += 1
        if result.passed:
            self.passed += 1
        else:
            self.failed += 1
        self.results.append(result)

        cat = result.category
        if cat not in self.by_category:
            self.by_category[cat] = {"total": 0, "passed": 0}
        self.by_category[cat]["total"] += 1
        if result.passed:
            self.by_category[cat]["passed"] += 1

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0

    def summary(self) -> str:
        lines = [
            f"Test Report: {self.passed}/{self.total} passed ({self.pass_rate:.0%})",
            "",
            "By category:",
        ]
        for cat, stats in sorted(self.by_category.items()):
            rate = stats["passed"] / stats["total"] if stats["total"] > 0 else 0
            lines.append(f"  {cat:25s} {stats['passed']}/{stats['total']} ({rate:.0%})")

        failed = [r for r in self.results if not r.passed]
        if failed:
            lines.append(f"\nFailed ({len(failed)}):")
            for r in failed:
                lines.append(f"  {r.name}: expected={r.expected_status} actual={r.actual_status}")
                if r.detail:
                    lines.append(f"    {r.detail}")

        return "\n".join(lines)


async def run_scenarios(
    scenarios: List[TestScenario],
    sut_base_url: str,
    mock_ports: Dict[str, int],
    timeout: float = 60.0,
) -> TestReport:
    """Run all test scenarios against the SUT with mocked dependencies.

    For LLM mocks, the mock_behaviors in each scenario are communicated
    via the LLM's conversation context (it reads the scenario description).
    """
    report = TestReport()

    async with httpx.AsyncClient(timeout=timeout) as client:
        for scenario in scenarios:
            # Reset all mocks before each scenario
            for dep_name, port in mock_ports.items():
                try:
                    await client.post(f"http://localhost:{port}/__mock__/reset")
                except Exception:
                    pass

            action = scenario.sut_action
            method = action.get("method", "POST")
            path = action.get("path", "/")
            body = action.get("body")
            headers = action.get("headers", {})

            t0 = time.monotonic()
            try:
                if method == "GET":
                    resp = await client.get(f"{sut_base_url}{path}", headers=headers)
                elif method == "POST":
                    resp = await client.post(f"{sut_base_url}{path}", json=body, headers=headers)
                elif method == "DELETE":
                    resp = await client.delete(f"{sut_base_url}{path}", headers=headers)
                else:
                    resp = await client.request(method, f"{sut_base_url}{path}",
                                                json=body, headers=headers)

                actual_status = resp.status_code
                duration = (time.monotonic() - t0) * 1000

                passed = True
                detail = ""
                if scenario.expected_status is not None:
                    passed = actual_status == scenario.expected_status
                    if not passed:
                        detail = f"Status mismatch: got {actual_status}"

                mock_log = {}
                for dep_name, port in mock_ports.items():
                    try:
                        log_resp = await client.get(f"http://localhost:{port}/__mock__/log")
                        mock_log[dep_name] = log_resp.json()
                    except Exception:
                        pass

                result = ScenarioResult(
                    name=scenario.name,
                    category=scenario.category,
                    passed=passed,
                    expected_status=scenario.expected_status,
                    actual_status=actual_status,
                    duration_ms=round(duration, 1),
                    detail=detail,
                    mock_log=mock_log,
                )

            except Exception as e:
                duration = (time.monotonic() - t0) * 1000
                result = ScenarioResult(
                    name=scenario.name,
                    category=scenario.category,
                    passed=False,
                    expected_status=scenario.expected_status,
                    actual_status=-1,
                    duration_ms=round(duration, 1),
                    detail=f"Error: {e}",
                )

            report.add(result)

    return report
