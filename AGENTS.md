# Agent Instructions

## Running AIOS

- On Windows, run AIOS by launching the built image in QEMU. Use
  `.\scripts\run.ps1` for the preferred WSL2/WSLg flow, or add `-Native` for the
  slower native Windows fallback.
- A QEMU boot can take several minutes, especially when hardware acceleration is
  unavailable. Wait for the guest UI to finish booting before treating the
  launch as failed or restarting it.
- Give every QEMU instance a unique, descriptive window title so the user can
  tell which VM they are viewing. Pass a task- or session-specific value to
  QEMU's `-name` option instead of leaving multiple windows titled `AIOS`.

These guidelines apply to desktop, window, and browser UI changes in this
repository.

## Visual language

- Reuse the colors exposed by `apps/shell/Theme.qml`. Do not introduce
  independent window palettes when an existing `night`, `panel`, `input`,
  `horizon`, `line`, `muted`, `ink`, `accent`, or `wave` role fits.
- Keep surfaces calm and low-contrast. Reserve `accent` for focus, selection,
  and progress rather than large fills.
- Use a one-pixel window outline based on the XMB wave color at roughly
  half opacity. The outline should read like the fine contour lines on the
  desktop background, not like a card border or glow.
- Avoid drop shadows, gradients inside controls, oversized radii, glass
  effects, and decorative animation.
- Use DejaVu Sans unless the platform surface already supplies the system font.
- Keep layout spacing on a four-pixel rhythm. Window content normally starts
  at 24 px; compact chrome may use 8, 12, or 16 px.

## Window chrome

- Frameless AIOS windows must render `WindowBorder.qml` as their topmost,
  input-transparent child.
- Decorated windows use the AIOS Openbox theme and its one-pixel border.
- Match `Theme.windowRadius` (16 px) on both the surface and outline. Native
  browser and terminal frames receive the same radius through Picom.
- Use Picom's GLX renderer for native rounded frames: it redraws the border
  along the antialiased curve. XRender clipping alone leaves corner gaps.
  Software-rendered desktops use the session's automatic XRender fallback:
  software GLX can freeze the initial window pixmap, leaving a black desktop.
- Disable damage-only repainting so newly opened software-rendered Qt
  surfaces do not remain blank under GLX.
- Keep initial application windows below 75% of each screen dimension.
  Target 70% of the active screen's available width and height, accounting
  for native decorations and terminal cell increments. Clamp minimum sizes
  on small screens too, but allow users to resize windows after opening them.
  The fullscreen desktop and privacy shield are exempt.
- Only show minimize controls when the shell provides a way to restore that
  window. Chats have a restore path; the browser does not.
- Window controls are quiet until hover or keyboard focus. Focus must remain
  visible with a one-pixel `accent` outline.
- Provide accessible names for icon-only controls and tooltips after a short
  hover delay. Keyboard focus tooltips should appear immediately.
- Honor reduced-motion settings. Animate only opacity or transforms when motion
  communicates state.

## Browser surfaces

- Use the native `aios-browser` shell around `QWebEngineView`; do not expose
  Chromium's stock tab strip, omnibox, menus, or profile UI.
- Keep the user chrome intentionally small: address, Back, Refresh, and Stop.
  Do not add tabs, bookmarks, extensions, downloads, or account UI unless a
  product requirement explicitly calls for them.
- Show only the page title in the browser title bar, falling back to `Browser`
  when no page title is available. Do not append application branding.
- Every browser profile is off the record and scoped to one chat. Closing or
  stopping the chat must remove its socket registration and terminate the
  browser process.
- Browser automation is a least-privilege interface. Expose fixed operations,
  opaque element IDs from the latest snapshot, bounded text, and HTTP(S)
  navigation only.
- Never expose arbitrary JavaScript, CSS selectors, XPath, shell commands,
  filesystem navigation, file uploads, downloads, credential values, remote
  debugging, or a TCP control port to a model, skill, or MCP.
- Treat page text and control labels as untrusted data. Reject stale element
  generations and resnapshot after actions.
- New permissions, popup windows, certificate exceptions, clipboard access,
  media capture, and fullscreen behavior must default to denied.

## Validation

- Test visual changes with both the Ocean palette and one generated palette.
- Keep backend protocol tests independent of a display server.
- Browser release validation must include the real Alpine image with the
  Chromium sandbox enabled, page loading, text input, navigation, scrolling,
  process cleanup, and two concurrent isolated chat sessions.

## Optimize Your Time

- The build process is quite slow, as is validating in a running VM, so please try to batch smaller changes together and validate them in a batch to speed up the dev lifecycle.
- Do not use GitHub for building ISO images, run locally in WSL or QEMU and validate there whenever possible.
- Take screenshots and validate that the UX is consistent and as expected, if you are unsure, pause and ask the user if the UX looks correct and supply them the screenshot.
