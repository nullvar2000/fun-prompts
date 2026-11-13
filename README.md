# fun-prompts

A gallery of zero-shot web applications built by AI — and the builder that makes
them. Point it at any OpenAI-compatible LLM endpoint, submit a prompt, and it
fans that prompt out to up to four models at once, validates what comes back,
tags it with the model that produced it, and publishes it to the gallery.

**Status:** M0 (skeleton) — config, security headers, container and CI are in
place. Gallery, builder and admin land in M1–M7.

| Milestone | What lands |
| --- | --- |
| M0 | Config, logging, app skeleton, Containerfile, CI, fake-LLM test harness |
| M1 | SQLite schema, migrations, repositories, seed import |
| M2 | Provider adapters: `chat/completions`, `/v1/models` discovery |
| M3 | Prompt guard, artifact extract/validate, job queue + worker |
| M4 | Gallery, home "3 random", build pages, artifact origin on :8081 |
| M5 | Admin: endpoints, models, tags, moderation, failure logs |
| M6 | Side-by-side comparison (up to 4), refine with version history |
| M7 | E2E tests, container smoke test in CI, docs |

## Quick start — local

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Secrets are generated into data/first-run.txt if you don't set them.
FP_DATA_DIR=./data FP_HOST=127.0.0.1 .venv/bin/python -m app.main
# gallery  http://127.0.0.1:8080
# health   http://127.0.0.1:8080/healthz
```

## Quick start — container

```sh
podman build -t fun-prompts -f Containerfile .
podman run -d --name fun-prompts \
  -p 8080:8080 -p 8081:8081 \
  -v fun-prompts-data:/data \
  -e FP_ARTIFACT_BASE_URL=http://localhost:8081 \
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
  config.py          env-first settings, validation, secret bootstrap
  providers/         plugin boundary for LLM backends (base.py = the contract)
  web/               security headers, Jinja templates, JSON API, artifact server
gallery_static/      style.css + vendored htmx.min.js (no CDN anywhere)
prompts/             seed prompts (markdown)
seed/apps/           pre-built HTML imported as seed builds
tests/               unit/API tests + FakeProvider (production code can't reach it)
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
  and logs them (`guard_log`).
- **SSRF guard** is allowlist-based (`FP_ENDPOINT_ALLOWLIST`) and always blocks
  metadata/link-local addresses. Blanket private-range blocking is deliberately
  not used — your Ollama/vLLM box lives on the LAN.
- **Artifact validation** rejects anything that isn't a single HTML document
  under the size cap with no external URLs, before it is ever published.

## Tests

```sh
.venv/bin/python -m pytest
```

21 tests cover config defaults/overrides/validation, secret bootstrap and file
permissions, security headers, the CSP following the configured artifact
origin, and the provider protocol via `tests/fakes.FakeProvider`.
