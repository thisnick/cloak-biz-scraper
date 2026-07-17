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

## 🔴 Generating the template requires GitHub — *and a reconnect afterwards*

`templateGenerate` **refuses a repo-sourced service** whose source Railway cannot resolve
through the GitHub App:

```
Service cloak-biz-scraper does not have a source that can be used to generate a template
```

Measured as a matched pair on the same account: an **image**-sourced service generates fine;
a **repo**-sourced one is refused — including for an unrelated public repo, so it was never
about this repo.

**The sharp edge: installing the GitHub App is not sufficient.** After Nick installed it,
`serviceInstanceAutoDeployStatus` flipped `{canEnable:false, reason:"NO_INSTALLATION"}` →
`{canEnable:true, reason:null}` — and `templateGenerate` **still refused**. The service had
been connected *before* the install, and that earlier `serviceConnect` had silently stored a
source Railway would not template. Re-running the identical `serviceConnect` afterwards
created the `repoTrigger` and generation was accepted immediately:

```
repoTriggers: [{branch: "release", repository: "thisnick/cloak-biz-scraper", provider: "github"}]
templateGenerate -> ACCEPTED (code rXxuD5, UNPUBLISHED)
```

So the real requirement is **a `repoTrigger`**, which needs the App installed *and* the
service connected after it. Anyone who installs the App and sees the same refusal will
conclude the install failed. It didn't — reconnect the service.

The model, corrected:

- **Without** a GitHub install, Railway will *anonymously clone a public repo to build*, and
  nothing more. (Proven — the image built and deployed for hours this way.)
- **With** one, plus a reconnect, the repo-aware features work: template generation, a real
  `repoTrigger`, branch pinning, autodeploy.

**This is a one-time action for the template author. It does not touch the user story:
deployers still need no GitHub account** — Railway clones this public repo for them
anonymously. Those two roles are easy to conflate; they are not the same.

Note there is **no `templateCreate` and no `templateUpdate`** in the live schema (only
`templateGenerate|Clone|Delete|Publish|Unpublish|DeployV2|VolumeUpdate`), and `templateClone`
takes only `{code, workspaceId}` with no config override. A template's stored config can
*only* come from generation, and the deploy button serves that **stored** config —
`templateDeployV2(serializedConfig=…)` passes config at deploy time, which is *not* what a
user clicking the button gets. That constraint is what makes the strips below dangerous.

## 🔴 What generation actually stripped (measured on OUR repo source)

Generated config for our service, audited field by field against the table above:

| field | intended | generated |
|---|---|---|
| `deploy.healthcheckPath` | `/healthz` | ✅ `/healthz` |
| `variables.APP_SECRET` | `${{secret(32)}}` | ✅ `${{secret(32)}}`, `isOptional:false` |
| `volumeMounts.<id>.mountPath` | `/data` | ✅ `/data` |
| `networking.serviceDomains` | `<hasDomain>` | ✅ present |
| **`deploy.sleepApplication`** | **`true`** | 🔴 **ABSENT** |
| **`source.branch`** | **`release`** | 🔴 **ABSENT** |

`sleepApplication` was expected. **`source.branch` was not** — and it is the more dangerous of
the two, because it is silent in a different way. The service *is* pinned: `repoTriggers` says
`branch: "release"`. Generation reads that and **emits no branch key at all**, so the template
falls back to the repo's **default branch** for every user who clicks the button. The entire
"track `release`, never `main`" strategy evaporates without a single error message.

`secret()` values survive — they are stored literals, not live expressions.

**Both strips are proven, not inferred.** With `main` at `3b0bd45` and `release` at `0a5eb98`,
deploying the template's **stored** config — exactly what the button does — produced:

```
commitHash : 3b0bd456   -> main, NOT release
deployed sleepApplication: False   -> pays 24/7
```

And the hand-edit fixes both. The same config with `deploy.sleepApplication: true` and
`source.branch: "release"` added deploys:

```
commit 0a5eb98d -> RELEASE ✅      sleepApplication: True ✅      healthcheckPath: /healthz
```

So the two keys are **valid and sufficient** — the strict validator accepts them and the
deployed service honours them. The problem is not knowing what to write. It is where to put it.

## 🔴 The API cannot ship this template. The dashboard has to.

**`templateDeployV2(serializedConfig=…)` is deploy-time only — it does not write back.**
Measured: deploy the hand-edited config, then re-read the template — **the stored config is
byte-identical to before**. Combined with there being no `templateCreate`/`templateUpdate`, and
`templateClone` taking only `{code, workspaceId}`:

> Every API path that can *store* a template config is generation, and generation strips the
> two keys that matter. The hand-edit can only ever be passed at deploy time, which is not
> what a user clicking the button gets.

**The branch has a documented dashboard-only form.** Railway's template editor takes *"the full
URL to the desired branch in the Source Repo configuration"*:

```
https://github.com/thisnick/cloak-biz-scraper/tree/release
```

