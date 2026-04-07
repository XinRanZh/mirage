"""MIRAGE — Microservice Integration Runtime Agent for Generative Emulation.

LLM-powered dependency simulation for microservice integration testing.

Modes:
  BLACK_BOX: Caller code + traces (no dependency source code)
  GREY_BOX:  Caller code + traces + API schema
  WHITE_BOX: Dependency source code + caller code + traces

Usage:
    # CLI
    mirage init --codebase ./src --traces ./traces
    mirage serve
    mirage test

    # Python API
    from mirage.mockgen.llm_backend import LLMMock, AccessMode
    from mirage.testgen.scenario_gen import generate_scenarios
    from mirage.analyzer.sut_analyzer import analyze_sut
    from mirage.analyzer.trace_analyzer import analyze_traces
"""

__version__ = "0.1.0"
