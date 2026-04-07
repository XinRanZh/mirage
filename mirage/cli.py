#!/usr/bin/env python3
"""MIRAGE CLI — LLM-powered dependency simulation for E2E testing.

Commands:
    mirage init     Discover services and generate mirage.yaml
    mirage serve    Start all mock servers
    mirage validate Compare mock behavior against real services
    mirage info     Show discovered services and config
"""

from __future__ import annotations

import argparse
import os
import sys


def cmd_init(args):
    """Discover services and generate mirage.yaml."""
    from .discover import auto_generate_config

    codebase = args.codebase or "."
    traces = args.traces or "trace-data"
    model = args.model

    print(f"Discovering services from traces={traces}, codebase={codebase}...")
    yaml_content = auto_generate_config(traces, codebase, model=model)

    output = args.output or "mirage.yaml"
    with open(output, "w") as f:
        f.write(yaml_content)

    print(f"Config written to {output}")
    print(f"Edit {output} to adjust settings, then run: mirage serve")


def cmd_serve(args):
    """Start all mock servers."""
    from .config import load_config

    config = load_config(args.config)

    print(f"Starting {len(config.dependencies)} mock server(s) (model: {config.mock.model})...")

    # Import and start mock servers
    from .mockgen.llm_backend import LLMMock, AccessMode

    import uvicorn
    import threading

    servers = {}
    for name, dep in config.dependencies.items():
        dep_source = None
        if dep.source and os.path.exists(dep.source):
            with open(dep.source) as f:
                dep_source = f.read()

        trace_analysis = None
        if dep.traces and os.path.exists(os.path.dirname(dep.traces)):
            from .analyzer.trace_analyzer import analyze_traces, format_trace_analysis
            traces_dir = os.path.dirname(dep.traces)
            profiles = analyze_traces(traces_dir)
            trace_analysis = format_trace_analysis(profiles)

        mode = AccessMode.WHITE_BOX if dep_source else AccessMode.BLACK_BOX
        mock = LLMMock(
            dep_name=name,
            mode=mode,
            dep_source=dep_source,
            trace_analysis=trace_analysis,
            model=config.mock.model,
            api_key=config.mock.api_key,
            api_base=config.mock.api_base,
            temperature=config.mock.temperature,
            max_tokens=config.mock.max_tokens,
            timeout=config.mock.timeout,
        )
        servers[name] = (mock, dep.mock_port)

    print(f"\nMock servers:")
    for name, (mock, port) in servers.items():
        print(f"  {name}: http://localhost:{port} [{mock.mode.value}]")

    # Start all servers in threads
    threads = []
    for name, (mock, port) in servers.items():
        t = threading.Thread(
            target=uvicorn.run,
            args=(mock.app,),
            kwargs={"host": "0.0.0.0", "port": port, "log_level": "warning"},
            daemon=True,
        )
        t.start()
        threads.append(t)

    print(f"\nPress Ctrl+C to stop all mock servers.")
    try:
        import signal
        signal.pause()
    except KeyboardInterrupt:
        print("\nStopping...")


def cmd_info(args):
    """Show discovered services and current config."""
    config_path = args.config
    if os.path.exists(config_path):
        from .config import load_config
        config = load_config(config_path)
        print(f"Project: {config.project_name}")
        print(f"Model: {config.mock.model}")
        print(f"\nSUT: {config.sut.name} (port {config.sut.port})")
        print(f"\nDependencies:")
        for name, dep in config.dependencies.items():
            print(f"  {name}:")
            print(f"    source: {dep.source}")
            print(f"    real port: {dep.port}")
            print(f"    mock port: {dep.mock_port}")
    else:
        from .discover import discover_services
        traces = args.traces or "trace-data"
        codebase = args.codebase or "services"
        services = discover_services(traces, codebase)

        print(f"Discovered {len(services)} service(s):\n")
        for name, svc in sorted(services.items()):
            role = "CALLER" if svc.is_caller else "dependency"
            print(f"  {name} ({role}):")
            print(f"    port: {svc.port}")
            print(f"    source: {svc.source_path or 'not found'}")
            print(f"    traces: {svc.trace_path or 'not found'}")
            print(f"    endpoints: {len(svc.endpoints)}")
            if svc.calls_to:
                print(f"    calls: {', '.join(svc.calls_to)}")
            print()


def main():
    parser = argparse.ArgumentParser(
        prog="mirage",
        description="MIRAGE: LLM-powered dependency simulation for microservice integration testing",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Discover services and generate config")
    p_init.add_argument("--codebase", default="services", help="Path to service source code")
    p_init.add_argument("--traces", default="trace-data", help="Path to OTel trace JSONL files")
    p_init.add_argument("--model", default=None, help="LLM model (default: claude-sonnet-4-5-20241022)")
    p_init.add_argument("--output", default="mirage.yaml")

    # serve
    p_serve = sub.add_parser("serve", help="Start all mock servers")
    p_serve.add_argument("--config", default="mirage.yaml")

    # info
    p_info = sub.add_parser("info", help="Show service info")
    p_info.add_argument("--config", default="mirage.yaml")
    p_info.add_argument("--traces", default="trace-data")
    p_info.add_argument("--codebase", default="services")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "info":
        cmd_info(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
