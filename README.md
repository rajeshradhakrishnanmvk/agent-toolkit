# agent-toolkit

One small, single-file tool for AI agent builders, every week.

Built in public by Rajesh (Kochi). Each tool is announced on LinkedIn with a short screen recording. The project page is [https://rajeshradhakrishnanmvk.github.io/agent-toolkit/](https://rajeshradhakrishnanmvk.github.io/agent-toolkit/).

| Week | Tool | Status |
| --- | --- | --- |
| 1 | [Budget guard](#week-1--budget-guard) | Live |
| 2 | [Handoff checker](#week-2--handoff-checker) | Live |
| 3 | [Work-claim ledger](#week-3--work-claim-ledger) | Live |
| 4 | [Approval ledger](#week-4--approval-ledger) | Live |

The page is static HTML in `/docs`. GitHub Pages: branch `main`, folder `/docs`.

## Week 1 — Budget guard

Agents (crews, coding agents, MCP-tool bots) burn tokens, dollars, and tool calls on long tasks. The human usually finds out afterwards.

`budget_guard.py` lets the harness declare a budget for one task and check in before each costly step. The answer is `continue`, `warn`, or `stop-and-ask-human`. Totals live in a JSON file, so they survive a restart.

It is one file, standard library only. Use it as a Python module, a CLI, or a local MCP server.

The guard reports a decision. It does not kill your process. When the decision is `stop-and-ask-human`, your harness should refuse the next step and show `ask_human` to the person. Week 4 can store that question and the later answer. Storing an approval does not raise this budget or run the next step.

### Decisions

| Decision | Meaning |
| --- | --- |
| `continue` | Every configured limit is under the warn line. |
| `warn` | At least one limit is at or past the warn ratio (default 80%). Still ok to proceed. |
| `stop-and-ask-human` | The step would pass a limit, or a limit is already past. Do not spend more until a human says so. |

Any one of tokens, US dollars, tool calls, or wall-clock can trip the guard. Unset limits are not checked. Wall-clock is real time since `open`, including idle time, and it keeps moving across restarts.

`check` does not spend. Two checks do not stack: each one is a hypothetical on the committed total. `record` writes what actually happened, even if that total is over budget.

`step` does both in one call: it records the last step's real usage, then checks the next step against the new total. Pass the tokens, dollars, and tool calls that just happened, and `next_*` for the step you are about to take. The decision is about that next step. This is the call to use when the model would otherwise record a turn late. `check` and `record` stay as they are.

### 30-second quickstart

```bash
python3 budget_guard.py open fix-flaky-test \
  --max-tokens 2500 --max-cost-usd 0.02 --max-tool-calls 4

python3 budget_guard.py check fix-flaky-test --tokens 600 --tool-calls 1
python3 budget_guard.py record fix-flaky-test --tokens 600 --tool-calls 1 --note read_failures
python3 budget_guard.py status fix-flaky-test
```

To record the last step and check the next one together:

```bash
python3 budget_guard.py step fix-flaky-test --tokens 600 --next-tokens 400
```

`check`, `record`, `step`, and `status` exit `0` on continue or warn, `3` on stop-and-ask-human, and `1` on a real error. Add `--json` for the machine-readable decision. The CLI JSON is the full decision. Over MCP the same decision is the short form: `decision`, `reason`, `spent`, `remaining`, and `limits` (plus `recorded` and `next` on `step`).

```bash
python3 demo.py
```

That demo is the week-1 recording: a four-step loop that is allowed, warned, then stopped. Step 4 is not written to the ledger.

### As a library

```python
from budget_guard import STOP_AND_ASK, BudgetGuard

guard = BudgetGuard(".agent-toolkit/budget-ledger.json")
guard.open_task(
    "fix-flaky-test",
    max_tokens=2500,
    max_cost_usd=0.02,
    max_tool_calls=4,
)

decision = guard.check("fix-flaky-test", tokens=600, tool_calls=1)
if decision["decision"] == "warn":
    print(decision["reason"])  # near the limit; still ok to proceed
if decision["decision"] == STOP_AND_ASK:
    raise SystemExit(decision["ask_human"])

# ...do the step, then record what it actually cost...
guard.record("fix-flaky-test", tokens=600, tool_calls=1, note="read_failures")

# Or, instead of record plus a later check, record and check together:
# guard.step("fix-flaky-test", tokens=600, tool_calls=1, next_tokens=400)
```

`open_task` is the method name because `open` is a Python builtin. The CLI and MCP tool are both called `open` / `budget_open`.

### As an MCP server

```bash
python3 budget_guard.py serve --ledger /absolute/path/to/budget-ledger.json
```

Stdio transport, one JSON-RPC message per line (the same shape Cursor and Claude Desktop speak). Tools: `budget_open`, `budget_check`, `budget_record`, `budget_step`, `budget_status`.

The server speaks protocol versions `2024-11-05`, `2025-03-26`, and `2025-06-18`. A client that asks for any other version, including a newer one, gets `2025-06-18`. An unknown tool name is a JSON-RPC `-32602` error, not an `isError` tool result.

```json
{
  "mcpServers": {
    "budget-guard": {
      "command": "python3",
      "args": [
        "/absolute/path/to/agent-toolkit/budget_guard.py",
        "serve",
        "--ledger",
        "/absolute/path/to/agent-toolkit/.agent-toolkit/budget-ledger.json"
      ]
    }
  }
}
```

A relative `--ledger`, `$BUDGET_GUARD_LEDGER`, or per-call `ledger_path` resolves against `--root`, not the process working directory. Pass an absolute `--root` when the client does not start the server in the project directory. An absolute ledger path is used as given. A relative `root` on a tool call stays under the server `--root`.

### Ledger

Default path: `.agent-toolkit/budget-ledger.json` under `--root`, or `$BUDGET_GUARD_LEDGER`. A relative `$BUDGET_GUARD_LEDGER` may not climb above `--root`. An explicit `--ledger` may.

The file is pretty-printed JSON. A sibling `budget-ledger.json.lock` serializes readers and writers. Writes go to a temp file and replace the ledger, so a crash mid-write keeps the previous totals. The recent 100 events stay in the file; older ones are counted in `events_dropped` and the totals still include them.

This repo gitignores `.agent-toolkit/`. If you point the ledger somewhere else, don't commit it. Notes you pass to `record` are stored in plain text.

### Pricing

There is no built-in price list. Providers change prices, and the same model name can cost different amounts on different hosts. Pass `cost_usd` yourself.

To turn token counts into dollars, pass rates you looked up. The numbers below are an example, not a quote:

```bash
python3 budget_guard.py cost \
  --input-tokens 2000 --output-tokens 500 \
  --usd-per-million-input 3 --usd-per-million-output 15
```

```python
from budget_guard import estimate_cost_usd

estimate_cost_usd(2000, 500, usd_per_million_input=3, usd_per_million_output=15)
# 0.0135, from those rates only
```

The CLI, the library, and the MCP server do not make network calls.

## Week 2 — Handoff checker

When one agent hands work to the next, the payload often drops the goal, the acceptance criteria, a file it cites, an open question, the constraints, the budget left, or who owns the next step.

`handoff_checker.py` reads a handoff — JSON, or Markdown with front matter — and answers `ready`, `needs-fixes`, or `blocked`. `needs-fixes` comes with a punch list. Each line names the field and the edit.

It is one file, standard library only. Use it as a Python module, a CLI, or a local MCP server. The checker does not send the handoff and does not rewrite it. A harness should refuse the handoff unless the status is `ready`.

### Statuses

| Status | Meaning |
| --- | --- |
| `ready` | Every check passed. Send it. |
| `needs-fixes` | The payload can be repaired. `problems` lists each fix. Do not send it yet. |
| `blocked` | Do not send it. The budget left is exhausted, or the payload is over the size cap. Other problems are listed too. |

### Checks

- Required fields are present and non-empty: `goal`, `acceptance_criteria`, `owner`, `constraints`, `budget_left`.
- Local paths cited in the text, or listed under `artifacts`, exist. They resolve against `--root`, or the working directory. A relative handoff `path` uses that same directory, not the process working directory, and a missing file is reported with the absolute path.
- Acceptance criteria do not use vague words (`properly`, `etc`, `as needed`, and the rest of the list from `explain`).
- No `TODO`, `TBD`, or `FIXME` is left in. The message names the field that holds it, and the quote is the text around the marker.
- `budget_left` of zero or a negative amount (`-500 tokens`, `-10`, `-$1.50`) is blocked. A positive amount is not.
- Questions are answered, or there are none. A criterion written as a question is a check, not an open question.
- The payload is at most 16,384 bytes (override with `--max-bytes`, ceiling 2 MiB).

A JSON schema file can require more fields and a few simple types. It cannot remove the built-in ones.

### 30-second quickstart

```bash
python3 handoff_checker.py template --output handoff.md
python3 handoff_checker.py check handoff.md
python3 handoff_checker.py explain
```

The blank template is supposed to fail. Fill it in, then check again.

```bash
python3 demo_handoff.py
```

That demo is the week-2 recording: a sloppy handoff fails with a punch list, then the repaired one passes.

`check` exits `0` on ready, `3` on needs-fixes, `4` on blocked, and `1` on a real error (unreadable file, broken schema). Add `--json` for the machine-readable decision.

### As a library

```python
from handoff_checker import READY, HandoffChecker

checker = HandoffChecker(root=".", schema="handoff.schema.json")
decision = checker.check("handoff.md")
if decision["status"] != READY:
    for problem in decision["problems"]:
        print(problem["message"])
    raise SystemExit(1)
```

`check` accepts a dict, a path, or the document text. It does not write.

### As an MCP server

```bash
python3 handoff_checker.py serve --root /absolute/path/to/agent-toolkit
```

Stdio transport, one JSON-RPC message per line (the same shape Cursor and Claude Desktop speak). Tools: `handoff_check`, `handoff_template`, `handoff_explain`.

The server speaks protocol versions `2024-11-05`, `2025-03-26`, and `2025-06-18`. A client that asks for any other version, including a newer one, gets `2025-06-18`. An unknown tool name is a JSON-RPC `-32602` error, not an `isError` tool result.

```json
{
  "mcpServers": {
    "handoff-checker": {
      "command": "python3",
      "args": [
        "/absolute/path/to/agent-toolkit/handoff_checker.py",
        "serve",
        "--root",
        "/absolute/path/to/agent-toolkit"
      ]
    }
  }
}
```

Use an absolute `--root`. A relative `path` resolves against the per-call `root` if you pass one, otherwise this `--root`, not the process working directory. A relative per-call `root` stays under the server `--root`. Cited paths use that same directory.

### Schema

Built-in fields stay required. This file adds `ticket`:

```json
{
  "required": ["ticket"],
  "properties": {
    "ticket": {"type": "string", "minLength": 3},
    "priority": {"type": "string", "enum": ["low", "normal", "high"]}
  },
  "additionalProperties": false
}
```

```bash
python3 handoff_checker.py check handoff.md --schema handoff.schema.json
```

Supported keys are `required`, `properties` (`type`, `minLength`, `minItems`, `enum`, `description`), and `additionalProperties` set to `false`. Types are `string`, `number`, `integer`, `boolean`, `array`, and `object`.

### Front matter

Markdown handoffs open and close the header with a line that is just `---`. Values are scalars, lists, and one level of maps. Quote a list item that contains a colon. An unquoted colon is read as a map, and the check says so instead of reporting the list as empty.

```markdown
---
goal: List week 2 on the project page as a live tool.
owner: writer-agent
budget_left: 4000 tokens
constraints:
  - Leave the visit counter script unchanged.
acceptance_criteria:
  - The week 2 row shows a Live badge.
questions: []
artifacts:
  - docs/index.html
---
```

JSON is the same object, without the fence. `questions` entries may be strings, or objects with `question` and `answer`.

The CLI, the library, and the MCP server do not make network calls.

## Week 3 — Work-claim ledger

When several agents share a repo, two of them will pick up the same task. `work_claim.py` is a file-backed ledger: an agent claims an item under its owner id for a lease (a TTL). Another agent that tries the same item is told who holds it. When the lease expires, the claim is stale and the next agent can take it over.

Renew extends a lease you hold. Release gives the item back. Done marks it finished, so a later take will not redo it unless you pass `--reopen`.

It is one file, standard library only. Use it as a Python module, a CLI, or a local MCP server. The owner id is a label, not a secret. The ledger trusts the caller. It does not stop another process. The harness should skip an item when the result is `busy`.

### Results

| Result | Meaning |
| --- | --- |
| `taken` | You hold the item. The lease starts now. |
| `taken-over` | You hold it now. `previous_owner` names who had it. |
| `held` | You already hold it. Call renew to extend the lease. |
| `busy` | Someone else holds an unexpired lease. |
| `renewed` | Your lease now starts from this moment. |
| `released` | You gave the item up. |
| `done` | You marked it finished. |
| `not-owner` | That item is held by someone else. |
| `absent` | There is no claim for that item. |
| `closed` | It is done. Take it again only with `--reopen`. |

`list` shows open claims. Expired rows are stale and takeable. Done rows stay out of the default list. `list_claims` is the method name because `list` is a Python builtin. The CLI and MCP tool are `list` / `claim_list`.

### 30-second quickstart

```bash
python3 work_claim.py take fix-flaky-test --owner agent-a --ttl 600
python3 work_claim.py list
python3 work_claim.py renew fix-flaky-test --owner agent-a
python3 work_claim.py done fix-flaky-test --owner agent-a
```

`take`, `renew`, `release`, and `done` exit `0` when `ok` is true, `3` when the claim is refused (`busy`, `not-owner`, `closed`, `absent`), and `1` on a real error. `list` and `explain` exit `0`. Add `--json` for the machine-readable result.

```bash
python3 demo_work_claim.py
```

That demo is the week-3 recording: agent A claims a task, agent B is blocked, the lease expires, and agent B takes it over.

### As a library

```python
from work_claim import WorkLedger

ledger = WorkLedger(".agent-toolkit/work-claims.json")
claim = ledger.take("fix-flaky-test", "agent-a", ttl_seconds=600)
if not claim["ok"]:
    raise SystemExit(claim["result"])

ledger.renew("fix-flaky-test", "agent-a")
ledger.done("fix-flaky-test", "agent-a")
```

The path you pass is the file. Relative paths in the CLI and the MCP server are a separate rule: they resolve against `--root`. Pass a `clock` callable that returns a timezone-aware datetime when you want to test expiry without sleeping.

### As an MCP server

```bash
python3 work_claim.py serve --root /absolute/path/to/agent-toolkit
```

Stdio transport, one JSON-RPC message per line (the same shape Cursor and Claude Desktop speak). Tools: `claim_take`, `claim_renew`, `claim_release`, `claim_done`, `claim_list`, `claim_explain`.

```json
{
  "mcpServers": {
    "work-claim": {
      "command": "python3",
      "args": [
        "/absolute/path/to/agent-toolkit/work_claim.py",
        "serve",
        "--root",
        "/absolute/path/to/agent-toolkit"
      ]
    }
  }
}
```

Use an absolute `--root`. A relative `ledger` or `root` on a tool call is resolved against that root, not the process working directory. If a client asks for a protocol version this server does not speak, the server answers with `2025-06-18`.

### Ledger

Default path: `.agent-toolkit/work-claims.json` under `--root`. `$WORK_CLAIM_LEDGER` overrides it. An absolute path is used as given. A relative one resolves against `--root`. A relative `$WORK_CLAIM_LEDGER` may not climb above that directory.

The file is pretty-printed JSON. A sibling `work-claims.json.lock` serializes readers and writers across processes (`flock` on Linux and macOS). Writes go to a temp file in the same directory and replace the ledger, so a crash mid-write keeps the previous claims. The default lease is 900 seconds. The maximum is 86400 (24 hours). The lease is over at `expires_at`, and that instant is already takeable. The owner can renew after expiry until someone else takes the item.

Finished items are kept (up to 100) so a later take does not redo them by accident. This repo gitignores `.agent-toolkit/`. Notes you pass are stored in plain text.

The CLI, the library, and the MCP server do not make network calls.

## Week 4 — Approval ledger

A budget check can say `stop-and-ask-human` and return an `ask_human` string. `ask_human.py` stores that question, the action it covers, and the later answer. `ask` files one pending request and returns a request id. `answer` records `approved` or `denied` for that id only. `status` reads the open request, or one id. `list` shows what was stored.

It is one file, standard library only. Use it as a Python module, a CLI, or its own local MCP server. It is not part of `mcp_server.py`.

This is a local record. It is not proof of who answered, and it is not a security boundary. Any caller that can write the ledger, or call `answer`, can store `approved`. Restrict the answer path to the human. An approval does not raise a budget, start a tool, or resume work. The harness decides whether that specific request is enough to continue.

A second `ask` for a task that already has a pending request returns `pending-exists` and writes nothing. The request id, task, question, and action are fixed when the request is filed. An answer must repeat that question and action. A decided request is not overwritten. After it is decided, a new `ask` gets a new id. An answer for the old id does not approve the new one.

### Statuses

| Status | Meaning |
| --- | --- |
| `pending` | A question is waiting. Nothing has been approved. |
| `approved` | That request id was recorded as approved. |
| `denied` | That request id was recorded as denied. |
| `none` | This task has no stored request. |

### 30-second quickstart

```bash
python3 ask_human.py ask fix-flaky-test \
  --question "Ask a human before the next costly step." \
  --action "spend the next step on fix-flaky-test"

python3 ask_human.py status fix-flaky-test

python3 ask_human.py answer fix-flaky-test \
  --request-id REQUEST_ID \
  --decision approved \
  --question "Ask a human before the next costly step." \
  --action "spend the next step on fix-flaky-test"

python3 ask_human.py status fix-flaky-test --request-id REQUEST_ID
```

`ask` and `answer` exit `0` when `ok` is true, `3` when the call is refused (`pending-exists`, `already-decided`, `mismatch`, `absent`), and `1` on a real error, including a malformed ledger. `status`, `list`, and `explain` exit `0`. Add `--json` for the machine-readable result.

```bash
python3 demo_ask_human.py
```

That example opens a 100-token budget, checks a 1000-token step, stores the stop question, reads it back as `pending`, records an answer for that request id, and shows the budget still at 0 tokens. It makes no network call. The example process writes the answer itself, which is the point of the trust limit above.

### As a library

```python
from ask_human import ApprovalLedger

book = ApprovalLedger(".agent-toolkit/approvals.json")
filed = book.ask(
    "fix-flaky-test",
    "Ask a human before the next costly step.",
    action="spend the next step on fix-flaky-test",
)
if not filed["ok"]:
    raise SystemExit(filed["result"])

book.answer(
    "fix-flaky-test",
    filed["request_id"],
    "approved",
    question=filed["question"],
    action=filed["action"],
)
print(book.status("fix-flaky-test", request_id=filed["request_id"])["status"])
```

`list_requests` is the method name because `list` is a Python builtin. The CLI and MCP tool are `list` / `approval_list`. The path you pass is the file. Relative paths in the CLI and the MCP server resolve against `--root`.

### As an MCP server

```bash
python3 ask_human.py serve --root /absolute/path/to/agent-toolkit
```

Stdio transport, one JSON-RPC message per line (the same shape Cursor and Claude Desktop speak). Tools: `approval_ask`, `approval_answer`, `approval_status`, `approval_list`, `approval_explain`.

```json
{
  "mcpServers": {
    "ask-human": {
      "command": "python3",
      "args": [
        "/absolute/path/to/agent-toolkit/ask_human.py",
        "serve",
        "--root",
        "/absolute/path/to/agent-toolkit"
      ]
    }
  }
}
```

Use an absolute `--root`. A relative `ledger` or `root` on a tool call is resolved against that root, not the process working directory. If a client asks for a protocol version this server does not speak, the server answers with `2025-06-18`. A refused answer is a normal tool result with `ok` false. Bad arguments come back with `isError`.

### Ledger

Default path: `.agent-toolkit/approvals.json` under `--root`. `$ASK_HUMAN_LEDGER` overrides it. An absolute path is used as given. A relative one resolves against `--root`. A relative `$ASK_HUMAN_LEDGER` may not climb above that directory.

The file is pretty-printed JSON. A sibling `approvals.json.lock` serializes readers and writers across processes (`flock` on Linux and macOS). Writes go to a temp file in the same directory and replace the ledger, so a crash mid-write keeps the previous requests. An empty or corrupt file is an error and is left in place. A missing file reads as no requests and is created on `ask`.

The CLI, the library, and the MCP server do not make network calls.

## Unified MCP server

Instead of running three separate MCP servers, you can use `mcp_server.py` to expose all tools from budget guard, handoff checker, and work-claim ledger in a single server.

```bash
python3 mcp_server.py
```

The unified server accepts the same arguments as the individual tools:

```bash
python3 mcp_server.py \
  --root /absolute/path/to/project \
  --budget-ledger /absolute/path/to/budget-ledger.json \
  --work-ledger /absolute/path/to/work-claims.json
```

Defaults:
- `--root`: Current working directory
- `--budget-ledger`: `.agent-toolkit/budget-ledger.json` (relative to `--root`)
- `--work-ledger`: `.agent-toolkit/work-claims.json` (relative to `--root`)

### Configure in Claude Desktop or Cursor

Add to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`; Windows: `%APPDATA%/Claude/claude_desktop_config.json`; Linux: `~/.config/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "agent-toolkit": {
      "command": "python3",
      "args": [
        "/absolute/path/to/agent-toolkit/mcp_server.py",
        "--root",
        "/absolute/path/to/your/project",
        "--budget-ledger",
        "/absolute/path/to/your/project/.agent-toolkit/budget-ledger.json",
        "--work-ledger",
        "/absolute/path/to/your/project/.agent-toolkit/work-claims.json"
      ]
    }
  }
}
```

### Available tools (11 total)

**Budget Guard** (4 tools):
- `budget-guard-open`: Open a new budget task with limits
- `budget-guard-check`: Check if a step would exceed limits
- `budget-guard-record`: Record actual spend from a completed step
- `budget-guard-status`: Get the current status of a budget task

**Handoff Checker** (2 tools):
- `handoff-check`: Validate a handoff payload
- `handoff-template`: Get a template for a valid handoff

**Work Claim** (5 tools):
- `work-claim-take`: Claim an item for an owner
- `work-claim-renew`: Renew a lease on a claimed item
- `work-claim-release`: Release a claimed item
- `work-claim-done`: Mark an item as completed
- `work-claim-list`: List work claims with optional filtering

Stdio transport, one JSON-RPC message per line. Protocol version: `2024-11-05`.

## Codespaces

On GitHub, choose **Code**, then **Codespaces**, then **Create codespace on main**. `.devcontainer/devcontainer.json` uses the Microsoft Python 3.12 image. After create it runs the unit tests. No ports are forwarded.

The three tools stay offline. `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` are for Toolkit Tester or another live-test harness. When they are set, call that endpoint instead of DeepSeek or another cloud API.

| Variable | Codespaces |
| --- | --- |
| `OPENAI_BASE_URL` | Default in the devcontainer: `https://chat.srv1536099.hstgr.cloud/v1`. A secret of the same name overrides it after you open a new terminal. |
| `OPENAI_API_KEY` | Secret. Bearer token. Set this to call the model. |
| `LLAMA_API_KEY` | Optional secret. The same token under the other name. A new terminal copies it into `OPENAI_API_KEY` when that one is empty. |
| `OPENAI_MODEL` | Optional secret. Default `Llama-3.2-3B-Instruct`. |

Carrie White is the chat on VPS `srv1536099.hstgr.cloud`, Docker project `timeline-chat`: llama.cpp serving Llama-3.2-3B-Instruct, and the Carrie proxy under `/api`. The public host allows about 10 requests a minute. Stop after the two curls below.

Create secrets under GitHub **Settings → Codespaces → Codespaces secrets**, and allow this repository. Keys stay in that store. `.env` is gitignored. `.env.example` is the same list for a local shell. This repo does not load `.env` for you. Source the helper, or export the variables yourself:

```bash
. .devcontainer/openai-env.sh
```

`/health` answers without a key and does not name the model. `/v1/models` returns 401 until the bearer token is sent. Use the `id` from that list. If it is not `Llama-3.2-3B-Instruct`, put the advertised id in `OPENAI_MODEL`.

```bash
KEY="${OPENAI_API_KEY:-$LLAMA_API_KEY}"
curl -sS "$OPENAI_BASE_URL/models" \
  -H "Authorization: Bearer $KEY"
```

```bash
curl -sS "$OPENAI_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${OPENAI_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word pong.\"}],\"max_tokens\":16}"
```

Run the interactive chat client in this repo:

```bash
. .devcontainer/openai-env.sh
python3 chat_app.py
```

Optional flags:

```bash
python3 chat_app.py --model "$OPENAI_MODEL" --max-tokens 256 --temperature 0.2
```

Chat commands:
- `/help` show commands
- `/reset` clear conversation history
- `/model` show active model
- `/quit` or `/exit` leave the app

The same client is a static page in `docs/chat/`. GitHub Pages serves it at <https://rajeshradhakrishnanmvk.github.io/agent-toolkit/chat/>. You type the API key in the browser. It stays in that browser's storage and is not in this repository. The page can keep several conversations, and it estimates a budget the way `chat_app.py` does before it calls the model. It does not run the Python ledger files.

Memory is per-browser. Turns are stored in IndexedDB on the device (existing local-storage chats are copied in, not discarded). Recall is keyword-based, a small BM25 ranker with a stopword list and a synonym map from the global profile, not a semantic embedding. A short rolling summary is refreshed on the cadence in that profile, through the same endpoint. A 429 skips that refresh. Only the summary and the recalled snippets that fit the prompt are sent to the model, along with as many recent turns as the context window allows after the reply reserve and a safety margin. Turn memory off to send only the current step.

The persona is not fixed in the page. A global profile, a soul profile, and a session profile are instructions plus JSON configuration. Configuration resolves global, then soul, then session. Instructions are joined in that order into the system prompt. Conversations belong to a session, and switching session switches the soul and the project notes. Shipped defaults are in `docs/chat/profiles/`. The financial consultant is one preset soul. Edits stay in the browser. See `docs/chat/PROFILES.md`.

## Harness

`docs/harness/` is a small browser agent loop: layered profiles, one fenced tool call at a time, a budget check before each model call, and an Approve / Deny gate for medium and high risk tools. GitHub Pages serves it at <https://rajeshradhakrishnanmvk.github.io/agent-toolkit/harness/>. You type the API key in the browser. It stays in that tab. The agent runs on that page. Node is used only to run `node docs/harness/test.js`.

The default endpoint and model match the chat page (`https://chat.srv1536099.hstgr.cloud/v1`, `Llama-3.2-3B-Instruct`). v0 tools are `clock`, `budget`, and `ask_human`. v1 adds `handoff` and `work_claim`, and a two-agent demo: Planner and Writer share one session, and a blocked handoff stops the run. The budget, approval, and claim ledgers live in `localStorage`. A trace downloads as JSONL. Details, the profile schema, and the v2–v8 roadmap are in `docs/harness/README.md`.

v2 is a leaderboard for tiny-model tool calling. The fixed eval set is `tiny-tools` version 1. The board is [`docs/harness/leaderboard/`](docs/harness/leaderboard/), served at <https://rajeshradhakrishnanmvk.github.io/agent-toolkit/harness/leaderboard/>. Carrie White (Llama 3.2 3B via llama.cpp) is a pending live run, with no scores, until that eval is run on the Pages site and the results file lands in a follow-up pull request. Field names, the index entry, and a dummy example are in [Using the leaderboard](docs/harness/README.md#using-the-leaderboard). The numbers there are made up.

### Using the leaderboard

1. Open the harness page on GitHub Pages: <https://rajeshradhakrishnanmvk.github.io/agent-toolkit/harness/>.
2. Enter the endpoint, the model, and an API key. The key stays in that browser tab and is never exported. The endpoint must allow CORS from the Pages origin (`https://rajeshradhakrishnanmvk.github.io`).
3. Press **Run eval** to run the tiny-tools eval. A live run is paced and takes a few minutes. `?stub=1` previews the eval in the browser and is not a submission.
4. Press **Download results**. The browser saves `tiny-tools-result.json`.
5. Add that file under `docs/harness/results/` with a new `.json` name, and add its entry to `docs/harness/results/index.json`. A scored entry is `{ "file": "<name>.json" }`. The name may use letters, digits, `.`, `_`, and `-`.
6. Run `node docs/harness/test.js`. That command includes the results validator.
7. Open a pull request. The templates are `.github/ISSUE_TEMPLATE/harness-result.md` and `.github/PULL_REQUEST_TEMPLATE/harness-result.md`.

Do not copy the dummy example from the harness README into `docs/harness/results/` or the index.

### Reading the leaderboard

The board ranks scored rows by total score (`scores.value`, checks passed divided by checks), highest first. The columns are rank, model, total, and one per category (tool choice, arguments, no tool, multi-step, approval, budget), then the run date and a link to the results file. Above the table the page shows the eval set id, version, and content hash (`tiny-tools` version 1). A **pending live run** row has no results file and no numbers. The total is a dash, and the status says `pending live run`. Carrie White is that row until a live run replaces the pending index entry with a `file` entry. A labelled sample table is in the harness README. Those cells are made up.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Requires Python 3.9+. No third-party packages.

## License

MIT. Copyright 2026 Rajesh Radhakrishnan.
