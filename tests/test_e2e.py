"""One prompt, all the way through, over real sockets.

Every other test swaps something out: an ASGI stand-in as the transport, a fake
provider instead of the pipeline, the artifact origin exercised at the ASGI level
rather than across a socket. This module swaps out nothing.

A scripted OpenAI-compatible server answers on a TCP port; the production
`openai_compat` adapter talks to it because that is the endpoint's provider type;
the queue in the app's own process picks the job up; the artifact origin serves
the result on a second TCP port. The three things checked here are the ones that
only hold when the layers are wired together: the document that reaches the
browser is the one the model sent (fence stripped, nothing else touched), the
model that is asked twice is the same model both times, and an artifact that
fails the gate reaches no browser at all.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import replace

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage.db import Database
from app.storage.repos.endpoints import EndpointRepo
from tests.fake_openai_server import FakeOpenAIServer

PROMPT = "A dice roller with d4 to d20, a rolling history and a big ROLL button"


def app_html(marker: str = "E2E-MARKER") -> str:
    """A document that passes the gate: doctype, one document, nothing external."""
    return (
        "<!DOCTYPE html><html><head><title>e2e app</title>"
        "<style>body{font-family:sans-serif}</style></head>"
        f'<body><h1 id="{marker.lower()}">{marker}</h1>'
        "<script>document.title = 'built';</script></body></html>"
    )


def fenced(html: str) -> str:
    """How a chatty model actually answers: a preamble, then the file in a fence."""
    return f"Sure — here you go:\n\n```html\n{html}\n```\n\nEnjoy!"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LiveEndpoint:
    """A scripted OpenAI-compatible server bound to a real TCP port.

    It runs in its own thread with its own event loop, so the requests the
    adapter makes are ordinary sockets rather than an injected transport — and
    ``completions()`` reports what the endpoint was actually asked, wire format
    included.
    """

    def __init__(self, script: FakeOpenAIServer) -> None:
        import uvicorn

        self.script = script
        self.port = _free_port()
        config = uvicorn.Config(
            script, host="127.0.0.1", port=self.port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        # The test process owns signals; a second server taking SIGTERM would
        # leave the first one running.
        self.server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._thread = threading.Thread(target=self.server.run, name="fake-llm", daemon=True)

    def __enter__(self) -> "LiveEndpoint":
        self._thread.start()
        self._wait_until_listening()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True  # type: ignore[attr-defined]
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "fake endpoint did not shut down"

    def _wait_until_listening(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base_url}/models", timeout=0.5).status_code == 200:
                    return
            except httpx.TransportError as exc:  # still binding
                last_error = exc
            time.sleep(0.05)
        raise AssertionError(f"fake endpoint never answered: {last_error}")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def completions(self) -> list[dict]:
        """The chat/completions bodies, in the order they arrived."""
        return [
            request["body"]
            for request in self.script.requests
            if request["path"].endswith("/chat/completions")
        ]


def _register(db: Database, base_url: str) -> tuple[int, int]:
    """An endpoint of the real provider type, with one model row. Returns the pair."""
    repo = EndpointRepo(db)
    endpoint = repo.create(name="live", base_url=base_url, tags=("live-test",))
    repo.replace_models(endpoint.id, (("fake-1", "Fake One"),))
    model = repo.list_models(endpoint.id)[0]
    return endpoint.id, model.id


def _live_settings(bare_settings: Settings) -> Settings:
    """The app as it really runs: two origins, two ports, queue always eager."""
    artifact_port = _free_port()
    return replace(
        bare_settings,
        host="127.0.0.1",
        artifact_port=artifact_port,
        artifact_base_url=f"http://127.0.0.1:{artifact_port}",
        run_artifact_server=True,
        worker_poll_interval_ms=100,
    )


def _submit(api: TestClient, target: tuple[int, int], body: str = PROMPT) -> int:
    response = api.post(
        "/api/prompts",
        json={"body": body, "targets": [{"endpoint_id": target[0], "model_row_id": target[1]}]},
    )
    assert response.status_code == 202, response.text
    return int(response.json()["builds"][0]["id"])


def _wait_for_build(
    api: TestClient, build_id: int, until: Callable[[dict], bool], timeout: float = 15.0
) -> dict:
    """Poll the build until ``until`` is satisfied.

    Timeout rather than a fixed sleep: the point is that the path completes, and
    a hung path should read as a failure, not as an empty assertion.
    """
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        body = api.get(f"/api/builds/{build_id}").json()
        if until(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"build {build_id} never reached the expected state: {body}")


# --- the whole path ------------------------------------------------------------
def test_a_prompt_becomes_an_app_served_from_the_other_origin(
    db: Database, bare_settings: Settings
) -> None:
    html = app_html()
    with LiveEndpoint(FakeOpenAIServer(completion=fenced(html), model_name="fake-1")) as llm:
        target = _register(db, llm.base_url)
        settings = _live_settings(bare_settings)

        with TestClient(create_app(settings)) as api:
            build_id = _submit(api, target)
            built = _wait_for_build(api, build_id, lambda build: build["latest_version"] == 1)

            # Published, tagged with the endpoint it came from, usage intact.
            assert built["status"] == "published"
            version = built["version"]
            assert version is not None
            assert version["bytes"] == len(html.encode())
            assert version["model_returned"] == "fake-1"
            assert version["usage"]["completion_tokens"] == 22
            assert built["failures"] == []
            assert "live-test" in built["tags"]

            # Asked exactly once, for the model that was picked, with the prompt
            # wrapped as data inside the fixed builder prompt.
            asks = llm.completions()
            assert [ask["model"] for ask in asks] == ["fake-1"]
            messages = asks[0]["messages"]
            assert [m["role"] for m in messages] == ["system", "user"]
            assert f"<task>\n{PROMPT}\n</task>" in messages[1]["content"]

            # Browsable: on the home page, in the gallery, framed from :8081.
            assert f"/build/{build_id}" in api.get("/").text
            assert f"/build/{build_id}" in api.get("/gallery").text
            page = api.get(f"/build/{build_id}").text
            assert f'src="{settings.artifact_base_url}/artifact/{version["id"]}"' in page
            assert 'sandbox="allow-scripts"' in page
            assert "allow-same-origin" not in page

            # The artifact origin, across a real socket: the document the model
            # sent, with the fence stripped and nothing else altered.
            served = httpx.get(f"{settings.artifact_base_url}/artifact/{version['id']}")
            assert served.status_code == 200
            assert served.text == html
            assert "```" not in served.text
            assert "connect-src 'none'" in served.headers["content-security-policy"]
            # Nothing on that origin can identify a visitor or be framed by a
            # third-party page.
            assert "set-cookie" not in served.headers
            assert "x-frame-options" not in served.headers
            assert "frame-ancestors" in served.headers["content-security-policy"]

            # And the same bytes as a file, from the gallery's own origin.
            download = api.get(f"/download/{version['id']}")
            assert "attachment" in download.headers["content-disposition"]
            assert download.text == html

            # The comparison view frames it too, under the same sandbox rules.
            comparison = api.get(f"/compare?b={build_id}").text
            assert 'class="compare compare-single"' in comparison
            assert comparison.count("<iframe") == 1


def test_a_follow_up_turn_asks_the_same_model_again(
    db: Database, bare_settings: Settings
) -> None:
    first, second = app_html("v1"), app_html("v2")
    script = FakeOpenAIServer(completion=fenced(first), model_name="fake-1")

    with LiveEndpoint(script) as llm:
        target = _register(db, llm.base_url)
        settings = _live_settings(bare_settings)

        with TestClient(create_app(settings)) as api:
            build_id = _submit(api, target)
            built = _wait_for_build(api, build_id, lambda build: build["latest_version"] == 1)
            assert built["status"] == "published"

            script.completion = fenced(second)
            queued = api.post(
                f"/api/builds/{build_id}/refine",
                json={"instruction": "make the ROLL button bigger"},
            )
            assert queued.status_code == 202

            again = _wait_for_build(api, build_id, lambda build: build["latest_version"] == 2)
            assert again["status"] == "published"
            chain = again["versions"]
            assert [v["version_no"] for v in chain] == [1, 2]
            assert chain[1]["parent_version_id"] == chain[0]["id"]
            assert chain[1]["refined"] is True

            # The wire proves the rule: same endpoint, same model, and the second
            # turn carries the first document back as data.
            asks = llm.completions()
            assert [ask["model"] for ask in asks] == ["fake-1", "fake-1"]
            user = asks[1]["messages"][1]["content"]
            assert f"<current_html>\n{first}\n</current_html>" in user
            assert "<instruction>\nmake the ROLL button bigger\n</instruction>" in user

            # v2 is what the artifact origin now serves; v1 is still there.
            for version_id, document in ((chain[0]["id"], first), (chain[1]["id"], second)):
                served = httpx.get(f"{settings.artifact_base_url}/artifact/{version_id}")
                assert served.status_code == 200 and served.text == document
            assert httpx.get(f"{settings.artifact_base_url}/artifact/{chain[1]['id']}").text == second


def test_an_unshippable_document_reaches_no_browser(
    db: Database, bare_settings: Settings
) -> None:
    bad = (
        "<!DOCTYPE html><html><head>"
        "<script src='https://cdn.example/app.js'></script>"
        "</head><body><b>needs a network</b></body></html>"
    )

    with LiveEndpoint(FakeOpenAIServer(completion=fenced(bad), model_name="fake-1")) as llm:
        target = _register(db, llm.base_url)
        settings = _live_settings(bare_settings)

        with TestClient(create_app(settings)) as api:
            build_id = _submit(api, target)
            failed = _wait_for_build(api, build_id, lambda build: build["status"] == "rejected")

            assert failed["latest_version"] == 0
            assert failed["version"] is None
            assert any("external-url" in f["violations"] for f in failed["failures"])

            # The gate does not simply fail: it sends its verdict back once. The
            # repair retry quotes the rejected document and names the violation,
            # and the fake keeps answering badly, so this is the last request.
            asks = llm.completions()
            assert len(asks) == 1 + settings.job_repair_retries
            assert "external-url" in asks[-1]["messages"][-1]["content"]
            assert bad in asks[-1]["messages"][-2]["content"]

            # Nothing was published, so there is nothing to browse, frame or fetch.
            assert "Nothing published yet" in api.get("/").text
            assert api.get("/gallery").text.count(f"/build/{build_id}") == 0

            # The build page says what happened and frames nothing. (The
            # artifact origin is still named in the page's meta tag — that is how
            # the pages know where frames come from — but no artifact path does.)
            page = api.get(f"/build/{build_id}").text
            assert "<iframe" not in page and "/artifact/" not in page
            assert "did not pass the gate" in page

            # The comparison says what happened instead of framing an empty column.
            comparison = api.get(f"/compare?b={build_id}").text
            assert "<iframe" not in comparison and "did not pass the gate" in comparison
            assert "hx-get" not in comparison  # terminal: the comparing page stops asking

            # And the artifact origin has no bytes for it to hand out.
            assert httpx.get(f"{settings.artifact_base_url}/artifact/1").status_code == 404
