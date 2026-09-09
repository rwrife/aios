# Restore Minimized Chat Design

## Goal

Make the desktop chat blob resume minimized chats before starting new ones, without
changing multi-window independence or accessibility.

## Architecture and Components

- `apps/shell/ChatOrb.qml` keeps its accessible name and description but removes
  the visual hover/focus tooltip.
- `apps/shell/Main.qml` owns an explicit stack of chat windows ordered by when
  they most recently entered `Window.Minimized`. It remains responsible for
  creating sessions and chat windows.
- `apps/shell/ChatWindow.qml` reports lifecycle changes to `Main.qml`: entering
  `Window.Minimized` adds or moves the window to the top of the stack, and
  closing removes it.

The tracking state belongs in `Main.qml`, not the orb or backend, because it is
desktop-window state and must not affect conversation persistence or session
creation APIs.

## Data Flow

When any chat window enters `Window.Minimized`, including through operating-system
window controls, it notifies `Main.qml`. `Main.qml` removes any prior occurrence
of that window and appends it, making the stack unique and ordered from oldest to
newest minimization.

On each blob activation, `Main.qml` removes invalid entries from the newest end
of the stack. If a valid minimized chat remains, it removes that entry, restores
the window, raises it, and requests activation. Subsequent activations repeat the
process for the next newest minimized chat.

If no minimized chat remains, `Main.qml` creates a new backend session and a new
independent chat window using the existing creation path. Visible chat windows
do not block this path.

## Lifecycle and Error Handling

A window is tracked only while it is a valid minimized chat. Repeated minimized
notifications reorder rather than duplicate it. Restoring consumes its tracking
entry; minimizing it again adds it back as newest. Closing removes all references
to the window so later blob activations cannot target a destroyed object.

If a tracked object has become invalid or is no longer minimized, activation
discards it and continues to older entries. Failure to create a new window keeps
the existing behavior: no invalid window operation is attempted.

## Documentation

Update documentation that says every launcher activation creates a new chat.
The replacement wording must state that the launcher first restores the most
recently minimized chat, while still creating independent sessions whenever no
minimized chat remains. Historical QA evidence should remain historical unless
it describes current behavior.

## Testing

QML regression tests should cover:

- the orb exposes accessibility metadata without a visual tooltip;
- entering `Window.Minimized` through window visibility changes updates tracking;
- multiple minimized chats restore newest first, then older chats;
- closing a minimized chat removes it from tracking;
- restoring tracked chats does not create backend sessions;
- activation creates exactly one independent session/window when no minimized
  chat remains, even if other chats are visible.

Prefer behavioral QML tests that instantiate the relevant components and observe
window/session effects. Static source checks are insufficient for lifecycle and
ordering behavior.

## Scope

This change does not alter conversation storage, backend session semantics,
custom window controls beyond their resulting visibility state, or the behavior
of settings and other desktop launchers.
