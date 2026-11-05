#!/usr/bin/env python3
"""Unified MCP server for agent-toolkit.

Exposes all three tools (budget guard, handoff checker, work-claim ledger)
as a single MCP server over stdio.

Usage:
    python3 mcp_server.py [--root /path/to/root] [--budget-ledger /path] [--work-ledger /path]

Each tool is exposed as a set of resources and tools.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import budget_guard
import handoff_checker
import work_claim

VERSION = "0.1.0"


def _rpc_error(req_id: int | str | None, code: int, message: str) -> dict[str, Any]:
    """JSON-RPC 2.0 error response."""
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": req_id,
    }


def _rpc_success(req_id: int | str | None, result: Any) -> dict[str, Any]:
    """JSON-RPC 2.0 success response."""
    return {
        "jsonrpc": "2.0",
        "result": result,
        "id": req_id,
    }


def handle_initialize(
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handle initialize request."""
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {},
            "resources": {},
        },
        "serverInfo": {
            "name": "agent-toolkit",
            "version": VERSION,
        },
    }


def handle_list_tools() -> dict[str, Any]:
    """List all available tools from the three modules."""
    tools = [
        {
            "name": "budget-guard-open",
            "description": "Open a new budget task with limits on tokens, cost, tool calls, and wall-clock time",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task identifier (alphanumeric, dots, colons, hyphens)",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum tokens allowed",
                    },
                    "max_cost_usd": {
                        "type": "number",
                        "description": "Maximum cost in USD",
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": "Maximum tool calls allowed",
                    },
                    "max_wall_clock_seconds": {
                        "type": "integer",
                        "description": "Maximum wall-clock seconds",
                    },
                    "warn_ratio": {
                        "type": "number",
                        "description": "Ratio at which to warn (0.0-1.0, default 0.8)",
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "budget-guard-check",
            "description": "Check if the next step would exceed budget limits. Returns: continue, warn, or stop-and-ask-human",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task identifier",
                    },
                    "tokens": {
                        "type": "integer",
                        "description": "Tokens for this step",
                    },
                    "cost_usd": {
                        "type": "number",
                        "description": "Cost in USD for this step",
                    },
                    "tool_calls": {
                        "type": "integer",
                        "description": "Tool calls for this step",
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "budget-guard-record",
            "description": "Record the actual spend of the last step",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task identifier",
                    },
                    "tokens": {
                        "type": "integer",
                        "description": "Actual tokens spent",
                    },
                    "cost_usd": {
                        "type": "number",
                        "description": "Actual cost in USD",
                    },
                    "tool_calls": {
                        "type": "integer",
                        "description": "Actual tool calls made",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note about this step (max 200 chars)",
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "budget-guard-status",
            "description": "Get the current status of a budget task",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task identifier",
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "handoff-check",
            "description": "Validate a handoff payload (JSON or Markdown with front matter). Returns: ready, needs-fixes, or blocked",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "string",
                        "description": "Handoff payload as JSON string or Markdown with front matter",
                    },
                    "schema_path": {
                        "type": "string",
                        "description": "Optional path to JSON schema file for additional validation",
                    },
                },
                "required": ["payload"],
            },
        },
        {
            "name": "handoff-template",
            "description": "Get a template for a valid handoff payload",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "work-claim-take",
            "description": "Claim an item for an owner with a time-to-live (lease) duration",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Item identifier",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Owner/agent name",
                    },
                    "ttl": {
                        "type": "integer",
                        "description": "Time-to-live in seconds (default 900, max 86400)",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note about the claim (max 200 chars)",
                    },
                },
                "required": ["item", "owner"],
            },
        },
        {
            "name": "work-claim-renew",
            "description": "Renew a lease on a claimed item",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Item identifier",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Current owner/agent name",
                    },
                    "ttl": {
                        "type": "integer",
                        "description": "New time-to-live in seconds",
                    },
                },
                "required": ["item", "owner"],
            },
        },
        {
            "name": "work-claim-release",
            "description": "Release a claimed item back to the pool",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Item identifier",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Current owner/agent name",
                    },
                },
                "required": ["item", "owner"],
            },
        },
        {
            "name": "work-claim-done",
            "description": "Mark a claimed item as completed",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Item identifier",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Current owner/agent name",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional completion note (max 200 chars)",
                    },
                },
                "required": ["item", "owner"],
            },
        },
        {
            "name": "work-claim-list",
            "description": "List work claims with optional filtering by state",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["open", "active", "expired", "stale", "done", "all"],
                        "description": "Filter by claim state (default: open)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default 50, max 100)",
                    },
                },
            },
        },
    ]
    return tools


