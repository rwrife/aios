# Desktop settings

Appearance offers eight saved theme colors: Ocean (default), Lagoon, Sage,
Amber, Copper, Rose, Dusk, and Slate. A selection immediately updates the AIOS
background, wave crests and fades, launcher, and open chat/settings panels.
Text stays light for contrast. The existing reduced-motion setting remains
independent. Installed sessions retain the selection after login; live-session
preferences remain temporary like other live settings.

Open the sliders icon beside Terminal and Power. Settings is a single reusable
window; opening it again raises the existing panel. Chat continues in its own
windows. The section list and matching StackLayout pages in SettingsWindow.qml
provide the extension point for future settings.

- **AI models:** shared with chat's ellipsis menu. Choose a local GGUF or bundled
  starter model, or configure a compatible remote endpoint, model and API key. The
  Voice tab configures remote speech or local Whisper/eSpeak. Save applies the
  selected tab; closing without saving discards uncommitted field edits on reopen.
- **Sound:** opens PulseAudio Volume Control for input/output devices, volume,
  mute and per-application audio. Set fallback devices for subsequent voice use.
- **Camera:** lists connected webcams and starts a local preview only on request.
  Leaving the section or closing Settings stops capture. No pictures are saved
  or sent to a model. Video chat and camera attachments remain future work.
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

Configuration survives reboot on an installed system. Live sessions remain
ephemeral. Audio/camera/Wi-Fi capabilities depend on the connected hardware and
drivers; unavailable cameras are shown explicitly. No camera is activated just
by opening Settings.

System tools are launched from a fixed allowlist; no arbitrary command comes
from QML settings input. AI credentials retain the existing private config-file
storage and separate chat/speech endpoint behavior.
