# Design: detecting that the server never went to sleep

**Status: DESIGN ONLY. Not built.** Step 5's review is in flight and this is new logic on the
page that holds the licence, proxy and Notion credentials, so it needs its own reviewer. This
is a Step 7 candidate, and only if Nick wants it — documenting the toggle may be enough.

## The problem it solves

A template cannot ship `sleepApplication` (measured: 0 of 2964 services across 1500 public
templates — see `railway-template.md`). So every user must switch Serverless on themselves.
If they don't, nothing breaks and nothing complains: the server just quietly bills ~$8–9/month
for holding a gigabyte it isn't using. **An invisible billing leak is the worst kind of bug for
a non-technical audience — there is no symptom to notice.**

We cannot read the toggle: that would need a Railway API token, and asking a user for one to
watch their own billing is a worse trade than the bug.

## The signal: our own uptime

**A wake is a fresh process.** That is already established — it is exactly why the job store
had to live on the volume rather than in memory. So process uptime *is* a sleep-cycle marker,
and we get it for free:

> If the process has been alive for **T_up** while **no job has been in flight** and the last
> job ended more than **T_quiet** ago, then the service **did not sleep when it should have**.

Sleep was measured at roughly **6–10 minutes** of no outbound traffic, and varies. With
`T_quiet = 20 min` and `T_up = 30 min` there is a wide margin over the upper bound, so a
false positive needs the sleep threshold to be triple its observed maximum.

## Why the confounds don't fire

Each of these is a way the process could stay alive innocently. All are closed:

- **Our heartbeat** is gated on `in_flight` (`services/heartbeat.py`), so an idle server emits
  nothing. Confirmed by the idle run sleeping at 10.1 min with the app deployed and healthy.
- **A polling browser tab** can't hold it: the UI has zero JS (decision #19), so an open tab
  makes no requests.
- **Railway's healthchecks are inbound**, and inbound does not reset the sleep clock — only
  outbound does. Measured directly: the service slept repeatedly while deployed and healthy,
  with healthchecks configured on `/healthz`.
- **A user's own polling** *would* reset it — their poll gets a response, and a response is
  outbound. But that is not a false positive: a server being polled every minute forever
  genuinely isn't sleeping, and they genuinely are paying for it.

## Why it fails in the safe direction

**It can only fire when the service demonstrably failed to sleep** — it observes our own
uptime, which is ground truth, not an inference about Railway's settings. The failure mode is
staying **silent** when it can't tell (e.g. the user restarted the service manually a minute
ago). It cannot produce a false "you're fine", because it never says that: absence of the
banner is not a claim.

## What it must say — and must not

Same discipline as "Test proxy" reporting an exit IP rather than "credentials OK": **report the
measurement, not the inference.** We measured uptime and job history. We did **not** measure the
toggle.

> **This server has been running for 18 hours without scraping anything.**
> It should switch itself off when idle, and it hasn't — so it is very likely billing you for
> doing nothing (roughly $8–9/month). In Railway, open this service → Settings → enable
> **Serverless**. [what this means]

Not *"Serverless is off"* — we don't know that. Something else could be pinning it, which the
user equally wants to know, and the banner is still correct and still worth acting on.

## Cost

~30 lines: a module-level start timestamp, the last-job-ended timestamp the job store already
has, and a banner in the settings template. No framework, no JS, no dependency (#19). It reads
state that already exists.

## Open questions for the reviewer

- **`T_up`/`T_quiet` are guesses bounded by one measured range (6–10 min).** They deserve their
  own measurement, and the sleep threshold has already embarrassed one confident number
  (a single 10.1-min reading was reported as *the* threshold and is actually variable).
- **Restart loops** produce short uptimes, so they suppress the banner rather than trigger it —
  correct, but it means a crash-looping service (which Railway emails about anyway) shows
  nothing here.
- **Does the banner belong on `/` only, or also in `/healthz`?** `/healthz` is unauthenticated;
  leaking "this deployment is idle and misconfigured" to anonymous callers is a small
  information disclosure for no gain. Recommend: settings page only.