def _handoff_document(payload: Any) -> str:
    """Return payload text for ``HandoffChecker.check``.

    ``check()`` accepts ``str | Path``. A string with no newline that does
    not start with ``{`` or ``---`` is opened as a file. This tool's payload
    is always document text, so a path-like string is forced into the
    document parser instead of the filesystem.
    """
    if not isinstance(payload, str):
        raise handoff_checker.HandoffError("payload must be a string")
    leading = payload.lstrip("\ufeff").lstrip()
    if (
        not leading
        or "\n" in payload
        or leading.startswith("{")
        or leading.startswith("---")
    ):
        return payload
    return payload + "\n"


def _schema_under_root(schema_path: Any, root: Path) -> Path | None:
    """Resolve a relative schema path against ``--root``, not the process cwd."""
    if schema_path is None or schema_path == "":
        return None
    if not isinstance(schema_path, str):
        raise handoff_checker.HandoffError("schema_path must be a string")
    path = Path(schema_path)
    if path.is_absolute():
        return path
    return root / path


def call_tool(
    name: str, arguments: dict[str, Any], root: Path, budget_ledger: Path, work_ledger: Path
) -> Any:
    """Call a tool and return the result."""
    try:
        # Budget guard tools
        if name == "budget-guard-open":
            bg = budget_guard.BudgetGuard(budget_ledger)
            return bg.open_task(
                arguments["task"],
                max_tokens=arguments.get("max_tokens"),
                max_cost_usd=arguments.get("max_cost_usd"),
                max_tool_calls=arguments.get("max_tool_calls"),
                max_wall_clock_seconds=arguments.get("max_wall_clock_seconds"),
                warn_ratio=arguments.get("warn_ratio", 0.8),
            )
        elif name == "budget-guard-check":
            bg = budget_guard.BudgetGuard(budget_ledger)
            return bg.check(
                arguments["task"],
                tokens=arguments.get("tokens", 0),
                cost_usd=arguments.get("cost_usd", 0.0),
                tool_calls=arguments.get("tool_calls", 0),
            )
        elif name == "budget-guard-record":
            bg = budget_guard.BudgetGuard(budget_ledger)
            # Same defaults as budget-guard-check. A missing number is 0;
            # passing None makes record() reject an optional argument.
            return bg.record(
                arguments["task"],
                tokens=arguments.get("tokens", 0),
                cost_usd=arguments.get("cost_usd", 0.0),
                tool_calls=arguments.get("tool_calls", 0),
                note=arguments.get("note"),
            )
        elif name == "budget-guard-status":
            bg = budget_guard.BudgetGuard(budget_ledger)
            return bg.status(arguments["task"])

        # Handoff checker tools. check and template are methods on
        # HandoffChecker, not functions on the module.
        elif name == "handoff-check":
            document = _handoff_document(arguments["payload"])
            checker = handoff_checker.HandoffChecker(
                root=root,
                schema=_schema_under_root(arguments.get("schema_path"), root),
            )
            return checker.check(document)
        elif name == "handoff-template":
            return handoff_checker.HandoffChecker().template()

        # Work claim tools
        elif name == "work-claim-take":
            wc = work_claim.WorkLedger(work_ledger)
            return wc.take(
                arguments["item"],
                arguments["owner"],
                ttl_seconds=arguments.get("ttl"),
                note=arguments.get("note", ""),
            )
        elif name == "work-claim-renew":
            wc = work_claim.WorkLedger(work_ledger)
            return wc.renew(
                arguments["item"],
                arguments["owner"],
                ttl_seconds=arguments.get("ttl"),
            )
        elif name == "work-claim-release":
            wc = work_claim.WorkLedger(work_ledger)
            return wc.release(arguments["item"], arguments["owner"])
        elif name == "work-claim-done":
            wc = work_claim.WorkLedger(work_ledger)
            return wc.done(
                arguments["item"],
                arguments["owner"],
                note=arguments.get("note", ""),
            )
        elif name == "work-claim-list":
            wc = work_claim.WorkLedger(work_ledger)
            return wc.list_claims(
                state=arguments.get("state", "open"),
                limit=arguments.get("limit", work_claim.DEFAULT_LIMIT),
            )

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        raise ValueError(f"Tool {name} failed: {str(e)}")


