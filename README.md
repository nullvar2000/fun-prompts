# fun-prompts

A gallery of zero-shot web applications built by AI — and the builder that makes
them. Point it at any OpenAI-compatible LLM endpoint, submit a prompt, and it
fans that prompt out to up to four models at once, validates what comes back,
tags it with the model that produced it, and publishes it to the gallery.

**Status:** the builder and the gallery both work — submit a prompt over `/api`,
watch it publish, browse the result. The admin panel and the comparison view are
still to come.

| Milestone | What lands | |
| --- | --- | --- |
| M0 | Config, logging, app skeleton, Containerfile, CI, fake-LLM test harness | ✅ |
| M1 | SQLite schema, migrations, repositories, seed import | ✅ |
| M2 | Provider adapters: `chat/completions`, `/v1/models` discovery, SSRF guard | ✅ |
| M3 | Prompt guard, artifact extract/validate, job queue + worker, submission API | ✅ |
| M4 | Gallery, home "3 random", build pages, artifact origin on :8081 | ✅ |
| M5 | Admin: endpoints, models, tags, moderation, failure logs | ⏳ |
| M6 | Side-by-side comparison (up to 4), refine with version history | ⏳ |
| M7 | E2E tests, container smoke test in CI, docs | ⏳ |

## Quick start — local

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Secrets are generated into data/first-run.txt if you don't set them.
FP_DATA_DIR=./data FP_HOST=127.0.0.1 .venv/bin/python -m app.main
# gallery  http://127.0.0.1:8080
# health   http://127.0.0.1:8080/healthz
```

## Build something

Two terminals. The first is a scripted OpenAI-compatible endpoint that ships in
`tests/` so production code can never reach it; swap it for your Ollama, vLLM or
llama.cpp server and the steps are the same.

```sh
# terminal 1 — a fake endpoint that answers with a real self-contained app
.venv/bin/python -m tests.fake_openai_server --port 8123 \
  --completion-file seed/apps/dice-roller.html --fence
```

```sh
# terminal 2 — register it, then start the app
export FP_DATA_DIR=./data FP_ENDPOINT_ALLOWLIST=127.0.0.1:8123
.venv/bin/python -m app.cli endpoints add lan http://127.0.0.1:8123/v1 --tag lan-demo
.venv/bin/python -m app.cli endpoints discover lan   # pulls /v1/models
.venv/bin/python -m app.cli endpoints probe lan      # reachability check
FP_PORT=8090 .venv/bin/python -m app.main
```

Now submit a prompt and let the worker finish it:

```sh
curl -s localhost:8090/api/targets
curl -s -X POST localhost:8090/api/prompts -H 'content-type: application/json' -d '{
  "body": "A dice roller: pick d4 to d20, keep a rolling history.",
  "targets": [{"endpoint_id": 1, "model_row_id": 1}]
}'
curl -s localhost:8090/api/builds/4      # status, tags, version, failure history
curl -s localhost:8090/api/jobs          # queue state
```

One prompt, several targets, one build per target — that is the comparison
fan-out, and it is capped by `FP_MAX_COMPARISON_TARGETS`.

Endpoint writes only exist in the CLI until the authenticated admin panel (M5)
lands; until then there is deliberately no unauthenticated write surface.

## Browse

| URL | What it is |
| --- | --- |
| `/` | three published apps, chosen at random on every visit |
| `/gallery` | search, AND-combined tag chips, filter by endpoint+model, sort, paging |
| `/build/{id}` | the app in a sandboxed frame, plus endpoint, model, latency, tokens, extraction notes and version history |
| `/prompt/{slug}` | the prompt and every public build made from it |
| `/download/{version_id}` | the stored file as an attachment |
| `:8081/artifact/{version_id}` | the artifact origin: artifact bytes and nothing else |

The artifact origin runs in the same process as the gallery on a second port
(`FP_RUN_ARTIFACT_SERVER=1`). It has three routes, sets no cookies, serves no API
— every path is GET/HEAD only — and its CSP is
`default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'`, so a
generated page can draw but cannot reach a network. The gallery frames it with
`sandbox="allow-scripts"` and no `allow-same-origin`, which gives the frame an
opaque origin.

## Quick start — container

