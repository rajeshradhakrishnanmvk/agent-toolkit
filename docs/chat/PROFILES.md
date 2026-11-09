# Chat profiles

The chat page does not bake in a persona. Three profiles are layered. Each one is instructions (text) plus configuration (a JSON object).

Resolution for configuration is global, then soul, then session. A later value replaces an earlier one. Nested objects merge. Arrays, including a soul's `stages`, are replaced as a whole. Synonyms merge key by key.

Instructions are concatenated in the same order, global then soul then session, and that text is the system prompt. Session `goals` and `notes` are appended after the session instructions. The existing prompt budgeter then fits that system text with the summary, recalled notes, and recent turns into the context window (`contextTokens`, default 2048), leaving `maxReplyTokens` (default 256) plus `safetyTokens` (default 64).

Shipped defaults live in `docs/chat/profiles/`. Edits in the page are stored in IndexedDB on that browser. Reset to default drops the edited copy and loads the file again. Export and import move profiles as JSON and do not include the API key.

## Global

`profiles/global.json`

| Field | Role |
| --- | --- |
| `id`, `kind`, `name` | `kind` is `global`. |
| `instructions` | Rules applied to every bot. |
| `config.baseUrl`, `model`, `temperature` | Endpoint defaults. |
| `config.maxReplyTokens`, `contextTokens`, `safetyTokens` | Prompt budget. |
| `config.legacySoulId` | Soul used when an older chat has no session yet. |
| `config.memory` | On/off, scope, recall token budget, top-k, summary cadence, summary instruction, synonym map. |
| `config.budget` | Local token, tool-call, and cost caps. |
| `config.askHuman` | `enabled` and a `template` that contains `{reason}`. |

## Soul

`profiles/souls/*.json`

| Field | Role |
| --- | --- |
| `id`, `kind`, `name` | `kind` is `soul`. Several souls can exist. |
| `instructions` | Persona text. |
| `greeting` | Shown when a conversation has no messages yet. |
| `config.temperature`, `config.maxReplyTokens` | Optional overrides. |
| `config.stages` | Optional local steps before the model call. |

A stage has `id`, `owner`, and `template`. Templates may use `{{user}}`, `{{previous}}`, `{{tip}}`, and the `id` of an earlier stage. `tips` is a list rotated by turn number. The stage with `"model": true` is the one model call. With no stages, the user text is sent as itself.

The financial consultant is one preset soul. General assistant and Editor ship as neutral presets.

## Session

`profiles/sessions/*.json`

| Field | Role |
| --- | --- |
| `id`, `kind`, `name` | `kind` is `session`. A session is one project. |
| `instructions` | Project rules. |
| `config.soulId` | Which soul this project uses. |
| `config.goals`, `config.notes` | Added to the system prompt. |
| `config` overrides | Any global or soul setting, including `memory` and `temperature`. |

Conversations belong to one session. Switching session switches the soul and the project text. Memory of turns stays in the browser either way.

## Airport

The same three profiles run a takeoff. Nothing in the page script names a persona.

Global `config.airport` is the operations manual: airport `name` and `code`, `runway`, short `radio` rules, and the `checklist` items. `config.askHuman.template` is the human-approval hold. Safety rules live in the global instructions: no phase skipping, and HOLD stops the flight until it is resolved.

A session is a flight plan. `config.flight` is the flight number, `origin` and `destination` are the ends of the route, `cargo` and `notes` are the load, `weather` is constraints, and `crew` maps a role (`ground`, `captain`, `atc`, `first-officer`, `cabin`) to a soul id. Each conversation is one flight on that plan.

Crew souls own phases. Ground owns pre-flight, the captain owns clearance and the delivered answer, ATC owns the clearance decision, the first officer owns the read-back, and cabin owns the debrief. The financial consultant soul remains an optional passenger preset. General assistant and editor stay usable as souls, but they do not own a takeoff phase unless a flight plan assigns them.

Phases, in order, with at most one model call each: pre-flight (no model; HOLD if the request or destination is missing), clearance (captain, one call), ATC (deterministic fuel, handoff, and runway checks; a model call only phrases CLEARED), taxi (no model), takeoff (one call, the answer), after takeoff (one call, the debrief, also stored as the memory summary). A full takeoff is therefore four model calls. HOLD, REROUTE, a busy runway, and a 429 do not spend a call. A 429 becomes a traffic-delay hold with Retry. Later messages on a departed flight are one in-flight follow-up.
