# Sentinel client app

A small, customer-facing web app for the Sentinel API. Static files, no build step, no
dependencies: a person picks a review type, adds a document (or tries a bundled sample with one
click), and gets a plain-language result with the exact quoted evidence for every rule.

- **Results view:** overall status ("Issues found", "Needs human review", "All checks passed"),
  failed and needs-review rules first, passed rules collapsed, each with quoted evidence and page
  numbers, and what was (and was not) sent to public sources.
- **Privacy note:** shows which model endpoint your document is processed by, from the API.
- **Actions:** download the JSON report, print or save as PDF, review another document.
- **Accessible and responsive:** keyboard and screen-reader friendly, dark mode, mobile layout,
  reduced-motion support.

## Two ways to run it

**1. Served by the API (nothing to configure).** The API serves this folder at `/app/`:

```bash
uv run uvicorn sentinel.api:app     # then open http://localhost:8000/app/
```

**2. Hosted anywhere as static files.** Copy this folder to any static host, then:

1. In `config.js`, set `apiBase` to the API's URL, for example `"https://sentinel.example.com"`.
2. On the API, set `CORS_ALLOW_ORIGINS` to the app's origin, for example
   `https://app.example.com`, and restart it.

## Login

If the API has a visitor login (`ACCESS_PASSWORD`), the app shows its own sign-in dialog and sends
the credentials with each request. They are kept in `sessionStorage` (cleared when the tab
closes), never in a cookie. The bundled samples are fetched with the same credentials, and only
from the API's own origin.

## Bundled samples

`samples/` holds copies of the synthetic documents from the repository's `samples/` folder
(a test keeps them byte-identical) plus `index.json`, which maps each one to its review type.
Add your own by adding a file and a manifest entry. Never put real client data in them.

## Security notes

- **`?api=` is off by default.** A crafted link such as `?api=https://evil.example` would make a
  visitor upload their documents to someone else's server. `allowApiParam` in `config.js` turns it
  on for local development only; leave it `false` anywhere real.
- All server data is rendered as text, never as HTML, and links to public sources open with
  `rel="noopener noreferrer"`.

## Tested

The app was driven end to end in jsdom (a simulated browser DOM) against a live API with login
and a review cap: wrong password and retry, one-click sample review, the results view, the
rate-limit message, and cancelling the login. It has not been reviewed visually in a real browser.