This is **not** a service-source form — `serviceConnect` rejects that URL outright (`Problem
processing request`) and leaves the plain `owner/repo`. It only means something in the template
editor. Railway's docs also confirm templates can be edited from the workspace template page.

**So the template must be authored/edited in the Railway dashboard**, which is consistent with
the rest of this file: the one-click contract lives there, not in git. That is exactly why this
document exists.

**`release` IS the repo's default branch — and that is load-bearing, not cosmetic.** Generation
drops the branch *silently*, and every future regeneration will drop it again. Because `release`
is the default, a branch-less template deploys `release` anyway: the failure mode is now
harmless instead of invisible. Develop on `main`; **merging to `release` is what ships**, and the
repo's front page shows what users actually deploy.

> ⚠️ **Do not "tidy" the default branch back to `main`.** That single settings change would
> silently repoint every future one-click deploy at development code, with no error anywhere.
> This is the only thing standing between a regenerated template and shipping `main` to users.

Gotcha while setting it: **`gh repo edit --default-branch release` silently does nothing** —
exit 0, no output, no change. `gh api -X PATCH repos/<owner>/<repo> -f default_branch=release`
works.

## 🔴 Sleep appears to be UNSHIPPABLE in a template. Measured against the whole marketplace.

Railway's template docs never mention sleep / serverless / `sleepApplication`, so rather than
guess, every public template was surveyed (read-only; `templates` + `serializedConfig` need no
auth). **N = 1500 templates, 2964 services.**

**`sleepApplication`: 0 occurrences.** Not in `deploy.*`, not at service level, not anywhere —
and the search was for `sleep|serverless|scale|idle` case-insensitively, not just our one
hypothesis, precisely because the UI calls this "Serverless"/"App Sleeping" and might store it
under another name. The 50 raw-text matches are all false positives: shell `sleep 3` in start
commands, "scale to multiple replicas" in a variable description, and the word "Tail**scale**".

The complete observed vocabulary of `services.<id>.deploy.*` in stored configs:

| key | services | | key | services |
|---|---|---|---|---|
| `startCommand` | 1882 | | `preDeployCommand` | 47 |
| `healthcheckPath` | 1854 | | `cronSchedule` | 10 |
| `restartPolicyMaxRetries` | 1669 | | `healthcheckTimeout` | 9 |
| `restartPolicyType` | 1663 | | `drainingSeconds` | **2** |
| `requiredMountPath` | 229 | | `numReplicas` | **1** |
| | | | **`sleepApplication`** | **0** |

**The tail is what makes this convincing.** "Nobody wanted sleep" would be a fair objection to a
bare zero — plenty of templates are databases that *shouldn't* sleep. But it does not survive
the tail: `numReplicas` appears **once** in 2964 services and `drainingSeconds` twice, so rare
keys *are* visible when they are expressible. That not one author in 1500 managed to store the
platform's headline scale-to-zero feature is far better explained by **no authoring path emits
it** than by universal disinterest.

This sits exactly alongside the other measured facts: the validator **accepts**
`deploy.sleepApplication: true` and a deployed service **honours** it (so the key is real), but
that was only ever achieved through `templateDeployV2`'s *deploy-time* config — which does not
write back. Valid in the schema; producible by no authoring path.

**Consequence — this changes the cost story, not a setting.** If a template cannot ship
scale-to-zero, **every user must enable it themselves after deploying**, or they pay 24/7. That
must be a step in the setup guide with the same weight as pasting `APP_SECRET`, and the README
cannot claim "you only pay while a sweep runs" until the user has done it.

### What skipping the toggle actually costs — measured, because the guess was wrong twice

Taken from `metrics(MEMORY_USAGE_GB)` on the real deployment, read against windows whose state
was known independently (an idle sleep-watch; sweeps identified by their job logs):

| state | memory | 24/7 cost at ~$10/GB/mo |
|---|---|---|
| asleep | 0 | $0 |
| awake, fresh boot, no job | **0.121 GB** (flat for 10 min) | ~$1.20/mo |
| **awake after a sweep, no job** | **0.78–0.92 GB** (flat 5 min, then slept) | **~$8–9/mo** |
| sweep running | 1.2–1.6 GB peak | pennies per sweep |

Idle CPU is negligible (~0.0015 vCPU ≈ $0.03/mo); memory is the whole bill.

**The third row is the finding, and neither the naive estimate nor the "it's only uvicorn"
estimate would have produced it: memory is not reclaimed when a sweep's browsers exit.** Idle
after boot is 0.121 GB, but idle *after a sweep* sits at ~0.9 GB until the process dies — and
**sleeping is what kills it.** So sleep is doing double duty: it is the billing story *and* the
memory-reclamation story. A never-sleeping instance doesn't cost $1.20/mo, it costs ~$8–9/mo —
**more than the $5 the Hobby plan includes** — and anyone actually using the product will have
run a sweep.

