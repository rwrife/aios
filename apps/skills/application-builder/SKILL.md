---
name: application-builder
description: Builds, caches, and launches small offline applications for calculators, timers, converters, trackers, dashboards, games, forms, and similar local utilities.
allowed-tools: application
metadata:
  aios-triggers: build an app, build an application, make an app, make an application, create an app, create an application, need an app, need an application, want an app, want an application, build a calculator, make a calculator, create a calculator, need a calculator, want a calculator, build a timer, make a timer, create a timer, need a timer, want a timer, build a converter, make a converter, create a converter, need a converter, want a converter, build a tracker, make a tracker, create a tracker, need a tracker, want a tracker, build a dashboard, make a dashboard, create a dashboard, need a dashboard, want a dashboard, build a game, make a game, create a game, need a game, want a game
  aios-model: remote-preferred
compatibility: AIOS offline browser applications
---

Instructions:
- Search first for an existing cached app that already solves the request; reuse it when possible instead of rebuilding from scratch.
- Build exactly one self-contained `index.html` file with no remote resources, no external packages, no CDN dependencies, and no additional asset files unless absolutely required for the local utility.
- Keep the app keyboard accessible, responsive, and usable offline in a browser.
- Write the app, publish it into the cache, and launch it.
- Confirm success only after the app reports `launched: true`.
- If you cannot finish, report the exact blocker clearly, including the missing capability, invalid input, cache miss, or launch failure.
