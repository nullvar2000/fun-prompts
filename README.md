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
| `/admin` | the panel: endpoints, prompts, builds, failure and guard logs (session required) |

The artifact origin runs in the same process as the gallery on a second port
(`FP_RUN_ARTIFACT_SERVER=1`). It has three routes, sets no cookies, serves no API
— every path is GET/HEAD only — and its CSP is
`default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'`, so a
generated page can draw but cannot reach a network. The gallery frames it with
`sandbox="allow-scripts"` and no `allow-same-origin`, which gives the frame an
opaque origin.

## Admin panel

`/admin` is where endpoints and models are configured and where moderation
happens. Nothing that mutates lives anywhere else — the public API can queue a
build, but it cannot add an endpoint or hide a build.

| Page | What you do there |
| --- | --- |
| `/admin` | worker state, queue depth, endpoint summary, the last failures |
| `/admin/endpoints` | add/edit endpoints, set tags and per-endpoint caps, probe, discover models, delete |
| `/admin/endpoints/{id}` | the same for one endpoint, plus its model list, enable/disable and its recent failures |
| `/admin/prompts` | edit a stored prompt, hide it, fork it, delete it, or re-run it on other endpoints |
| `/admin/builds` | hide/unhide, re-run failed builds, cancel running ones, add or remove tags, delete |
| `/admin/failures` | every rejected attempt, with the endpoint's raw response shown as escaped text |
| `/admin/guard` | what the prompt-injection detector blocked or flagged |
| `/admin/tags` | the tag vocabulary and where each tag came from |

Signing in:

- Set `ADMIN_PASSWORD` (or `FP_ADMIN_PASSWORD`). Left unset, a password and a
  session secret are generated on first boot into `data/first-run.txt`, which is
  also printed to the startup log.
- The session is a signed cookie (HMAC-SHA256, `FP_SESSION_SECRET`), `httpOnly`
  and `SameSite=Strict`. `FP_COOKIE_SECURE=1` adds `Secure` once something
  TLS-terminating sits in front.
- Failed sign-ins are rate limited per client per hour
  (`FP_LOGIN_MAX_ATTEMPTS_PER_HOUR`, default 10).

Why the panel is not just "behind a login": the artifact origin is a different
*port* on the same host, and cookies ignore ports. Three things hold that
boundary together, and each is asserted in `tests/test_auth.py` —
`SameSite=Strict` (a cross-site request carries no session cookie at all), a CSRF
token carried inside the signed session and echoed back by every form, and
`X-Requested-With` on JSON writes, which a cross-origin page cannot set without a
CORS handshake this origin never offers.

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
both the iframe `src` and the `frame-ancestors` CSP directive. The gallery origin
is derived from it by swapping in `FP_PORT`, so if you remap host ports
(`-p 18080:8080`), also set `FP_GALLERY_BASE_URL=http://localhost:18080` —
otherwise the browser refuses to frame the artifact and every app page is a blank
box.

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
    auth.py             signed admin session, csrf gate, login throttle
    routes_admin.py     the /admin panel
    routes_gallery.py   home, gallery, build and prompt pages, downloads
    artifact_server.py  the :8081 origin — artifact bytes only
    templates/          Jinja templates (admin/ is the panel)
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
- **Cookies ignore ports**, so the admin surface is held by three independent
  things: a `SameSite=Strict` `httpOnly` session cookie (a cross-site request
  carries it not at all), a CSRF token that lives inside the signed session and
  must be echoed back on every mutating request, and `X-Requested-With` on JSON
  writes. Each is asserted separately in `tests/test_auth.py`.
- **Admin credentials are never echoed back.** API keys are encrypted at rest
  (`FP_KEY_FILE`, Fernet, `0600`), shown in the panel only in masked form, and a
  blank key field means "keep the stored one".
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

328 tests cover config and secret bootstrap, security headers, the schema and
repositories, provider adapters over real TCP (including timeouts and refused
addresses), the prompt guard, HTML extraction, the artifact gate, the pipeline's
publish/repair/reject/fail/cancel outcomes, the worker's concurrency cap and
crash recovery, the submission API, the operator CLI, the artifact origin's
headers and visibility rules, the gallery pages, the admin session and CSRF gate
(tampering, expiry, throttling, cross-site writes), and the admin panel's
create/edit/moderate behaviour including the ceiling clamps and allowlist
refusals.

CI runs `pyflakes` and `pytest`, then builds the image and checks a running
container: health, the gallery, the sandboxed frame, the artifact origin's header
set (no `X-Frame-Options`, no `Set-Cookie`), that downloads arrive as
attachments, and the admin gate end to end — anonymous redirects, cookie flags, a
CSRF refusal, a throttled wrong password, and a real sign-in that reaches the
endpoints page.
