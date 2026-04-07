"""LLM Backend — online per-request dependency simulation.

Modes:
  BLACK_BOX:  Caller code + traces only (dependency is opaque)
  GREY_BOX:   Caller code + traces + API schema
  WHITE_BOX:  Dependency source code (+ optional caller code + traces)

The LLM builds its understanding from whatever signals are available
and maintains cross-request state throughout a test scenario.
"""

from __future__ import annotations

import json
import re
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AccessMode(str, Enum):
    BLACK_BOX = "black_box"   # Caller code + traces only
    GREY_BOX = "grey_box"     # + API schema
    WHITE_BOX = "white_box"   # + dependency source code


def build_context(
    dep_name: str,
    mode: AccessMode,
    sut_code: Optional[str] = None,
    sut_analysis: Optional[str] = None,
    trace_analysis: Optional[str] = None,
    dep_source: Optional[str] = None,
    api_schema: Optional[str] = None,
    scenario_instruction: Optional[str] = None,
) -> str:
    """Build system prompt based on access mode."""

    parts = [f"You are simulating **{dep_name}**, a microservice dependency.\n"]

    if mode == AccessMode.WHITE_BOX and dep_source:
        parts.append(f"""## Access Level: WHITE BOX
You have the dependency's full source code.

## {dep_name} Source Code
```python
{dep_source[:8000]}
```
""")

    elif mode == AccessMode.BLACK_BOX:
        parts.append(f"""## Access Level: BLACK BOX
You do NOT have {dep_name}'s source code. You must infer its behavior from:
1. How the caller (SUT) uses it
2. What was observed in production traces
""")

    elif mode == AccessMode.GREY_BOX:
        parts.append(f"""## Access Level: GREY BOX
You have the API schema but NOT the source code of {dep_name}.
""")
        if api_schema:
            parts.append(f"## API Schema\n{api_schema}\n")

    # Always include SUT analysis if available
    if sut_analysis:
        parts.append(f"## What the Caller (SUT) Expects\n{sut_analysis}\n")

    if sut_code:
        parts.append(f"## Caller Source Code (relevant sections)\n```python\n{sut_code[:5000]}\n```\n")

    if trace_analysis:
        parts.append(f"## Observed Trace Patterns\n{trace_analysis}\n")

    # Scenario-specific instruction (for test case execution)
    if scenario_instruction:
        parts.append(f"""## SCENARIO INSTRUCTION (for this test)
{scenario_instruction}

Follow this instruction precisely. For example, if told to "return 503 on the 2nd charge",
track the call count and return 503 on exactly the 2nd call to /charges.
""")

    parts.append("""## Rules
1. Return ONLY JSON: {"status": <int>, "body": <object>, "headers": <object>}
2. Track state across requests (created resources, issued tokens, etc.)
3. Match response shapes from traces or source code
4. NO explanation, NO markdown — just JSON
""")

    return "\n".join(parts)


