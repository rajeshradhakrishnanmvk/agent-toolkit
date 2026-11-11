# Harness

A small agent harness that runs in the browser. GitHub Pages serves this folder, so the page, the loop, the tools, and the profiles are the same files.

v2 is a leaderboard for tiny models that call tools. It runs on the v0 loop and the v1 tools. v1 (`290479e`) added `handoff` and `work_claim`, and a two-agent demo. Planner and Writer take turns in one session. The loop is still short turns, a pluggable provider, tools, approval gates, a budget, and a trace.

Open the harness page, type an API key, and send a message. The key stays in that browser tab. It is sent only to the endpoint in the form. Nothing in this folder is a hosted chat service, and there is no Node command for running the agent.

## Quick start

1. Open the harness on GitHub Pages, or serve the `docs` folder with any static file server and open the `harness` path.
2. Leave the demo session selected. Autonomy defaults to `supervised`.
3. Type an API key. It is kept in `sessionStorage` for this tab and is not written into the trace.
4. Ask: `What time is it and how much budget is left?`
5. The model should call `clock`, then `budget` status, then answer from those results. A medium or high risk tool, such as `budget` record or `work_claim` claim, stops and shows Approve and Deny on the page.

Two agents, on the same page:

1. Select **Two agents**, or press **Run two agents** (it selects that session).
2. Planner claims `demo-task`, writes a handoff, and the run releases that claim. Writer claims the item, checks the handoff, and answers.
3. A handoff that is `blocked` or otherwise not `ready` stops the run. The reason is the checker summary and the problem list, in the thread and in the alert under the composer.
4. The whole demo makes at most 4 model calls. Add `?stub=1` to the page address to run that script locally. The stub does not send an API key.

Score the connected model with **Run eval**. The set is `tiny-tools` version 1, temperature 0. `?stub=1` also previews that eval: it does not read the API key and it does not call a model. Open the [leaderboard](leaderboard/) for submitted runs. A live run waits between requests so it stays under about 10 a minute.

The public endpoint allows about 10 requests a minute. A 429 waits and retries. A 401, a 5xx, or a network failure is shown as text and is not retried into a hidden loop.

Default endpoint and model, from `profiles/global.json`:

- `https://chat.srv1536099.hstgr.cloud/v1`
- `Llama-3.2-3B-Instruct`

Node is only for the test runner: `node docs/harness/test.js`. The same tests run in the browser at `test.html`.

## Profile schema

Profiles are JSON. `kind` is `global`, `soul`, or `session`.

```json
{
  "id": "demo",
  "kind": "session",
  "name": "Demo",
  "instructions": "What this layer should do.",
  "config": { "soulId": "general", "goals": "", "notes": "" }
}
```

Configuration merges global, then soul, then session. Nested objects merge. Arrays replace. `null` does not erase a value. Instructions are concatenated in that same order. A session's `goals` and `notes` are appended after its instructions.

`config.soulId` picks the soul. `config.autonomy` is `readonly`, `supervised`, or `full`. `config.budget` holds `maxTokens`, `maxToolCalls`, `maxCostUsd`, and `warnRatio` (default 0.8). `contextTokens` defaults to 2048 and `maxReplyTokens` to 256. The prompt must fit in `contextTokens - maxReplyTokens`. Older turns are dropped first. Token estimates are `ceil(characters / 4)`, not a model tokenizer.

Shipped files:

- `profiles/global.json`
- `profiles/souls/general.json`
- `profiles/souls/planner.json`
- `profiles/souls/writer.json`
- `profiles/sessions/demo.json`
- `profiles/sessions/handoff.json`
- `profiles/manifest.json`

Soul prompts for Planner and Writer are a few sentences. The two-agent run puts only `handoff` and `work_claim` in their tool list, so the system prompt stays small enough for a 2048-token context.

## Tool interface

A tool is a plain object. Add one to the array passed into `runAgent`.

