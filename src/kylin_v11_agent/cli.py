# -*- coding: utf-8 -*-
"""Tiny CLI for the Kylin V11 Agent (no third-party deps).

Examples (run from projects/os_agent_memory):
    py -3 -m src.kylin_v11_agent.cli run --description "check kernel version"
    py -3 -m src.kylin_v11_agent.cli plan --description "show disk usage"
    py -3 -m src.kylin_v11_agent.cli tools
    py -3 -m src.kylin_v11_agent.cli report
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .agent import AgentConfig, KylinAgent
from .memory import MemoryConfig


def _build_agent(args: argparse.Namespace) -> KylinAgent:
    mem_cfg = MemoryConfig(
        db_path=args.db_path,
        markdown_mirror=args.markdown_mirror,
        markdown_path=args.markdown_path,
    )
    cfg = AgentConfig(
        agent_id=args.agent_id,
        allow_write=args.allow_write,
        memory_config=mem_cfg,
        task_timeout_s=args.task_timeout,
    )
    return KylinAgent(cfg)


def cmd_run(args: argparse.Namespace) -> int:
    agent = _build_agent(args)
    try:
        report = agent.run(args.description)
    finally:
        # Always print the agent report so callers can see audit stats.
        agent_summary = agent.get_report()
        print("--- agent summary ---")
        print(json.dumps(agent_summary, ensure_ascii=False, indent=2))
        agent.close()
    print("--- task report ---")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.success else 1


def cmd_tools(args: argparse.Namespace) -> int:
    agent = _build_agent(args)
    try:
        for name in agent.registry.names():
            spec = agent.registry.spec(name)
            print(f"{name}\t{spec.binary}\twrite={spec.write}\t"
                  f"timeout={spec.timeout_s}s\tallow={spec.allow_args}")
    finally:
        agent.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    agent = _build_agent(args)
    try:
        print(json.dumps(agent.get_report(), ensure_ascii=False, indent=2))
    finally:
        agent.close()
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    agent = _build_agent(args)
    try:
        plan = agent._plan(args.description, argv_override={})  # noqa: SLF001 (CLI is local)
    finally:
        agent.close()
    for tool, argv in plan:
        print(f"{tool} {' '.join(argv) if argv else '<default-argv>'}")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="kylin_v11_agent", description=__doc__)
    parser.add_argument("--agent-id", default="kylin_v11_agent")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--markdown-mirror", action="store_true")
    parser.add_argument("--markdown-path", default="")
    parser.add_argument("--task-timeout", type=float, default=60.0)

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run an Agent task")
    p_run.add_argument("--description", required=True)
    p_run.set_defaults(func=cmd_run)

    p_plan = sub.add_parser("plan", help="Show the planner output for a description")
    p_plan.add_argument("--description", required=True)
    p_plan.set_defaults(func=cmd_plan)

    sub.add_parser("tools", help="List available tools").set_defaults(func=cmd_tools)
    sub.add_parser("report", help="Show agent report").set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())