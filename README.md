# fun-prompts

A series of fun prompts and the tiny web apps they built — with a searchable
gallery to browse them all.

## Quick start

Run the gallery locally (no build step needed):

```sh
python3 -m http.server 8080
# open http://localhost:8080/gallery/
```

Or in a container (rootless-friendly):

```sh
podman build -t fun-prompts .
podman run -d -p 8080:8080 localhost/fun-prompts
# open http://localhost:8080  (redirects to the gallery)
```

## Gallery

The gallery (`gallery/`) reads the `apps.json` manifest and renders one card
per app with its prompt, labels, and an "Open app" link. Search the text box
or click label chips to filter (labels are AND-combined; search terms match
titles, summaries, and the full prompt text).

## Adding an app

1. Write the prompt: `prompts/<id>.md`
2. Build the app as a self-contained page: `apps/<id>/index.html`
3. Add an entry to `apps.json`:

```json
{
  "id": "my-app",
  "title": "My App",
  "icon": "✨",
  "summary": "One-line description.",
  "tags": ["games", "tools"],
  "prompt": "prompts/my-app.md",
  "app": "apps/my-app/"
}
```

No rebuild of the gallery itself is needed — it picks up the manifest at
runtime. Apps are vanilla HTML/CSS/JS with no dependencies.

## Layout

```
apps.json          manifest: title, summary, labels, prompt + app paths
prompts/           the prompts (markdown, one per app)
apps/<id>/         one self-contained static app per directory
gallery/           static gallery: index.html + app.js + style.css
nginx/             container server config (port 8080)
Containerfile      podman build -t fun-prompts .
```