| Field | Meaning |
| --- | --- |
| `name` | Tool name the model puts in the fenced JSON. |
| `description` | One or two sentences, included in the system prompt. |
| `schema` | JSON Schema for the arguments. The harness documents the shape; it does not run a schema validator. |
| `risk` | `"low"`, `"medium"`, or `"high"`. A function `(args) => level` is allowed when one action is riskier than another. |
| `run(args, ctx)` | Async function. Return JSON-serializable data. `ctx` has `now`, `taskId`, `budget`, `approvals`, `claims`, `handoffBox`, `files`, and `fetch`. |

The model calls a tool with one fenced block:

```tool
{"name":"clock","args":{}}
```

The parser takes the first valid `tool` block. A broken tool block is treated as a plain answer. At most one tool runs for each model reply. The single-agent loop stops after 3 model calls. The two-agent demo stops after 4 model calls in total.

| Tool | Risk | What it does |
| --- | --- | --- |
| `clock` | low | Current UTC time. |
| `budget` | low for `estimate`, `check`, and `status`; medium for `record` | Same decisions as `budget_guard.py`: `continue`, `warn`, `stop-and-ask-human`. `check` does not spend. `record` writes what happened. The harness refuses the next model call when a check says stop. |
| `ask_human` | low | `ask`, `status`, and `list` on the approval ledger. `answer` is not available to the model. |
| `handoff` | low | `check` validates a handoff document. `template` returns a blank one. Status is `ready`, `needs-fixes`, or `blocked`, with the same problem list as `handoff_checker.py`. |
| `work_claim` | low for `list`; medium for `claim`, `release`, `heartbeat`, and `done` | One owner holds an item until the lease ends. `heartbeat` renews that lease. A second owner is `busy` while the lease is live. After `expires_at` the claim is stale and the next owner gets `taken-over`. |

`handoff` follows the Python checker: required fields (`goal`, `acceptance_criteria`, `owner`, `constraints`, `budget_left`), an optional schema that can add fields but cannot drop those, vague acceptance criteria, unresolved questions, leftover TODO/TBD/FIXME, an exhausted budget (`blocked`), a size cap (`blocked`, default 16 KiB), and cited paths. Paths are checked against `ctx.files`, a map of paths that exist in this page. There is no disk.

`work_claim` follows `work_claim.py`: `taken`, `taken-over`, `held`, `busy`, `renewed`, `released`, `done`, `not-owner`, `absent`, and `closed`. The default lease is 900 seconds. The maximum is 24 hours. The ledger is `localStorage` key `agent-toolkit.harness.claims.v1`. A single-tab queue replaces the Python file lock.

Ledgers for budget and approvals stay in `localStorage` (`agent-toolkit.harness.budget.v1` and `agent-toolkit.harness.approvals.v1`). They follow the Python ledgers' rules closely enough for one task at a time: one pending approval per task, an answer must match the stored question and action, and an approval does not raise the budget by itself.

## Autonomy

| Level | Behavior |
| --- | --- |
| `readonly` | Only low-risk tools run. A medium or high tool returns a block message and is not executed. |
| `supervised` | Default. Low-risk tools run. A medium or high tool files an `ask_human` request and pauses until Approve or Deny on this page. |
| `full` | Every registered tool runs without a pause. |

The two-agent demo runs claim, release, and heartbeat without a pause, including when the page selector is `supervised`. That is what keeps the demo inside 4 model calls. Approve and Deny still apply to a normal Run. Deny does not run the tool. The ledger is a local record, not a check of who clicked the button.

## Two-agent run

One session, two souls, one claim ledger, one handoff document:

1. Planner calls `work_claim` `claim` on `demo-task`.
2. Planner calls `handoff` `check` with the document. The run then releases Planner's claim.
3. Writer calls `work_claim` `claim`.
4. Writer calls `handoff` `check`. The document is the one Planner stored. If `ok_to_hand_off` is false, the run stops and the page shows the summary and each problem. If it is true, the sentence above the tool fence is the answer.

