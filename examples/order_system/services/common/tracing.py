"""Common OpenTelemetry tracing setup for all services.

Supports 3 export modes (set via env vars):
  OTEL_EXPORTER_OTLP_ENDPOINT  → OTLP gRPC to a collector
  OTEL_FILE_EXPORT=true        → append spans as JSONL to trace-data/<service>.jsonl
  OTEL_CONSOLE_EXPORT=true     → print spans to console
"""

import json
import os
import threading
from typing import Sequence

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor


# ── JSONL File Exporter ─────────────────────────────────

class JSONLFileExporter(SpanExporter):
    """Export spans as one-JSON-per-line to a file. Thread-safe."""

    def __init__(self, file_path: str):
        self._path = file_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = []
        for span in spans:
            rec = _span_to_dict(span)
            lines.append(json.dumps(rec, ensure_ascii=False, default=str))

        with self._lock:
            with open(self._path, "a") as f:
                for line in lines:
                    f.write(line + "\n")

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def _span_to_dict(span: ReadableSpan) -> dict:
    """Convert a ReadableSpan to a flat dict suitable for analysis."""
    ctx = span.context
    parent = span.parent

    rec = {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": format(parent.span_id, "016x") if parent else None,
        "name": span.name,
        "kind": span.kind.name if span.kind else None,
        "service": _get_service_name(span),
        "start_time": span.start_time,
        "end_time": span.end_time,
        "duration_ns": (span.end_time - span.start_time) if span.end_time and span.start_time else None,
        "status_code": span.status.status_code.name if span.status else None,
        "status_description": span.status.description if span.status else None,
        "attributes": dict(span.attributes) if span.attributes else {},
        "events": [
            {
                "name": e.name,
                "timestamp": e.timestamp,
                "attributes": dict(e.attributes) if e.attributes else {},
            }
            for e in (span.events or [])
        ],
    }

    # Extract HTTP-specific attributes for easier analysis
    attrs = rec["attributes"]
    if "http.method" in attrs:
        rec["http_method"] = attrs["http.method"]
    if "http.url" in attrs:
        rec["http_url"] = attrs["http.url"]
    if "http.target" in attrs:
        rec["http_target"] = attrs["http.target"]
    if "http.route" in attrs:
        rec["http_route"] = attrs["http.route"]
    if "http.status_code" in attrs:
        rec["http_status_code"] = attrs["http.status_code"]

    return rec


def _get_service_name(span: ReadableSpan) -> str:
    if span.resource and span.resource.attributes:
        return span.resource.attributes.get("service.name", "unknown")
    return "unknown"


# ── Setup ───────────────────────────────────────────────

def setup_tracing(service_name: str, app=None):
    """Initialize OTel tracing with configurable exporters."""
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    # OTLP exporter (for Docker / collector)
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    # JSONL file exporter (for local dev — no collector needed)
    trace_dir = os.environ.get("OTEL_FILE_EXPORT_DIR")
    if os.environ.get("OTEL_FILE_EXPORT", "false").lower() == "true":
        if not trace_dir:
            # Resolve from __file__ using abspath to handle different working dirs
            _this = os.path.abspath(__file__)
            trace_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(_this))),
                "trace-data",
            )
        file_path = os.path.join(trace_dir, f"{service_name}.jsonl")
        provider.add_span_processor(BatchSpanProcessor(JSONLFileExporter(file_path)))

    # Console exporter (for debugging)
    if os.environ.get("OTEL_CONSOLE_EXPORT", "false").lower() == "true":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)

    # Instrument FastAPI
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    # Instrument httpx (for outgoing calls)
    HTTPXClientInstrumentor().instrument()

    return trace.get_tracer(service_name)