def handle_call_tool(
    params: dict[str, Any], root: Path, budget_ledger: Path, work_ledger: Path
) -> dict[str, Any]:
    """Handle tool call requests."""
    name = params.get("name")
    arguments = params.get("arguments", {})

    try:
        result = call_tool(name, arguments, root, budget_ledger, work_ledger)
        return {"type": "tool_result", "content": [{"type": "text", "text": json.dumps(result)}]}
    except Exception as e:
        return {"type": "tool_result", "content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}


def handle_message(
    message: dict[str, Any], root: Path, budget_ledger: Path, work_ledger: Path
) -> dict[str, Any] | None:
    """Handle incoming JSON-RPC message."""
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "invalid request")

    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params", {})

    try:
        if method == "initialize":
            return _rpc_success(req_id, handle_initialize(params))
        elif method == "tools/list":
            return _rpc_success(req_id, {"tools": handle_list_tools()})
        elif method == "tools/call":
            result = handle_call_tool(params, root, budget_ledger, work_ledger)
            return _rpc_success(req_id, result)
        elif method == "notifications/initialized":
            # Ignore initialization notification
            return None
        else:
            return _rpc_error(req_id, -32601, f"method not found: {method}")

    except Exception as e:
        sys.stderr.write(f"agent-toolkit: {str(e)}\n")
        return _rpc_error(req_id, -32603, "internal error")


def serve(root: Path, budget_ledger: Path, work_ledger: Path) -> int:
    """Speak MCP over stdio. One JSON object per line."""
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = _rpc_error(None, -32700, "parse error")
        else:
            try:
                response = handle_message(message, root, budget_ledger, work_ledger)
            except Exception as exc:  # noqa: BLE001 - keep the stdio loop alive
                req_id = message.get("id") if isinstance(message, dict) else None
                sys.stderr.write(f"agent-toolkit: {exc}\n")
                response = _rpc_error(req_id, -32603, "internal error")

        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Unified MCP server for agent-toolkit",
        prog="mcp_server.py",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Root directory for relative paths (default: current directory)",
    )
    parser.add_argument(
        "--budget-ledger",
        type=Path,
        default=Path(".agent-toolkit") / "budget-ledger.json",
        help="Path to budget guard ledger file",
    )
    parser.add_argument(
        "--work-ledger",
        type=Path,
        default=Path(".agent-toolkit") / "work-claims.json",
        help="Path to work-claim ledger file",
    )

    args = parser.parse_args()

    # Resolve relative paths against root
    root = args.root.resolve()
    budget_ledger = args.budget_ledger if args.budget_ledger.is_absolute() else root / args.budget_ledger
    work_ledger = args.work_ledger if args.work_ledger.is_absolute() else root / args.work_ledger

    return serve(root, budget_ledger, work_ledger)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