```sh
podman build -t fun-prompts -f Containerfile .
podman run -d --name fun-prompts \
  -p 8080:8080 -p 8081:8081 \
  -v fun-prompts-data:/data \
  -e FP_ARTIFACT_BASE_URL=http://localhost:8081 \
  -e FP_ENDPOINT_ALLOWLIST=192.168.1.20:11434 \
  -e ADMIN_PASSWORD=change-me \
  localhost/fun-prompts

podman logs fun-prompts      # first-run.txt credentials land here too
```

`FP_ARTIFACT_BASE_URL` must match the hostname you actually browse to: it feeds
both the iframe `src` and the `frame-ancestors` CSP directive.

## Configuration

Everything is environment-driven — nothing operational is hardcoded. See
[`.env.example`](.env.example) for the full annotated list; `FP_DOTENV=1` loads a
local `.env`. The resolved config is logged at startup with secrets redacted.

Values you change often (endpoints, models, tags, per-endpoint timeout and token
caps, publish state) live in the database and are edited in the admin panel.
Environment values act as deployment wiring **and** as ceilings: a global
`FP_JOB_TIMEOUT_S` still bounds an endpoint that asks for longer.

## Layout

```
app/
  main.py            composition root — the only place layers are wired
  container.py       builds the object graph once, for the app and the CLI
  cli.py             operator tools: endpoints add/list/discover/probe, status
  config.py          env-first settings, validation, secret bootstrap
  domain/            models, status machines, errors — no I/O
  storage/           migrations + repositories (SQLite, WAL)
  providers/         plugin boundary for LLM backends (base.py = the contract)
  build/             guard → provider → extract → validate → publish
  worker/            the queue loop that runs builds
  web/               security headers, Jinja templates, JSON API
    artifact_server.py  the :8081 origin — artifact bytes only
gallery_static/      style.css + vendored htmx.min.js (no CDN anywhere)
prompts/             seed prompts (markdown)
seed/apps/           pre-built HTML imported as seed builds
tests/               unit/API tests, FakeProvider, fake OpenAI server
```

Layering is one-way: `domain → storage → providers → build → web`. No layer
imports upward, `domain/` performs no I/O, and only `main.py` wires things.

## Security posture

- **Two origins.** `:8080` holds the admin session and the API. `:8081` serves
  generated HTML only: no cookies, no CORS, no API, strict CSP.
- **Cookies ignore ports**, so the session cookie is `SameSite=Strict` and every
  mutating request will require a CSRF token plus a custom header. Without that,
  a separate origin is only half a boundary.
- **Prompt injection screening** blocks obvious override attempts at submission
  and logs them (`guard_log`). The API answers `422` without naming the rule —
  describing the pattern that matched teaches how to evade it.
- **The builder prompt treats the task as data**, inside sentinels that user
  text cannot close early.
- **SSRF guard** is allowlist-based (`FP_ENDPOINT_ALLOWLIST`) and always blocks
  metadata/link-local addresses. Blanket private-range blocking is deliberately
  not used — your Ollama/vLLM box lives on the LAN.
- **Artifact validation** rejects anything that isn't a single HTML document
  under the size cap with no external URLs, no `<base>` and no meta refresh,
  before it is ever published. Rejected attempts are kept in `build_failures`
  with the raw response, so a misbehaving endpoint can be read about later.
- **Containment at serve time.** The artifact origin's CSP forbids every fetch
  (`connect-src 'none'`), and the frame is sandboxed without
  `allow-same-origin`. `FP_MAX_ARTIFACT_KB` is re-checked when bytes are served,
  because env values are ceilings: a row written under a larger cap stays
  unserved after the cap is lowered.
- **Hidden means gone.** Unpublishing or hiding a build or its prompt removes it
  from the gallery, home, downloads and the artifact origin on the next request —
  `Cache-Control: no-cache` on artifacts keeps a cached copy from outliving the row.

## Tests

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pyflakes app tests
```

244 tests cover config and secret bootstrap, security headers, the schema and
repositories, provider adapters over real TCP (including timeouts and refused
addresses), the prompt guard, HTML extraction, the artifact gate, the pipeline's
publish/repair/reject/fail/cancel outcomes, the worker's concurrency cap and
crash recovery, the submission API, the operator CLI, the artifact origin's
headers and visibility rules, and the gallery pages.

CI runs `pyflakes` and `pytest`, then builds the image and checks a running
container: health, the gallery, the sandboxed frame, the artifact origin's header
set (no `X-Frame-Options`, no `Set-Cookie`), and that downloads arrive as
attachments.
