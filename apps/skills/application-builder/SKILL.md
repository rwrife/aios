---
name: application-builder
description: Builds, caches, and launches small offline applications for calculators, timers, converters, trackers, dashboards, games, forms, and similar local utilities.
allowed-tools: application
metadata:
  aios-triggers: build an app, build an application, make an app, make an application, create an app, create an application, need an app, need an application, want an app, want an application, build a calculator, make a calculator, create a calculator, need a calculator, want a calculator, build a timer, make a timer, create a timer, need a timer, want a timer, build a converter, make a converter, create a converter, need a converter, want a converter, build a tracker, make a tracker, create a tracker, need a tracker, want a tracker, build a dashboard, make a dashboard, create a dashboard, need a dashboard, want a dashboard, build a game, make a game, create a game, need a game, want a game
  aios-model: remote-preferred
compatibility: AIOS native templates and offline browser applications
---

Instructions:
- Search first for an existing cached app that already solves the request. Always search before creating anything.
- Inspect the advertised `application` tool schema before choosing a runtime. Native support exists only when both `runtime` and `template` are offered; use only advertised values and never invent capabilities.
- When the request matches an advertised trusted native template, prefer native. The currently supported native template is `calculator`.
- Reuse an exact or strong cached native match. If the only suitable cached match is web but the trusted native `calculator` template is advertised for a calculator request, create the native app instead of permanently preferring the old web app.
- For a native calculator, call `create` with `runtime: "native"` and `template: "calculator"`, then `publish` with a concise summary and search keywords, then `launch`. Do not call `read` or `write`, and do not generate or invent HTML, C++, QML, assets, or build steps.
- If native fields are not advertised or no advertised native template supports the request, use the web fallback: create a web draft, write exactly one self-contained `index.html` with no remote resources, external packages, CDN dependencies, or extra asset files, publish it, and launch it. Keep it keyboard accessible, responsive, and usable offline.
- Confirm success only after `launch` returns `launched: true`. If it returns `launched: false`, report its safe reason and stop. Do not rebuild, re-publish, or retry in a loop.
- If you cannot finish, report the exact blocker clearly, including the missing capability, invalid input, cache miss, or launch failure.
- These native instructions apply only while this skill is selected through the normal skill activation policy; do not treat native support as permission to activate the skill more broadly.
