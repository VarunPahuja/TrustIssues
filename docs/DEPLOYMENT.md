# Deployment — backend on Render, frontend on Vercel

The backend is a Python service that needs a Postgres database; the frontend is
a Next.js app that needs one environment variable. Nothing else is required.

`render.yaml` at the repository root is the backend's blueprint, so the build
and start commands live in version control rather than in a web form. Vercel
has no equivalent file for project settings, so its three settings are listed
below and must be entered by hand.

---

## Prerequisites

- The repository pushed to GitHub.
- A Render account and a Vercel account, both signed in with GitHub.
- Nothing installed locally. Both platforms build in the cloud.

**Deploy in this order.** The backend needs the frontend's URL for CORS and the
frontend needs the backend's URL, so the last step goes back and closes the
loop. It is not optional: skipping it leaves a dashboard that renders empty
while the API answers every request correctly.

---

## 1. Backend (Render)

**New + → Blueprint**, pick this repository. Render reads `render.yaml` and
creates the web service and the free Postgres database together, wiring
`DATABASE_URL` between them.

Set `CORS_ALLOW_ORIGINS` when prompted — it is marked `sync: false` precisely
so Render asks. Leave it blank for now; step 3 fills it in.

### Two settings worth understanding rather than copying

**Root Directory must stay blank.** `app` imports `shared.enums`,
`shared.constants` and `shared.contracts`, and `shared/` is a plain directory at
the repository root with no `pyproject.toml` of its own. It resolves only
because the working directory *is* the root. Setting Root Directory to
`backend` makes every one of those imports fail at startup.

**The start command does three things, each safe to repeat:**

```
python -m alembic -c backend/alembic.ini upgrade head &&
python -m app.seed &&
python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT
```

- `alembic upgrade head` — a fresh managed database is empty, so without this
  every endpoint returns 500 on a missing table.
- `app.seed` — creates the three agents and the role users. It checks for
  existing data first and does nothing if already seeded, so restarts never
  duplicate or wipe anything.
- `uvicorn` — no `--reload`: it restarts the worker mid-request and drops
  in-flight simulation decisions. `0.0.0.0` and `$PORT` are required for Render
  to reach the process at all; the default `127.0.0.1:8000` makes the deploy
  hang waiting for a port and eventually fail.

### Verify

```
GET https://<service>.onrender.com/api/v1/health   -> 200, JSON
GET https://<service>.onrender.com/api/v1/agents   -> three agents
```

Three agents means the seed ran: agent-01 at rung 2 (INR 2,500), agent-02 at
rung 0, agent-03 at rung 1. An empty list means the seed step did not run —
read the deploy log for the alembic and seed output.

---

## 2. Frontend (Vercel)

**Add New → Project**, pick this repository.

| Setting | Value |
|---|---|
| Root Directory | `frontend` |
| Framework Preset | Next.js (auto-detected) |
| Production Branch | whichever branch you are deploying (**Settings → Git**) |

Root Directory *is* `frontend` here — the opposite of the backend — because the
Next app is self-contained and its `package.json` lives there.

One environment variable, under **Settings → Environment Variables**:

| Key | Value |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `https://<service>.onrender.com` (no trailing slash) |

This must be set. The fallback in `frontend/src/lib/api-client.ts` is
`http://localhost:8000`, which in a browser means *the viewer's own machine*.

**`NEXT_PUBLIC_` values are inlined into the JavaScript bundle at build time,
not read at runtime.** Changing one requires a redeploy, not a restart.

`NEXT_PUBLIC_API_ROLE` is optional and defaults to `admin`, which the dashboard
needs in order to authorise limit increases and start simulation runs. Set it to
`reviewer` or `auditor` to see the UI as those roles.

Nothing needs setting for MSW: `Providers.tsx` gates the mock server on
`NODE_ENV === "development"`, so a production build cannot serve mock data.

---

## 3. Close the CORS loop

Back in Render → the web service → **Environment**:

```
CORS_ALLOW_ORIGINS=https://<project>.vercel.app,http://localhost:3000
```

Keeping `localhost:3000` in the list leaves local development and the demo
working unchanged. Render restarts the service on save.

**Verify in the browser's Network tab, not by eye.** You want `200`s on the
`/api/v1/...` requests. An empty dashboard and a CORS-blocked dashboard look
identical, which is the entire reason this step gets its own section.

If requests are blocked, the origin string does not match exactly — check for a
trailing slash or `http` where it should be `https`.

---

## Environment variables

| Variable | Default | Why it exists |
|---|---|---|
| `DATABASE_URL` | local compose database | A provider URL beginning `postgres://` is normalised to `postgresql://` automatically; SQLAlchemy 2.0 dropped that alias and its error names a plugin rather than the scheme. |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | Comma-separated. Never falls back to `*`. |
| `AUTH_DEFAULT_ROLE` | `admin` | Role assumed for a request with no `X-User-Role` header. See below. |
| `GOVERNANCE_MODE` | `stub` | Canned opinions, zero LLM calls, no API key. What the demo arc runs on anyway. |
| `PYTHON_VERSION` | — | Pinned in `render.yaml`; Render's default has moved before. |

---

## Authentication: what is and is not true here

**There is no authentication.** Identity is the `X-User-Role` header, which the
caller chooses for itself (`backend/app/deps.py`). This was an explicit scope
decision, not an oversight — the role-check dependency is applied to every
mutating endpoint from the start, so the shape is already right for a real
identity provider to drop into.

What `AUTH_DEFAULT_ROLE=auditor` does, and all it does: a request arriving with
**no** role header is treated as read-only AUDITOR instead of ADMIN. A bare
`curl` against a public URL can therefore read the dashboard's data but cannot
move a limit, start a simulation run, or rule on a decision.

What it does **not** do: stop anyone who sends `X-User-Role: admin`. That is
pinned as a test (`test_an_explicit_admin_header_still_wins`) so the limit stays
visible rather than being mistaken for a security boundary.

The practical consequence: **do not put anything real behind this deployment,**
and do not describe it as production-ready. If asked, the honest answer is that
authentication was out of scope and the role header is the seam where an
identity provider plugs in.

---

## Free-tier behaviour that will affect a live demo

**The service sleeps after about 15 minutes of no traffic**, and the next
request takes roughly 30–60 seconds while it wakes. Open the URL and click
something a couple of minutes before presenting.

**This interacts badly with simulation runs.** `POST /simulation/runs` uses
FastAPI `BackgroundTasks`, so the work happens in the same process after the
response is sent. If the instance sleeps or restarts mid-run, that run stays
`running` for ever with no error, because nothing is watching it. A long arc run
on a free instance is genuinely risky.

**The free database expires** (currently 30 days from creation) and is
size-capped. Create it close to when you need it.

### Recommendation: deploy it, but demo locally

A deployed URL is worth having and it proves the thing ships. But a local run
has no cold start, no background-task fragility, and no CORS surface. If a
reviewer asks why the demo is local, that is a strong answer rather than a weak
one: the free tier sleeps, and simulation runs execute as in-process background
tasks, so a cold start can strand a run mid-flight.