Release is a ledger call, not a fifth model call. Both souls are fields on the JSONL trace (`soul`) and labels on the run view. When the run finishes, the demo releases its own item if Planner or Writer still holds it, so the next press can claim it. It does not take a live claim owned by someone else.

## Trace

Each step is a JSON object: time, kind, soul, prompt size, token estimate, tool name, gate decision, and duration. The page keeps the trace in memory. Download trace saves `harness-trace.jsonl`. The API key is not a field in that file, and it is stripped if an error message would have contained it.

## Tests

```bash
node docs/harness/test.js
```

Or open `test.html` on the same static server. The suite covers the v0 cases (profile merge, tool-call parsing, one tool per reply, the 3-iteration cap, supervised pause and resume, a readonly block, a budget stop, the trace, 429 backoff, and the demo question against a stubbed provider) plus handoff ready / needs-fixes / blocked, a schema, the blank template, a claim conflict, a stale-claim takeover, heartbeat, and the two-agent run. That run asserts the model is called 4 times or fewer. The v2 cases score a stubbed transcript twice, pass and fail each eval category, check that an export leaves out secrets, and run the results validator.

## Eval set

`tiny-tools` version 1 is ten short tasks. Eval tools are deterministic mocks with fixed outputs (`clock`, `lookup`, `echo`, `note`). They are separate from the page tools. The runner still uses `runAgent`: the same tool registry shape, autonomy modes, budget ledger, approval gate, and trace.

| Category | What a pass looks like |
| --- | --- |
| tool-choice | The first call names the tool the prompt asked for. |
| arguments | The call parses as JSON and matches that tool's schema. |
| no-tool | The reply has no tool fence, and the answer contains the expected token. |
| multi-step | A second call uses the fixed value returned by the first call. |
| approval | A denied medium-risk call does not run, and the answer does not claim success. |
| budget | The tool-call budget is already spent, the model asks for `clock`, and the run stops with `budget-stop` before the tool runs. |

Approvals in an eval are scripted by the task. The page does not wait for a click. `deny-note` is denied. Temperature is 0. Prompts are written for a 2048-token context with a 256-token reply reserve. Estimates stay `ceil(characters / 4)`.

A task score is checks passed divided by checks on that task. The total, and each category score, use that same ratio over the checks in the group. Every check has the same weight. The stored scores are only those ratios.

The content hash is SHA-256 of the canonical JSON of the task set. Object keys are sorted, arrays keep their order, and there is no extra whitespace. The browser uses SubtleCrypto. The Node tests use `node:crypto`. The hash covers the set id, version, temperature, context size, instructions, tools, prompts, and checks. It does not cover a single run. The result file stores that hash so a score stays tied to this exact set.

## Contribute a result

1. Open the harness on the live GitHub Pages site. The model endpoint allows that origin. Use `?stub=1` only to preview. A preview does not read the API key, does not call a model, and is not a submission (`preview` is true, and the validator rejects it).
2. Type an API key in the form. It stays in the tab. It is sent only to the endpoint in the form.
3. Enter a leaderboard label (your name and the model).
4. Press **Run eval**. The page shows the task it is on and waits between requests so a live run stays under about 10 a minute.
5. Press **Download results**. The JSON has schema version 1, the eval set id, version, and hash, your label, the model id the API returned, harness version 2, the run date, total and per-category scores, and each task's checks and trace.
6. Add that file under `docs/harness/results/` with a new `.json` name. Add an object to `entries` in `docs/harness/results/index.json` with `"file"` set to that name. Pages cannot list a folder, so the leaderboard reads the index. The shape is described in `docs/harness/results/result.schema.json` and checked by `docs/harness/validate-results.js`.
7. Open a pull request. The issue template is `.github/ISSUE_TEMPLATE/harness-result.md`. The pull request template is `.github/PULL_REQUEST_TEMPLATE/harness-result.md`. `node docs/harness/test.js` must pass. The validator recomputes every score from the checks, checks the eval hash against the current tiny-tools v1 set, checks that every results file is in the index and every scored index entry has a file, and rejects a key-like string, an Authorization header, or a URL to a model endpoint.

