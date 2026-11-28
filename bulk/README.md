# Bulk Clock

Phone-first meal timer for a small-meals-often bulk: 5 to 8 meals of 400 to 700 kcal,
spaced evenly between wake-up and bedtime, with an alert at every meal.

- Big countdown to the next meal, full-screen "EAT NOW" alarm when it's due.
- One-tap logging from a list of foods (most eaten first), plus custom entries.
- Daily kcal bar with a marker for where you should be by now, last-7-days view.
- Morning weigh-ins charted against a straight-line plan to the goal date.
- Notifications at meal time and a nudge if a meal isn't logged.
- Push notifications at every meal and nudge, even with the app closed, sent by
  your own free Cloudflare Worker (`push-worker.js`). Calendar alarms (.ics) are
  an optional alternative and can be switched off.
- Gemini quick log (centre button): type, dictate or photograph what you ate, for
  today or any of the last 7 days.
- Forecast: projected weight on the deadline from your latest weigh-in and what
  you actually eat, kcal per day needed for the goal, and kcal left to eat.

Installable PWA, works offline, all data stays in the phone's browser storage.

## Push server

`push-worker.js` is a self-contained Cloudflare Worker (Web Push encryption and
VAPID in WebCrypto, no dependencies). Free plan:

1. Create a Worker, paste the file in, Deploy.
2. Create a KV namespace and bind it as `BULK`.
3. Add a Cron trigger `* * * * *`.
4. Paste the Worker URL into Setup → Push notifications → Connect.

The app generates its own VAPID keys and sends the Worker its meal times, timezone,
and which meals are logged, so meals you've eaten don't get an alert.

## Run locally

`python3 -m http.server` in this folder, then open http://localhost:8000.

## Publish

`.github/workflows/carousel.yml` deploys it to GitHub Pages at `/bulk/` on every push
to `master` that touches this folder.
