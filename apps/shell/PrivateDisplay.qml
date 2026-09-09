import QtQuick
import QtQuick.Window
import QtWayland.Compositor
import QtWayland.Compositor.XdgShell
import AIOS.Display 1.0

Item {
    id: display
    required property var control
    required property var bridge
    property var compositor: null
    property string lease: ""
    clip: true
    enabled: !control.shield && !control.secureInput
    function clear() {
        lease = ""
        if (compositor) { compositor.clearInput(); compositor.destroy(); compositor = null }
        for (var index = children.length - 1; index >= 0; --index) children[index].destroy()
    }
    function prepare(app) {
        var acquired = bridge.acquire()
        if (acquired.descriptor === undefined) { control.displayFailed(); return }
        if (lease !== acquired.lease) {
            clear()
            lease = acquired.lease
            compositor = compositorComponent.createObject(display, {descriptor: acquired.descriptor})
            if (!compositor || !compositor.created) {
                if (!compositor) bridge.closeDescriptor(acquired.descriptor)
                clear(); control.displayFailed(); return
            }
        } else bridge.closeDescriptor(acquired.descriptor)
        control.displayReady(lease, app)
    }
    Connections {
        target: control
        function onDisplayRequested(app) { display.prepare(app) }
        function onPrivacyLost() { display.clear() }
        function onChanged() { if (!display.enabled && display.compositor) display.compositor.clearInput() }
    }
    Connections {
        target: display.Window.window
        function onActiveFocusItemChanged() {
            var item = display.Window.window.activeFocusItem
            while (item && item !== display) item = item.parent
            if (!item && display.compositor) display.compositor.clearInput()
        }
    }
    Component {
        id: compositorComponent
        PrivateCompositor {
            id: privateServer
            Component.onCompleted: initialize()
            property XdgShell shell: XdgShell {
                onToplevelCreated: (toplevel, surface) => {
                    surfaceComponent.createObject(display, {shellSurface: surface})
                }
            }
            property WaylandOutput output: WaylandOutput {
                compositor: privateServer; window: display.Window.window; sizeFollowsWindow: true
            }
        }
    }
    Component {
        id: surfaceComponent
        ShellSurfaceItem {
            autoCreatePopupItems: true
            focus: false
            transformOrigin: Item.TopLeft
            scale: Math.min(1, display.width / Math.max(1, width), display.height / Math.max(1, height))
            onSurfaceDestroyed: destroy()
        }
    }
}