A pending index entry has `"pending": true`, a label, and no `file` and no scores. Carrie White (Llama 3.2 3B via llama.cpp) is that kind of row. There are no numbers for her yet. Rajesh will run the eval on the live Pages site and add the real results file in a follow-up PR. That follow-up replaces the pending entry with a `file` entry so the row is scored instead of pending.

## Roadmap

- v2: this leaderboard (the tiny-tools eval, exported results, and the static board).
- v3: IndexedDB keyword memory. Reuse the keyword-recall code in `docs/chat/memory.js`, and score that memory on this eval set.
- v4: a virtual file workspace with write approval.
- v5: an MCP-style bridge.
- v6: one inbound channel, if GitHub Pages allows it.
- v7: scheduled runs.
- v8: a multi-session router.

The earlier golden-prompt evals item is folded into this leaderboard.

## Limits of v1

The page talks to an OpenAI-compatible `/v1/chat/completions` endpoint. Tool calls are fenced JSON because native tool calling on the 3B model is unreliable. The single-agent loop will not run two tools from one reply, and it will not make a fourth model call. The two-agent demo will not make a fifth.

The handoff checker matches `handoff_checker.py` on the decisions it can make in a browser: required fields, the schema subset, vague criteria, questions, TODO/TBD/FIXME, budget, size, and cited paths. Cited paths use an in-memory map, not the computer's disk, and a schema is an object on the call, not a file path. A bare filename is not read from disk. Markdown front matter is the same small subset (scalars, lists, one level of maps).

The claim ledger matches `work_claim.py` for take, renew (heartbeat), release, done, and list, including `busy`, `held`, stale takeover, and `closed`. It does not lock a file across processes or browser tabs. Two tabs can race. Timestamps are ISO-8601 UTC (`Z`), not the Python `+00:00` form. The owner id is a label, not a secret.

The demo releases Planner's claim itself after the handoff is stored, so that step is not a model call. A reply that is not the expected tool stops the run with a reason. It does not retry, because the endpoint allows about 10 requests a minute and the cap is 4 calls.

The budget, approval, and claim ledgers are per browser, not the Python processes. Reset budget clears the spend for the selected session. A paused turn is kept in `sessionStorage` so a reload of this tab can still Approve or Deny. A claim left behind by a closed tab stays until its lease ends (default 15 minutes) or the next demo press clears an item still held by Planner or Writer.

`?stub=1` replays the scripted demo in this browser, and it replays the scripted eval. It does not call the endpoint and it does not read the API key.

## Limits of v2

The leaderboard ranks files in `results/index.json`. With no scored files it shows an empty board plus the pending Carrie White row, and that row has no numbers. This repository copy does not include a live Carrie result: producing one needs a key, and the endpoint only allows the GitHub Pages origin.

The validator checks that a file matches the current task set and that its scores are the ratios of its own checks. It does not call the model again. Read the trace in the pull request before trusting a perfect score. A stub preview sets `preview` to true so it cannot be submitted as-is. Stripping that flag would still leave a scripted transcript; the trace is the evidence.

Eval tools are mocks. They do not exercise `handoff` or `work_claim`. The loop still accepts one fenced tool call per reply and stops after 3 model calls, so the multi-step tasks fit in that cap. Temperature 0 is what the page sends. A server can still vary. Token counts remain character estimates. The board changes when a pull request updates the index. A live eval is paced, so the ten tasks take a few minutes. The second-call checks look for the fixed mock value, which a reply could repeat without having read the tool if it already knew that word.