Don't cry wolf and don't undersell: **~$1.20/mo if it never scrapes, ~$8–9/mo once it has.**
(The 5 GB volume bills either way and is not part of this delta.)

### Does it plateau, or climb forever? Tested, because ~$8–9 assumed the answer.

Every idle-after-sweep reading above came from a wake containing **exactly one sweep**, each
ended by a sleep that reset the process. So "post-sweep idle = 0.9 GB" was really "post-*first*-
sweep idle", and the price above assumed it plateaus — in the one scenario the docs describe,
where a user skips Serverless and the process **never** resets.

**Two things were measured.**

**1. Awake, it does not let go.** One sweep, then the service held awake for **20 minutes** by
polling `/healthz` every 20s (a response is outbound, so the sleep clock cannot fire — status
stayed `SUCCESS` throughout, far past the 6–10 min threshold). Memory sat at **exactly 0.858 GB
for 20 consecutive samples**, flat. So "sleeping is what reclaims it" is **confirmed**, now by a
test where sleep was excluded by construction rather than by never having waited long enough.

**2. Across sweeps it creeps, slowly, and does not run away.** Six sweeps in one process
(continuous trace, no drop to ~0.07 = no sleep or restart in between):

| sweep | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|
| idle GB | 0.800 | 0.821 | 0.833 | 0.843 | 0.858 |

Monotonic: **+21, +12, +10, +15 MB — about +14 MB per sweep, ~58 MB over four sweeps.**

So: **not a plateau, but not a runaway either.** Linear extrapolation (from five points, so
treat as an order of magnitude, not a forecast) puts a daily sweeper who never sleeps at ~1.2 GB
(~$12/mo) after a month. The 48 GB ceiling is never approached, so **cost is the failure mode,
not an OOM** — and any redeploy or platform restart resets it. **~$8–9/mo is the right number
for the README**, drifting upward for a long-lived never-sleeping process.

⚠️ **Two of the checks in that run were themselves wrong — noted so nobody trusts them again:**
- **Counting `ready:` lines is not a restart invariant.** `deploymentLogs` is a rolling window,
  so the count went **6 → 5** and the script declared "same process: False". A restart would
  make it go *up*. The real invariant is the memory trace: a restart shows as a drop to ~0.07
  and a climb back, and 20 flat samples show neither.
- **An outlier fooled the verdict.** The first reading (1.555 GB) caught a browser still tearing
  down; comparing it to the last produced a *negative* delta and a confident "PLATEAUS" that hid
  a monotonic climb. The series is the evidence, not the endpoints.

### 🔴 PENDING: the same false claim is live in the product

`app/templates/index.html` (the Pool section, next to `max_instances`) still says:

> *"…because the service sleeps when idle, **you only pay while a sweep is actually running**
> — a 3-minute sweep with 4 browsers costs pennies."*

It states as fact the thing the template **cannot** deliver, and it is the copy a user reads
while deciding how many browsers to run. Untouched only because Step 5's review is in flight
and this is app code. **Apply the moment the review signs off** — the numbers are measured, and
the comment above it ("the honest framing is that it's cheap because it sleeps") should go too,
since that framing is what turned out to be conditional:

```
  <p class="hint">
    Each running browser uses roughly <strong>0.5–1 GB</strong>. You're billed on
    <strong>actual usage</strong> (about $10/GB per month). If you turned on
    <strong>Serverless</strong> in Railway, the server sleeps when idle and you pay
    for the minutes a sweep runs — a 3-minute sweep with 4 browsers costs pennies.
    <strong>If you didn't, you're paying for every hour of the month</strong>
    (~$8–9) whether you scrape or not. Raise this for faster multi-page sweeps;
    lower it if you want a tighter cap.
  </p>
```

*(Plan §4 Budgets specifies this copy and carries the same unconditional claim; it needs the
same correction at the source, or the next implementer will faithfully re-add it.)*

### Decided: document the toggle; do not detect the leak *(Nick, 2026-07-17)*

The app could plausibly notice this itself — a wake is a fresh process, so our own uptime is a
sleep-cycle marker, and "alive for hours with no job in flight" would mean the service never
slept. It was designed and deliberately **not built**.

**Why not:** the toggle lives in the dashboard tab the user is already in to copy `APP_SECRET`,
so the README step costs them one click where they are already standing. A detector is new
logic on the page holding the licence, proxy and Notion credentials — it needs its own review —
and it would be a second answer to a question the docs already answer.

**The accepted cost, stated plainly so nobody rediscovers it as a surprise:** if a user skips
the step, **nothing warns them. The bill is the only feedback, and it arrives a month late.**
Revisit only if that turns out to happen to a real person.

**Not proof.** A marketplace listing is not a random sample of what the editor permits, and this
measures what authoring paths *emit*, not what they *allow*. A dashboard check settles it — but
it now starts from a measured expectation rather than an absent doc line.

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
