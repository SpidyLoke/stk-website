# Bulk Clock

Phone-first meal timer for a small-meals-often bulk: 5 to 8 meals of 400 to 700 kcal,
spaced evenly between wake-up and bedtime, with an alert at every meal.

- Big countdown to the next meal, full-screen "EAT NOW" alarm when it's due.
- One-tap logging from a list of foods (most eaten first), plus custom entries.
- Daily kcal bar with a marker for where you should be by now, last-7-days view.
- Morning weigh-ins charted against a straight-line plan to the goal date.
- Notifications at meal time and a nudge if a meal isn't logged.
- Exports daily calendar alarms (.ics) until the deadline, so the phone rings
  even with the app closed.

Installable PWA, works offline, all data stays in the phone's browser storage.

## Run locally

`python3 -m http.server` in this folder, then open http://localhost:8000.

## Publish

`.github/workflows/carousel.yml` deploys it to GitHub Pages at `/bulk/` on every push
to `master` that touches this folder.
