#!/usr/bin/env python3
"""Demo of the unified MCP server.

Shows how to interact with the agent-toolkit MCP server over stdio.
Run from the repo root:

    python3 demo_mcp_server.py

This demo:
1. Opens a budget task
2. Lists available tools
3. Records a step
4. Checks the next step
5. Creates a work claim
6. Lists claims
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Colors for output
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
RESET = "\033[0m"


def send_request(stdin, stdout, method: str, params: dict, req_id: int) -> dict:
    """Send a JSON-RPC request to the server."""
    message = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id})
    stdin.write(message + "\n")
    stdin.flush()
    response_line = stdout.readline()
    return json.loads(response_line.strip())


def main() -> int:
    """Run the demo."""
    ledger = Path(".agent-toolkit") / "demo-mcp-ledger.json"
    work_ledger = Path(".agent-toolkit") / "demo-work-claims.json"

    # Clean up old ledgers
    ledger.parent.mkdir(exist_ok=True)
    if ledger.exists():
        ledger.unlink()
    if work_ledger.exists():
        work_ledger.unlink()

    print(f"{BLUE}agent-toolkit · Unified MCP Server Demo{RESET}\n")

    # Start the MCP server
    proc = subprocess.Popen(
        [
            "python3",
            "mcp_server.py",
            "--budget-ledger",
            str(ledger),
            "--work-ledger",
            str(work_ledger),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    print("Starting unified MCP server...\n")

    # 1. Initialize
    print(f"{YELLOW}1. Initialize the server{RESET}")
    response = send_request(proc.stdin, proc.stdout, "initialize", {}, 1)
    server_info = response["result"]["serverInfo"]
    print(f"   Server: {server_info['name']} v{server_info['version']}\n")

    # 2. List tools
    print(f"{YELLOW}2. List available tools{RESET}")
    response = send_request(proc.stdin, proc.stdout, "tools/list", {}, 2)
    tools = response["result"]["tools"]
    print(f"   Found {len(tools)} tools:")
    for tool in tools:
        print(f"   • {tool['name']}")
    print()

    # 3. Open a budget task
    print(f"{YELLOW}3. Open a budget task{RESET}")
    response = send_request(
        proc.stdin,
        proc.stdout,
        "tools/call",
        {
            "name": "budget-guard-open",
            "arguments": {
                "task": "demo-task",
                "max_tokens": 2000,
                "max_cost_usd": 0.05,
                "max_tool_calls": 5,
            },
        },
        3,
    )
    result = json.loads(response["result"]["content"][0]["text"])
    decision = result["decision"]
    limits = result["limits"]
    print(f"   Status: {GREEN}{decision}{RESET}")
    print(f"   Max tokens: {limits['max_tokens']:,}")
    print(f"   Max cost: ${limits['max_cost_usd']:.2f}")
    print(f"   Max tool calls: {limits['max_tool_calls']}\n")

    # 4. Record a step
    print(f"{YELLOW}4. Record a step{RESET}")
    response = send_request(
        proc.stdin,
        proc.stdout,
        "tools/call",
        {
            "name": "budget-guard-record",
            "arguments": {
                "task": "demo-task",
                "tokens": 500,
                "cost_usd": 0.01,
                "tool_calls": 1,
                "note": "initial analysis",
            },
        },
        4,
    )
    result = json.loads(response["result"]["content"][0]["text"])
    if "isError" in response["result"] and response["result"]["isError"]:
        print(f"   Error: {result}\n")
    else:
        recorded = result.get("recorded", {})
        print(f"   Recorded: {recorded.get('tokens', 500)} tokens, ${recorded.get('cost_usd', 0.01):.4f}\n")

    # 5. Check the next step
    print(f"{YELLOW}5. Check if next step is allowed{RESET}")
    response = send_request(
        proc.stdin,
        proc.stdout,
        "tools/call",
        {
            "name": "budget-guard-check",
            "arguments": {
                "task": "demo-task",
                "tokens": 800,
                "tool_calls": 2,
            },
        },
        5,
    )
    try:
        result = json.loads(response["result"]["content"][0]["text"])
        decision = result.get("decision", "unknown")
        reason = result.get("reason", "")
        print(f"   Decision: {GREEN}{decision}{RESET}")
        print(f"   Reason: {reason}\n")
    except (KeyError, ValueError):
        print(f"   Response: {response['result']['content'][0]['text']}\n")

    # 6. Work claim: take
    print(f"{YELLOW}6. Claim a work item{RESET}")
    response = send_request(
        proc.stdin,
        proc.stdout,
        "tools/call",
        {
            "name": "work-claim-take",
            "arguments": {
                "item": "fix-flaky-test",
                "owner": "agent-a",
                "ttl": 300,
                "note": "demo task",
            },
        },
        6,
    )
    try:
        result = json.loads(response["result"]["content"][0]["text"])
        ok = result.get("ok", False)
        status = result.get("result", "Unknown")
        print(f"   Status: {GREEN if ok else YELLOW}{status}{RESET}\n")
    except (KeyError, ValueError):
        print(f"   Response: {response['result']['content'][0]['text']}\n")

    print(f"{GREEN}Demo complete!{RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"{YELLOW}Error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)
