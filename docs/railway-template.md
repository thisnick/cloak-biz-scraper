# The Railway template

The one-click contract lives in **Railway's dashboard, not in git**. `railway.json`
covers build/deploy/healthcheck/restart/cron and **cannot express variables, volumes,
or sleep** — which is most of what this template is. So this file is the source of
truth for what the template must contain, and the thing to diff against if a deploy
ever behaves oddly.

Everything below was measured against the real API on 2026-07-17, not read off a doc.

## What the template must produce

One service, from this repo, with:

| | value | why |
|---|---|---|
| source | `thisnick/cloak-biz-scraper`, branch **`release`** | never `main` — see "Branch" below |
| volume | mount path **`/data`** | settings, the Chromium binary cache, profiles, jobs, evidence |
| healthcheck | `/healthz` | unauthenticated by design |
| variables | **`APP_SECRET` = `${{secret(32)}}`** and nothing else | decision #4: exactly one variable |
| sleep | **`deploy.sleepApplication: true`** | without it users pay 24/7 — see "Sleep" |
| domain | `serviceDomains: {"<hasDomain>": {}}` | the app is useless without a public URL |

## The config

Shape verified against shipped repo+volume templates (`n8n`, `ghost`, `vaultwarden`).
`volumeMounts` is keyed by an arbitrary UUID; `<hasDomain>` is a literal placeholder key.

```json
{
  "services": {
    "<service-uuid>": {
      "icon": null,
      "name": "cloak-biz-scraper",
      "deploy": {
        "startCommand": null,
        "healthcheckPath": "/healthz",
        "restartPolicyType": "ON_FAILURE",
        "restartPolicyMaxRetries": 10,
        "sleepApplication": true
      },
      "source": {
        "repo": "thisnick/cloak-biz-scraper",
        "branch": "release",
        "rootDirectory": null
      },
      "networking": {
        "serviceDomains": { "<hasDomain>": {} }
      },
      "volumeMounts": {
        "<volume-uuid>": { "mountPath": "/data" }
      },
      "variables": {
        "APP_SECRET": {
          "description": "Log in to this server with this. Copy it from the Variables tab after deploying. You can rotate it later in the app's settings.",
          "defaultValue": "${{secret(32)}}",
          "isOptional": false
        }
      }
    }
  }
}
```

## Sleep — the part generation gets wrong

Railway's default is `sleepApplication: false`, and **`templateGenerate` silently drops
the key**. A generated template therefore **never scales to zero and the user pays 24/7**,
which is the whole cost story. It must be hand-edited back in, and placement is exact —
the validator is strict and names the offending key, which makes it a useful oracle:

- `services.<id>.sleepApplication` → **REJECTED** (`unrecognized_keys`)
- `services.<id>.deploy.sleepApplication: true` → **ACCEPTED** ✅

**`templateGenerate` also strips constant variable values.** `PLAIN_VAR="constant"` comes
back with no `defaultValue` and deploys as a blank required field. `secret()` values are
unaffected — they are stored literals, not live expressions.

**So: never ship what generation hands you. Audit the `serializedConfig` against the table
above, field by field.**

Measured: idle → `SLEEPING` at **~6–6.7 min** (the documented 10 is an upper bound), and the
first inbound request woke it and was served in **1.1s**.

## Branch — why `release`, and what actually pins it

The template tracks **`release`**, never `main`. Otherwise Nick's `main` *is* production for
the whole community and every push broadcasts an update notice. Develop on `main`; **merging
to `release` is what ships.** Update tracking is branch-specific: pushing to the pinned
branch flips `isUpdatable` true; pushing to `main` leaves it false. Users are *notified* and
choose when to apply — a push does not auto-deploy them.

Only **`serializedConfig.source.branch`** actually pins a branch. Do not trust the
alternatives:

- `serviceCreate`/`serviceConnect` accept a `branch` argument and **silently ignore it**,
  building the repo's default branch instead. There is no `ServiceInstance.branch` field to
  read it back from.
- **`deployment.meta.branch` lies.** It reported `"main"` on a deploy whose `commitHash` was
  provably a probe branch's tip. **Verify a pin by `commitHash`, or by `RAILWAY_GIT_COMMIT_SHA`
  inside the container — never by `meta.branch`.**

## 🔴 Generating the template requires a GitHub connection

`templateGenerate` **refuses a repo-sourced service** unless the workspace has a GitHub App
installation:

```
Service cloak-biz-scraper does not have a source that can be used to generate a template
```

Measured as a matched pair on the same account: an **image**-sourced service generates fine;
a **repo**-sourced one is refused — including for an unrelated public repo, so it is not
about this repo. `githubRepoDeploy` fails the same way (`Could not find latest commit for
repo`), and `repoTriggers` is empty (`NO_INSTALLATION`).

The coherent model:

- **Without** a GitHub install, Railway will *anonymously clone a public repo to build*, and
  nothing more. (This is proven — the image builds and deploys.)
- **With** one, the repo-aware features work: template generation, branch pinning,
  `repoTriggers`, autodeploy.

**This is a one-time action for the template author (Nick). It does not touch the user
story: deployers still need no GitHub account** — Railway clones this public repo for them
anonymously. Those two roles are easy to conflate; they are not the same.

There is no way around it: the live schema has **no `templateCreate` and no `templateUpdate`**
(only `templateGenerate|Clone|Delete|Publish|Unpublish|DeployV2|VolumeUpdate`), and
`templateClone` takes only `{code, workspaceId}` with no config override. A template's stored
config can *only* come from generation, and the deploy button serves that **stored** config —
`templateDeployV2(serializedConfig=…)` passes config at deploy time, which is not what a user
clicking the button gets.

## Do NOT publish

An **unpublished** template is deployable by link, invisible to `templateSearch`, and **still
delivers update notifications**. Publishing only adds a public marketplace listing attributed
to the workspace name ("Nick Yu's Projects"). Measured on an unpublished template: `template(code:…)`
returns the full config **with no auth**, and `railway.com/new/template/<code>` returns HTTP 200
unauthenticated. That *is* "unlisted", and it is what we want.

Publishing is reversible (`templateUnpublish`, `templateDelete`), but marketplace caching and
already-deployed users are not.

## Deploying from a Docker image instead — don't

Railway: *"we do not have a mechanism to check for updates to Docker images from which services
may be sourced"*. Image-sourced templates **lose update notifications entirely** — the wrong
trade for an audience that will never manually redeploy.

## Gotchas that cost real time

- **`VOLUME` in the Dockerfile makes the image unbuildable here.** Railway rejects it at parse
  time (`docker VOLUME at Line N is not supported, use Railway Volumes`); the build dies in ~3s
  before any layer runs and **before any log exists**, so it reads as an infrastructure fault.
  The mount comes from outside: `data:/data` in compose, `volumeMounts` here.
- **Editing a variable does not auto-redeploy.** It creates *staged changes* the user must
  review and deploy. Say so in the setup guide.
- `templateDeployV2` requires a `templateId`; `serializedConfig` alone 400s opaquely.
- The API bypasses variable staging (`variableUpsert` applies immediately); staging is
  dashboard-only.
