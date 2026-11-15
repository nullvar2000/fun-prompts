# fun-prompts

A gallery of zero-shot web applications built by AI — and the builder that makes
them. Point it at any OpenAI-compatible LLM endpoint, submit a prompt, and it
fans that prompt out to up to four models at once, validates what comes back,
tags it with the model that produced it, and publishes it to the gallery.

**Status:** the whole thing works. Submit a prompt from the form or the API,
compare up to four outputs side by side, revise a published app against the model
that made it, and administer endpoints from `/admin`. Every layer is wired
together and covered, including one end-to-end pass over real sockets and a
container job that builds inside the shipped image.

| Milestone | What lands | |
| --- | --- | --- |
| M0 | Config, logging, app skeleton, Containerfile, CI, fake-LLM test harness | ✅ |
| M1 | SQLite schema, migrations, repositories, seed import | ✅ |
| M2 | Provider adapters: `chat/completions`, `/v1/models` discovery, SSRF guard | ✅ |
| M3 | Prompt guard, artifact extract/validate, job queue + worker, submission API | ✅ |
| M4 | Gallery, home "3 random", build pages, artifact origin on :8081 | ✅ |
| M5 | Admin: endpoints, models, tags, moderation, failure logs | ✅ |
| M6 | Side-by-side comparison (up to 4), refine with version history | ✅ |
| M7 | E2E tests, container smoke test in CI, docs | ✅ |

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
fan-out, and it is capped by `FP_MAX_COMPARISON_TARGETS`. Those commands are the
API version of what the gallery's **make one** form does: open
`http://localhost:8090/new`, tick the pairs and submit, and the redirect lands on
the comparison of the builds it just queued. The API exists for scripts.

## Browse

| URL | What it is |
| --- | --- |
| `/` | three published apps, chosen at random on every visit |
| `/new` | the submission form: a prompt plus the endpoint+model pairs to fan out to |
| `/gallery` | search, AND-combined tag chips, filter by endpoint+model, sort, paging |
| `/build/{id}` | the app in a sandboxed frame, plus endpoint, model, latency, tokens, extraction notes, version history and the revise box |
| `/build/{id}/status` | the frame-and-status fragment the build page re-fetches while a build or revision is moving |
| `/compare?b=…&hide=…` | one prompt, up to four outputs side by side; `hide` folds columns away |
| `/prompt/{slug}` | the prompt, every public build from it, the picker for comparing them, re-run and fork |
| `/prompt/{slug}/fork` | the prompt text, editable, built as a child prompt |
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

## Make, compare, revise

- **Make one** (`/new`) posts a prompt and a set of ticked endpoint+model pairs.
  Each pair gets its own build of the same prompt, and the redirect lands in the
  comparison view rather than on one build page. A rejected submission comes back
  with the text still in the box; the reason is one of a fixed set of sentences,
  never model or endpoint output.
- **Compare** (`/compare?b=1&b=2`) frames up to `FP_MAX_COMPARISON_TARGETS`
  outputs under the same sandbox rules as a build page. Show/hide and the
  selection itself are query parameters, so a comparison is a link. With exactly
  one output shown the grid becomes a single full-height frame. The prompt is
  pinned at the bottom of the page: it is the one thing every column has in
  common. A selection spanning two prompts is refused — the question a
  comparison answers is what *one* input produced.
- **Revise** (the box on a build page) adds a version to a build that already
  published, and it always asks that build's own model: a version chain only
  means something if nothing else moved. The published version keeps serving
  while the next one is generated, and a revision that fails the gate leaves it
  exactly where it was. The previous document is sent back as data inside
  sentinels and clipped to `FP_REFINE_CONTEXT_CHARS` when it is too big to send
  whole — the clipping is recorded on the version and shown, rather than
  pretended away.
- **Fork** copies a stored prompt (keeping `parent_prompt_id` and `source=fork`),
  lets you edit the copy, and builds the copy. The edited text is screened like
  any new prompt: forking is not a way around the guard. **Run it again** sends
  the stored text unchanged to other pairs, which is the point of keeping it.

