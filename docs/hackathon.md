# Nebius x NVIDIA Global AI Hackathon: track, requirements, and going live

Source: the official [rules](https://nebiusglobalaihackathon.devpost.com/rules), read on
2026-09-23. Check them again before you submit: deadlines and wording can change.

## Track

**02, Best Apps and Agents.** "Build any app or agent someone would actually use", leveraging
Nemotron models through Token Factory. Sentinel is a working application for a real job
(compliance review) built on Nemotron. The other tracks do not fit: Coding needs Token Factory
Sandboxes, Personal AI needs persistent memory and tools such as NemoClaw or Hermes Agent, and
Physical AI needs hardware footage.

Sentinel also qualifies for the separate **Best Use of Tavily** bonus award, which can be won in
addition to a track award. That is worth designing the demo around: a live, scoped Tavily check
that cites a public source (and refuses to assume when it finds none).

Judging: stage one is pass/fail (fits the theme, uses the required APIs). Stage two scores four
equally weighted criteria: Technological Implementation, Design, Potential Impact, and Quality of
the Idea.

## Requirements and where we stand

| Requirement (from the rules) | Status |
| --- | --- |
| Runs on Token Factory or AI Cloud, and uses an NVIDIA open model. Token Factory counts if "the project makes a runtime call to the Token Factory inference API". | **Done.** Sentinel calls `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` through Token Factory. Verified live on 2026-09-23: 24 of 24 sample verdicts as expected. |
| A URL to a working demo. A private one is allowed if you include login credentials in the testing instructions, and it must be free for the judges to use. | **To do.** Deploy with the runbook below; give judges the visitor login. |
| A public demo video under 3 minutes on YouTube, with audio explaining how you used Token Factory and Nemotron. | **To do.** A script is in [deploy/README.md](../deploy/README.md#7-demo-checklist). |
| A public code repository with all the source and setup instructions. | **Done.** The repository is public. |
| An open-source license file (Apache 2.0, MIT, or MPL 2.0). | **Done.** [`LICENSE`](../LICENSE) is MIT, Copyright (c) 2026 Sentinel PLD. Confirm GitHub shows "MIT" on the repository page once it is on `main`. |
| README highlights Nemotron and Token Factory usage. | **Partly.** Update the README's "How it works" once the demo runs on Token Factory. |
| No personal data in the submission. | **Done.** Every sample is synthetic. Tell judges not to upload real data. |
| Submission window closes **2026-10-30, 10:00 AM PT**. | Judging is December 1 to 15; winners are announced January 11, 2027. |

## Going live on the Nebius stack

Architecture: the **model** runs on Token Factory (a runtime call to the inference API); the
**app** runs on a small Nebius AI Cloud VM, in Docker, behind Caddy for HTTPS. Judges open
`https://<your-domain>/app/`, sign in with the visitor login, and click a sample.

The single biggest upgrade to the privacy story is a Token Factory **dedicated endpoint**
(single-tenant; see [deploy/README.md](../deploy/README.md#path-c-token-factory-dedicated-endpoint)),
if Nemotron 3 Nano is available as a template. A shared endpoint works for a demo with synthetic
documents, but say so plainly in the video and README rather than calling it single-tenant.

### Steps

1. **Validate your configuration locally.** Fill in `.env` (Token Factory endpoint, model id, both
   keys, plus `ACCESS_PASSWORD`, `ADMIN_PASSWORD`, `MAX_REVIEWS_PER_HOUR`, `SENTINEL_DOMAIN`), then:

   ```bash
   chmod 600 .env
   uv run sentinel preflight --public
   ```

   Do not continue until it says `READY`. It catches the mistakes that are easy to make: a model id
   that is not in the catalog (`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, exact capitalization), a
   key that is rejected, a weak password.

2. **Create a small VM** in the Nebius console (a CPU VM with 2 vCPUs and 4 to 8 GB is plenty; the
   app only calls out to APIs). Give it a public IP and allow inbound ports 80 and 443 in its
   network settings. I could not verify Nebius's exact console steps or CLI names for a CPU VM;
   the GPU VM commands in the deploy guide show the pattern.

3. **Point a domain at it.** Create an `A` record for your hostname pointing at the VM's public IP,
   and remove any registrar redirect or parking record on the same host (at Namecheap, a "URL
   Redirect Record" overrides an A record). Optionally add a `CNAME` for `www` pointing at the
   domain: Caddy redirects `www` to the main site. Do not add an `AAAA` record (the VM is IPv4
   only), and leave any MX records alone. Caddy needs the `A` record to get a certificate. Check
   it with `dig +short A <your-domain> @1.1.1.1`; a resolver may serve the old record until its
   cache expires, up to the old record's TTL. If you have no domain, a name like
   `<ip-with-dashes>.sslip.io` resolves to that IP; that is a common trick that I have not tried
   with Caddy here.

4. **Install Docker and get the code** on the VM:

   ```bash
   curl -fsSL https://get.docker.com | sh
   git clone https://github.com/talacha/sentinel.git && cd sentinel
   ```

5. **Copy your `.env` to the VM** securely (for example `scp .env user@<ip>:sentinel/.env`, then
   `chmod 600 .env` there). It holds your keys: never commit it or paste it into a chat.

6. **Start it:**

   ```bash
   cd deploy/public
   docker compose --env-file ../../.env -f docker-compose.yml up -d --build
   ```

   Caddy obtains a certificate on first request; give it a minute. Only ports 80 and 443 are
   published, and the app is reachable only through Caddy.

7. **Verify from your laptop.** Open `https://<your-domain>/status`, sign in as the admin, and
   click **Run live checks**. Everything should pass: the key works, the model exists, a real
   reasoning call returns JSON, Tavily answers. If the model id is wrong, fix it on the
   **Configuration** tab (no restart needed) and run the live checks again. Then open
   `https://<your-domain>/app/`, sign in as a visitor, and run every bundled sample once.

8. **Write the Devpost testing instructions:**

   > Open `https://<your-domain>/app/`. Sign in with username `<ACCESS_USER>` and password
   > `<ACCESS_PASSWORD>`. Choose a review type and click one of the sample documents (all
   > synthetic), or upload your own synthetic file. Please do not upload real personal data. The
   > service allows `<MAX_REVIEWS_PER_HOUR>` reviews per hour in total.

9. **Afterwards:** rotate the passwords (the Users page at `/admin/users` does it instantly, with no
   restart), and delete the VM when judging is over. Nebius bills while
   it exists.

### Protecting your credits

A public URL can be used by anyone who has the login, so bound the damage: `MAX_REVIEWS_PER_HOUR`
caps total reviews (a review is about a dozen small model calls and at most `MAX_SEARCHES_PER_REVIEW`
searches), `MAX_UPLOAD_MB` and `MAX_DOCUMENT_CHARS` cap the size of each, and the admin console
lets you lower any of them instantly without a restart. Check your Token Factory and Tavily usage
dashboards during judging.

## Submission text: points that map to the criteria

- **Technological Implementation:** Nemotron 3 Nano through Token Factory, structured output with
  local validation, deterministic checks instead of model arithmetic, every cited quote verified
  against the document, a fail-closed policy, scoped Tavily verification with an egress guard, a
  preflight and a live status console.
- **Potential Impact:** insurers, law firms, and hospitals need AI review but cannot use
  multi-tenant APIs for client data; the same engine serves all three by swapping the vault.
- **Design:** the client app (plain-language results, evidence, one-click samples) and the
  "never guess: needs review" principle.
- **Quality of the Idea:** it generalizes an existing anti-money-laundering product (Sentinel PLD)
  that already runs self-hosted in pilots.

Do not claim anything the demo does not show. In particular, say which model endpoint
processes the documents in the deployed demo.
