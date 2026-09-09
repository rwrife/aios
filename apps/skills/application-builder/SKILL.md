---
name: application-builder
description: Builds, caches, and launches small offline applications for calculators, timers, converters, trackers, dashboards, games, forms, and similar local utilities.
allowed-tools: application
metadata:
  aios-triggers: build an app, build a calculator, make an app, make a calculator, need an app, need a calculator, calculator, timer, converter, tracker, dashboard, game
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
