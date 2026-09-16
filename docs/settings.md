# Desktop settings

On first launch, an optional setup wizard walks through internet connection,
ChatGPT account sign-in, camera availability, and local chat/speech models.
Every step can be skipped. Close setup (or press Escape) at any time; completed
changes remain, while unfinished wizard downloads/sign-in are canceled and the
camera stops. Dismissing or finishing suppresses automatic setup on subsequent
launches for that desktop user. **Run setup wizard** in Settings reopens it.
Live sessions forget this preference on reboot, like other settings. The wizard
does not run in the experimental session-broker desktop.

The account step can create a local name/PIN profile with an optional camera
photo, and can separately connect a ChatGPT service account.
Camera previews and account recognition are separate opt-ins. Recognition is
disabled by default, requires a stable local `/dev/v4l/by-id/*-video-indexN`
device plus an administrator-provided, checksum-verified and Brio-calibrated
YuNet/SFace manifest, and never replaces the account PIN. Face samples are
captured only during explicit enrollment or short background bursts; raw frames
are discarded after local inference. OpenCV is included for service-owned previews
and photos in the default image; encrypted-template support remains in the
optional identity image. The [facial-data lifecycle](facial-data-lifecycle.md)
describes consent, key ownership, crash recovery and deletion boundaries.
The Qwen3 0.6B starter reuses the bundled copy or downloads 462 MiB when missing;
the [local model catalog](local-models.md) also offers three stronger Qwen3
sizes with RAM and free-disk checks. Optional local speech downloads 75 MiB. Nothing downloads merely by opening or
advancing through the wizard. Network setup opens the existing connection tool.

Appearance offers eight saved theme colors: Ocean (default), Lagoon, Sage,
Amber, Copper, Rose, Dusk, and Slate. A selection immediately updates the AIOS
background, wave crests and fades, launcher, and open chat/settings panels.
Text stays light for contrast. Background motion defaults on when the system has
at least two CPU cores and 4 GiB of RAM, and defaults off on smaller systems.
The saved reduced-motion choice overrides this detection. Installed sessions
retain the selection after login; live-session preferences remain temporary like
other live settings.

Open the sliders icon beside Terminal and Power. Settings is a single reusable
window; opening it again raises the existing panel. Chat continues in its own
windows. The section list and matching StackLayout pages in SettingsWindow.qml
provide the extension point for future settings.

The speaker icon in the lower-right controls opens a compact vertical volume
slider for the default output device. Its speaker button mutes or unmutes audio;
the full Sound settings page remains available for choosing devices and adjusting
individual applications.

- **AI models:** shared with chat's ellipsis menu. Download and use a catalog
  model, return to the bundled starter, import a local GGUF, or configure a
  compatible remote endpoint, model and API key. Catalog installs select the
  model immediately. The
  Agent tasks controls choose Current chat model, ChatGPT subscription, or a
  separate remote OpenAI-compatible endpoint/model/key for skills marked
  `remote-preferred`. Current is the default; ordinary chat remains on the primary
  provider, and AIOS never silently falls back to a paid provider. Changing the
  Agent tasks remote endpoint clears its saved key unless a replacement is entered.
  The Voice tab configures remote speech or local Whisper/eSpeak. Save applies the
  selected tab; closing without saving discards uncommitted field edits on reopen.
- **Sound:** opens PulseAudio Volume Control for input/output devices, volume,
  mute and per-application audio. Set fallback devices for subsequent voice use.
- **Camera:** lists connected webcams and starts a local preview only on request.
  Leaving the section or closing Settings stops preview capture. Optional account
  suggestions can be enabled with a stable local camera path after an account
  completes separate PIN-verified face enrollment. Suggestions expire after five
  seconds, pause for profile-photo capture and secure input, and always open the
  normal PIN prompt. Disabling recognition stops camera capture while preserving
  enrolled face templates. **Purge facial recognition data** withdraws consent
  and removes templates from the current local store without deleting accounts,
  profile photos, or PINs. External backups and snapshots are outside that deletion
  boundary. No raw recognition pictures are saved or sent to chat providers. Video chat and camera
  attachments remain future work.
- **Network & Wi-Fi:** opens NetworkManager's nmtui. Activate a connection joins
  Wi-Fi or selects Ethernet; Edit a connection configures addresses and DNS.
  NetworkManager replaces dhcpcd as the default interface manager. Its internal
  DHCP client manages Ethernet, and wpa_supplicant provides Wi-Fi. The ordinary
  aios user belongs to plugdev; Alpine's active-session policy allows editing
  connections without giving the desktop root privileges.
- **Display:** opens ARandR for resolution, orientation and monitor layout.
  Apply changes for this session. Save a desired layout as
  ~/.screenlayout/default.sh to restore it at login. An unsuccessful saved
  layout does not prevent the desktop from starting.
- **Appearance:** chooses a theme color and enables or reduces background motion.
- **About:** shows the AIOS version, build number, source commit, operating system,
  kernel, architecture, CPU and total memory.
- **Date & Time:** reads and sets the actual guest system clock used by all
  applications and chats. Refresh shows UTC and local time with the current
  timezone. Enter a whole-second ISO date/time with `Z` or an explicit UTC offset,
  for example `2026-09-11T07:30:00-07:00`; valid dates and UTC equivalents must be
  in 2000-2099 with offsets within +/-14:00. This does not change the timezone.
  Manual setting is blocked while a time-sync daemon is running; an administrator
  must stop it first. AIOS attempts to save the UTC hardware clock and reports
  whether that succeeded. A runtime-only change can be lost on reboot; a VM may
  restore its host's time even after a successful save. The existing desktop OS
  user's exact doas permission authorizes this fixed operation, not chat sign-in.
  Clock errors stay visible and never imply a successful change.

Configuration survives reboot on an installed system. Live sessions remain
ephemeral. Audio/camera/Wi-Fi capabilities depend on the connected hardware and
drivers; unavailable cameras are shown explicitly. No camera is activated just
by opening Settings.

System tools are launched from a fixed allowlist; no arbitrary command comes
from QML settings input. AI credentials retain the existing private config-file
storage and separate primary chat, Agent tasks, and speech endpoint behavior.
CLI users can set the same route with `--agent-mode`, `--agent-url`,
`--agent-model`, and the non-echoing `--ask-agent-key`. See
[agentic tools](agentic-tools.md).
## Recognition release status

Recognition remains experimental and off by default. Optional identity images
bundle pinned model artifacts but no calibrated approval. Missing approval keeps
manual PIN access available. Disabling recognition preserves enrolled templates;
purge is separate. See [release evidence](qa/recognition-release.md).
