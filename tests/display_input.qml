import QtQuick
import QtQuick.Controls
import "../apps/shell"

Window {
    id: window
    width: 800; height: 600; visible: true
    color: "#172633"
    PrivateDisplay { anchors.fill: parent; control: testControl; bridge: displayBridge }
    SecurePinOverlay { parent: window.contentItem; control: testControl }
}
