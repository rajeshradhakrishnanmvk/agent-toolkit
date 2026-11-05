#!/usr/bin/env python3
"""Quick reference for the agent-toolkit MCP server.

This file shows common patterns for using the MCP server.
"""

# ============================================================================
# QUICK REFERENCE: Agent-Toolkit MCP Server
# ============================================================================

# START THE SERVER
# ----------------
# python3 mcp_server.py
#
# WITH CUSTOM PATHS
# python3 mcp_server.py --root /path --budget-ledger /path/budget.json --work-ledger /path/work.json

# JSON-RPC 2.0 MESSAGE STRUCTURE
# --------------------------------
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "TOOL_NAME",
#     "arguments": { ... }
#   },
#   "id": 1
# }

# ============================================================================
# BUDGET GUARD EXAMPLES
# ============================================================================

# 1. OPEN A BUDGET TASK
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "budget-guard-open",
#     "arguments": {
#       "task": "my-task",
#       "max_tokens": 5000,
#       "max_cost_usd": 0.10,
#       "max_tool_calls": 10,
#       "warn_ratio": 0.8
#     }
#   },
#   "id": 1
# }

# 2. CHECK IF A STEP IS ALLOWED
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "budget-guard-check",
#     "arguments": {
#       "task": "my-task",
#       "tokens": 1000,
#       "cost_usd": 0.02,
#       "tool_calls": 2
#     }
#   },
#   "id": 2
# }
# Response decision: "continue", "warn", or "stop-and-ask-human"

# 3. RECORD ACTUAL SPEND
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "budget-guard-record",
#     "arguments": {
#       "task": "my-task",
#       "tokens": 950,
#       "cost_usd": 0.019,
#       "tool_calls": 2,
#       "note": "completed analysis phase"
#     }
#   },
#   "id": 3
# }

# 4. GET TASK STATUS
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "budget-guard-status",
#     "arguments": {
#       "task": "my-task"
#     }
#   },
#   "id": 4
# }

# ============================================================================
# HANDOFF CHECKER EXAMPLES
# ============================================================================

# 1. GET HANDOFF TEMPLATE
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "handoff-template",
#     "arguments": {}
#   },
#   "id": 5
# }

# 2. VALIDATE A HANDOFF
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "handoff-check",
#     "arguments": {
#       "payload": "---\ngoal: Complete the task\nacceptance_criteria: Code passes tests\nowner: agent-b\nconstraints: Use only stdlib\nbudget_left: 1000 tokens\n---\n\nDetailed description...",
#       "schema_path": "/path/to/schema.json"  # optional
#     }
#   },
#   "id": 6
# }
# Response status: "ready", "needs-fixes", or "blocked"

# ============================================================================
# WORK CLAIM EXAMPLES
# ============================================================================

# 1. CLAIM AN ITEM
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "work-claim-take",
#     "arguments": {
#       "item": "task-123",
#       "owner": "agent-a",
#       "ttl": 600,
#       "note": "Starting work on this task"
#     }
#   },
#   "id": 7
# }
# Response: {"ok": true, "result": "taken"}

# 2. RENEW A LEASE
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "work-claim-renew",
#     "arguments": {
#       "item": "task-123",
#       "owner": "agent-a",
#       "ttl": 600
#     }
#   },
#   "id": 8
# }

# 3. RELEASE AN ITEM
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "work-claim-release",
#     "arguments": {
#       "item": "task-123",
#       "owner": "agent-a"
#     }
#   },
#   "id": 9
# }

# 4. MARK ITEM AS DONE
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "work-claim-done",
#     "arguments": {
#       "item": "task-123",
#       "owner": "agent-a",
#       "note": "Task completed successfully"
#     }
#   },
#   "id": 10
# }

# 5. LIST CLAIMS
# {
#   "jsonrpc": "2.0",
#   "method": "tools/call",
#   "params": {
#     "name": "work-claim-list",
#     "arguments": {
#       "state": "all",  # or "open", "active", "expired", "stale", "done"
#       "limit": 50
#     }
#   },
#   "id": 11
# }

# ============================================================================
# CLAUDE DESKTOP CONFIGURATION
# ============================================================================
# Add to ~/.config/Claude/claude_desktop_config.json (or appropriate path):
# 
# {
#   "mcpServers": {
#     "agent-toolkit": {
#       "command": "python3",
#       "args": [
#         "/absolute/path/to/agent-toolkit/mcp_server.py",
#         "--root",
#         "/absolute/path/to/your/project"
#       ]
#     }
#   }
# }

# ============================================================================
# TESTING
# ============================================================================
# Run the demo:
#   python3 demo_mcp_server.py
#
# Run all tests:
#   python3 -m unittest discover -s tests -v

# ============================================================================
# DOCUMENTATION
# ============================================================================
# - Full API reference: MCP_SERVER.md
# - Integration guide: README.md (section: Unified MCP server)
# - Example config: mcp_server_config.example.json