class LLMMock:
    """Online LLM-based mock server supporting all 3 access modes.

    Each incoming HTTP request is answered by the LLM in real-time.
    The model maintains conversation history for cross-request state tracking.
    """

    def __init__(
        self,
        dep_name: str,
        mode: AccessMode = AccessMode.BLACK_BOX,
        sut_code: Optional[str] = None,
        sut_analysis: Optional[str] = None,
        trace_analysis: Optional[str] = None,
        dep_source: Optional[str] = None,
        api_schema: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        timeout: int = 120,
    ):
        self.dep_name = dep_name
        self.mode = mode
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.call_log: List[dict] = []
        self._lock = threading.Lock()
        self._scenario_instruction: Optional[str] = None

        self._base_context = build_context(
            dep_name, mode, sut_code, sut_analysis,
            trace_analysis, dep_source, api_schema,
        )
        self._conversation: List[dict] = [
            {"role": "system", "content": self._base_context}
        ]
        self.app = self._build_app()

    def set_scenario(self, instruction: str):
        """Set a scenario instruction for the next test run."""
        self._scenario_instruction = instruction
        ctx = self._base_context
        if instruction:
            ctx += f"\n## ACTIVE SCENARIO\n{instruction}\n"
        self._conversation = [{"role": "system", "content": ctx}]

    def reset(self):
        """Reset state for next test scenario."""
        self._scenario_instruction = None
        self._conversation = [{"role": "system", "content": self._base_context}]
        self.call_log.clear()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title=f"MIRAGE [{self.mode.value}]: {self.dep_name}")

        @app.get("/__mock__/log")
        async def log():
            return {"calls": self.call_log[-50:], "count": len(self.call_log)}

        @app.get("/__mock__/state")
        async def state():
            return {"type": f"llm-{self.mode.value}", "model": self.model,
                    "dep": self.dep_name, "turns": len(self._conversation)}

        @app.post("/__mock__/reset")
        async def reset():
            self.reset()
            return {"status": "reset"}

        @app.post("/__mock__/scenario")
        async def set_scenario(request: Request):
            body = await request.json()
            instruction = body.get("instruction", "")
            self.set_scenario(instruction)
            with self._lock:
                self.call_log.append({
                    "method": "SCENARIO",
                    "path": "/__mock__/scenario",
                    "status": "-",
                    "duration_ms": 0,
                    "response_body": instruction[:120] if instruction else "",
                })
            return {"status": "scenario_set"}

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        async def catch_all(request: Request, path: str):
            return await self._handle(request, path)

        return app

    async def _handle(self, request: Request, path: str) -> JSONResponse:
        method = request.method
        full_path = f"/{path}"
        query = dict(request.query_params)
        try:
            body = await request.json()
        except Exception:
            body = None
        headers = {k: v for k, v in dict(request.headers).items()
                   if k.lower() in ("authorization", "idempotency-key", "content-type")}

        user_msg = json.dumps({
            "method": method, "path": full_path,
            "query_params": query or None, "body": body,
            "headers": headers or None,
        }, default=str)

        t0 = time.monotonic()
        try:
            self._conversation.append({"role": "user", "content": user_msg})
            if len(self._conversation) > 42:
                self._conversation = [self._conversation[0]] + self._conversation[-40:]

            raw = self._call_llm(self._conversation)

            clean = raw.strip()
            if clean.startswith("```"):
                clean = re.sub(r'^```\w*\n?', '', clean)
                clean = re.sub(r'\n?```$', '', clean)
            if not clean.startswith("{"):
                m = re.search(r'\{[\s\S]*\}', clean)
                if m:
                    clean = m.group(0)

            resp = json.loads(clean)
            self._conversation.append({"role": "assistant", "content": raw})

            status = resp.get("status", 200)
            resp_body = resp.get("body", {})
            resp_headers = resp.get("headers", {})

            body_summary = json.dumps(resp_body, default=str)
            if len(body_summary) > 120:
                body_summary = body_summary[:120] + "..."

            with self._lock:
                self.call_log.append({
                    "method": method, "path": full_path, "status": status,
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                    "response_body": body_summary,
                })

            return JSONResponse(content=resp_body, status_code=status, headers=resp_headers)

        except Exception as e:
            with self._lock:
                self.call_log.append({
                    "method": method, "path": full_path, "status": 500,
                    "error": str(e),
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                    "response_body": None,
                })
            return JSONResponse({"error": "MOCK_ERROR", "detail": str(e)}, status_code=500)

    def _call_llm(self, messages: List[dict]) -> str:
        from mirage.llm import completion
        return completion(
            messages=messages,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            api_key=self.api_key,
            api_base=self.api_base,
        )

    def start(self, port: int = 9001, host: str = "0.0.0.0"):
        import uvicorn
        print(f"MIRAGE [{self.mode.value}] {self.dep_name} -> :{port} (model={self.model})")
        uvicorn.run(self.app, host=host, port=port)
