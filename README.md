# MIRAGE

**Microservice Integration Runtime Agent for Generative Emulation**

MIRAGE uses LLMs to simulate microservice dependencies at test time. Instead of writing mock servers by hand or relying on brittle record-replay, MIRAGE keeps an LLM in the loop: each HTTP request to a mocked dependency is answered by the model in real-time, while it maintains cross-request state throughout the test scenario.

**Paper:** [MIRAGE: LLM-Powered Simulation of Microservice Dependencies for Integration Testing](https://arxiv.org/abs/2604.04806)

---

## Key Idea

Traditional approaches to mocking microservice dependencies — record-replay, hand-written stubs, contract-based generators — struggle with **stateful behavior**: token lifecycles, optimistic locking, saga compensation, async polling. These require cross-request reasoning that static mocks cannot provide.

MIRAGE takes a different approach: give the LLM the dependency's source code (or just production traces), and let it simulate the dependency's behavior on every request. The model reads context from three signals:

| Signal | Source | Example |
|--------|--------|---------|
| **Dependency source** | Git repo | `payment_service/main.py` |
| **Caller source** | Git repo | How the SUT calls this dependency |
| **Production traces** | OpenTelemetry | Status code distributions, call sequences |

## Operating Modes

| Mode | What MIRAGE sees | When to use |
|------|-----------------|-------------|
| **White-box** | Dependency source + caller + traces | Internal services with source access |
| **Grey-box** | API schema + caller + traces | Services with OpenAPI specs |
| **Black-box** | Caller code + traces only | Third-party APIs, legacy services |

## Results

Evaluated on 110 scenarios across 3 benchmark systems (14 caller-dependency pairs):

| Approach | Status Fidelity | Body-Shape Fidelity |
|----------|:-:|:-:|
| Record-Replay | 62% | 16% |
| Pattern-Mining | 70% | 26% |
| Contract IR v1 | 77% | 55% |
| Contract IR v2 | 75% | 56% |
| **MIRAGE (black-box)** | **94%** | **75%** |
| **MIRAGE (white-box)** | **99%** | **99%** |

---

## Installation

```bash
pip install mirage-mock
```

Or from source:

```bash
git clone https://github.com/xinranzhang/mirage.git
cd mirage
pip install -e ".[dev,otel]"
```

## Quick Start

### 1. Set your LLM API key

MIRAGE uses [LiteLLM](https://github.com/BerriAI/litellm) as a universal LLM gateway, supporting 100+ providers (OpenAI, Anthropic, AWS Bedrock, Azure, Google, Ollama, etc.).

```bash
# Option A: Anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# Option B: OpenAI
export OPENAI_API_KEY=sk-...
export MIRAGE_MODEL=gpt-4o

# Option C: AWS Bedrock
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION_NAME=us-east-1
export MIRAGE_MODEL=bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0

# Option D: Local (Ollama)
export MIRAGE_MODEL=ollama/llama3
export MIRAGE_API_BASE=http://localhost:11434
```

### 2. Auto-discover services

```bash
mirage init --codebase ./services --traces ./trace-data
```

This scans your OpenTelemetry traces and source code to generate `mirage.yaml`:
- Identifies the caller (SUT) and its dependencies
- Maps service names to source files
- Detects communication patterns

### 3. Start mock servers

```bash
mirage serve --config mirage.yaml
```

Each dependency gets its own mock server on a dedicated port. Point your SUT's environment variables to these ports and run your tests.

### 4. Run tests

```bash
# Point SUT to mock ports
export INVENTORY_URL=http://localhost:9001
export PAYMENT_URL=http://localhost:9002

# Run your tests as usual
pytest tests/
```

---

## Python API

```python
from mirage.mockgen.llm_backend import LLMMock, AccessMode
from mirage.analyzer.trace_analyzer import analyze_traces, format_trace_analysis
from mirage.analyzer.sut_analyzer import analyze_sut, format_analysis

# Analyze traces and SUT code
trace_profiles = analyze_traces("./trace-data", sut_service="order-service")
sut_deps = analyze_sut("./services/order_service/main.py")

# Create a mock server
mock = LLMMock(
    dep_name="payment-service",
    mode=AccessMode.WHITE_BOX,
    dep_source=open("services/payment_service/main.py").read(),
    trace_analysis=format_trace_analysis(trace_profiles),
    model="claude-sonnet-4-5-20241022",  # or any litellm-supported model
)

# Start serving
mock.start(port=9002)
```

### Mock Control Endpoints

Every mock server exposes control endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/__mock__/reset` | POST | Clear conversation state for next scenario |
| `/__mock__/scenario` | POST | Inject scenario instruction (e.g., "return 503 on 2nd call") |
| `/__mock__/log` | GET | View recent call history |
| `/__mock__/state` | GET | Inspect mock configuration |

### Test Scenario Generation

```python
from mirage.testgen.scenario_gen import generate_scenarios, format_scenarios

scenarios = generate_scenarios(
    sut_code=open("services/order_service/main.py").read(),
    sut_analysis=format_analysis(sut_deps),
    trace_analysis=format_trace_analysis(trace_profiles),
)
print(format_scenarios(scenarios))
```

---

## Project Structure

```
mirage/
├── mirage/                          # Core Python package
│   ├── __init__.py                  # Package metadata
│   ├── cli.py                       # CLI entry point (init, serve, info)
│   ├── config.py                    # YAML config loader + dataclasses
│   ├── discover.py                  # Auto-discovery from traces + codebase
│   ├── llm.py                       # Unified LLM interface (via litellm)
│   ├── proxy.py                     # SUT traffic routing utilities
│   ├── analyzer/
│   │   ├── sut_analyzer.py          # Static analysis of SUT source code
│   │   └── trace_analyzer.py        # OTel trace behavioral extraction
│   ├── mockgen/
│   │   └── llm_backend.py           # Online per-request LLM mock server
│   └── testgen/
│       ├── scenario_gen.py          # LLM-based test scenario generation
│       └── test_runner.py           # Async test executor + reporting
│
├── examples/
│   └── order_system/                # Complete 4-service demo
│       ├── mirage.yaml              # Example configuration
│       ├── docker-compose.yml       # Run real services for comparison
│       ├── trace-data/              # Pre-collected OTel traces
│       └── services/                # Source code for all 4 services
│           ├── order_service/       # SUT: saga orchestration
│           ├── inventory_service/   # Dependency: optimistic locking, 409s
│           ├── payment_service/     # Dependency: token auth, retries
│           └── shipping_service/    # Dependency: async polling (202)
│
├── pyproject.toml                   # Package configuration
├── LICENSE                          # Apache 2.0
└── README.md
```

### Architecture

```
                    ┌─────────────────────────────────┐
                    │         Context Builder          │
                    │  ┌───────┐ ┌──────┐ ┌────────┐  │
                    │  │  Dep  │ │Caller│ │ Trace  │  │
                    │  │Source │ │Source│ │Summary │  │
                    │  └───┬───┘ └──┬───┘ └───┬────┘  │
                    │      └────────┼─────────┘       │
                    │               ▼                  │
                    │        System Prompt             │
                    └───────────────┬──────────────────┘
                                    │
                                    ▼
  ┌──────────┐    HTTP     ┌──────────────────┐   LLM API   ┌─────────┐
  │   SUT    │ ──────────► │  MIRAGE Server   │ ──────────► │  LLM    │
  │ (caller) │ ◄────────── │  (FastAPI mock)  │ ◄────────── │ (any)   │
  └──────────┘   response  │                  │   JSON      └─────────┘
                           │ Conversation Hx  │
                           │ [msg1, msg2, ..] │
                           └──────────────────┘
```

---

## Configuration

### `mirage.yaml`

```yaml
project:
  name: my-project
  codebase: ./services
  traces: ./trace-data

sut:
  name: order-service
  module: services.order_service.main:app
  port: 8000

dependencies:
  payment-service:
    source: services/payment_service/main.py
    traces: trace-data/payment-service.jsonl
    port: 8002
    mock_port: 9002

mock:
  backend: llm
  model: claude-sonnet-4-5-20241022
  temperature: 0.1
  max_tokens: 2048
  timeout: 120
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MIRAGE_MODEL` | LLM model identifier | `claude-sonnet-4-5-20241022` |
| `MIRAGE_API_KEY` | API key (overrides provider-specific keys) | — |
| `MIRAGE_API_BASE` | Custom API endpoint URL | — |

Plus any provider-specific variables supported by [LiteLLM](https://docs.litellm.ai/docs/providers):
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.

---

## Example: Order System

The `examples/order_system/` directory contains a complete 4-service microservice system demonstrating complex interaction patterns:

- **Order Service** (SUT): Saga orchestration — reserve inventory, charge payment, create shipment, confirm
- **Inventory Service**: Optimistic locking with version conflicts (409)
- **Payment Service**: Token-based auth with refresh (401), charge with retry on 503, decline (422)
- **Shipping Service**: Async processing (202) with polling for completion

```bash
cd examples/order_system

# Start mock dependencies
mirage serve --config mirage.yaml

# In another terminal, start the SUT pointing to mocks
INVENTORY_URL=http://localhost:9001 \
PAYMENT_URL=http://localhost:9002 \
SHIPPING_URL=http://localhost:9003 \
uvicorn services.order_service.main:app --port 8000

# Test it
curl -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"item_id": "item-001", "quantity": 1, "card_last4": "4242", "shipping_address": "123 Main St"}'
```

---

## Citation

If you use MIRAGE in your research, please cite:

```bibtex
@article{zhang2025mirage,
  title={MIRAGE: LLM-Powered Simulation of Microservice Dependencies for Integration Testing},
  author={Zhang, Xinran},
  journal={arXiv preprint arXiv:2604.04806},
  year={2025}
}
```

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