Every one of those writes is a public route with no session, so each is checked
against the origin the browser attached to it — see
[Security posture](#security-posture) — and counted against
`FP_RATE_LIMIT_PER_HOUR`.

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

Three worth knowing about:

- `FP_MAX_COMPARISON_TARGETS` (4) — how many pairs one prompt may fan out to, and
  how many columns `/compare` will show.
- `FP_REFINE_CONTEXT_CHARS` (24000) — how much of the previous version goes back
  to the model on a revise turn. An artifact may be 200 KB, so the context is
  clipped from the end and the clipping is recorded on the version.
- `FP_RATE_LIMIT_PER_HOUR` (0 = no limit) — the per-address ceiling on public
  writes: make, fork, re-run, revise and the submission API. Counted in memory,
  so it resets on restart. Fine on a LAN; set a number before exposing the
  gallery, because every submission spends someone else's API budget.

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
    refine.py          follow-up turns: previous HTML as context, same model
  worker/            the queue loop that runs builds
  web/               security headers, Jinja templates, JSON API
    auth.py             signed admin session, csrf gate, login throttle
    security.py         gallery origin derivation + the same-origin write gate
    throttle.py         per-address submission ceiling
    routes_admin.py     the /admin panel
    routes_gallery.py   home, gallery, build/prompt/compare pages, make, fork, downloads
    artifact_server.py  the :8081 origin — artifact bytes only
    templates/          Jinja templates (admin/ is the panel)
gallery_static/      style.css + vendored htmx.min.js (no CDN anywhere)
prompts/             seed prompts (markdown)
seed/apps/           pre-built HTML imported as seed builds
tests/               unit/API tests, e2e over real TCP, FakeProvider, fake OpenAI server
```

Layering is one-way: `domain → storage → providers → build → web`. No layer
imports upward, `domain/` performs no I/O, and only `main.py` wires things.

## Security posture

- **Two origins.** `:8080` holds the admin session and the API. `:8081` serves
  generated HTML only: no cookies, no CORS, no API, strict CSP.
- **Public writes have an origin gate.** The gallery's make/fork/re-run/revise
  routes and the submission API carry no session, so there is no token to
  compare; what they check instead is the origin the browser attached to the
  request (`app/web/security.same_origin_write`). A request with neither
  `Origin` nor `Referer` is not a browser cross-site request — curl, a health
  check, a test client — so it is allowed; a present one has to match the host
  being browsed or `FP_GALLERY_BASE_URL`. The artifact origin is never in that
  set: a generated app cannot submit prompts, revise builds or cancel jobs.
  `Origin: null` (an opaque-origin document: privacy hardening, a header-rewriting
  extension, a sandboxed page) is decided by `Sec-Fetch-Site` instead — another
  forbidden header page script cannot forge — and only the literal `same-origin`
  counts, so a cross-site page and a framed artifact still fail. Behind a
  TLS-terminating proxy, `FP_FORWARDED_ALLOW_IPS` names the proxy whose
  `X-Forwarded-Proto`/`Host` the derived origin may trust.
- **The gallery has a spend ceiling.** `FP_RATE_LIMIT_PER_HOUR` counts public
  writes per address in memory and says how long to wait. Cancelling a job is
  never counted, because that answer saves work.
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
  text cannot close early. A revise turn wraps the *previous artifact* the same
  way: it was generated by a model from user text, so it can contain a line that
  reads like an instruction, and on the next turn that is data and never a
  command.
- **A revise cannot take a working app offline.** A published build has no legal
  transition out of `published`, and a refine job never touches build status: the
  current version keeps serving while the next one is generated, and a revision
  rejected by the gate fails only its own job.
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

443 tests cover config and secret bootstrap, security headers, the schema and
repositories, provider adapters over real TCP (including timeouts and refused
addresses), the prompt guard, HTML extraction, the artifact gate, the pipeline's
publish/repair/reject/fail/cancel outcomes, follow-up turns (same model, version
lineage, clipping, a failed revision leaving the published one alone), the
worker's concurrency cap and crash recovery, the submission API, the operator
CLI, the artifact origin's headers and visibility rules, the gallery pages, the
comparison view (sandbox rules, show/hide as URLs, the one-prompt rule), the
make/fork/re-run forms, the same-origin write gate, the submission throttle, the
admin session and CSRF gate (tampering, expiry, throttling, cross-site writes),
and the admin panel's create/edit/moderate behaviour including the ceiling clamps
and allowlist refusals.

`tests/test_e2e.py` substitutes nothing: a scripted OpenAI-compatible endpoint on
a TCP port, the production adapter dialling it because that is the endpoint's
provider type, the queue in the app's own process, and the artifact origin on a
second port. It pins the three things only the wired-together app can get right —
the bytes served are the bytes the model sent with the fence stripped and nothing
else altered, two turns ask the same model and the second carries the first
document back as data, and a rejected artifact reaches no browser at all.

CI runs `pyflakes` and `pytest`, then builds the image and checks a running
container: health, the gallery, the sandboxed frame, the artifact origin's header
set (no `X-Frame-Options`, no `Set-Cookie`), that downloads arrive as
attachments, the submission and comparison pages render with their sandboxing,
that a cross-origin write is refused, and the admin gate end to end — anonymous
redirects, cookie flags, a CSRF refusal, a throttled wrong password, and a real
sign-in that reaches the endpoints page.

A second container job builds *inside* the image, with the scripted endpoint on
the runner and the container on the runner's network: the in-image CLI registers
the endpoint (only because `FP_ENDPOINT_ALLOWLIST` names it) and discovers models,
a prompt posted through the `/new` form publishes carrying the endpoint's tag with
the extraction reported as `fence`, the artifact origin serves those bytes and
nothing else, the download route hands over the same bytes as an attachment, and a
follow-up turn adds v2 on the same model while the build stays published.
