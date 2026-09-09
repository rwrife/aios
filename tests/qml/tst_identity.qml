import QtQuick
import QtQuick.Controls
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "IdentitySurfaces"
    when: windowShown
    width: 800; height: 650
    QtObject {
        id: control
        property bool enabled: true
        property bool shield: false
        property bool simulator: true
        property var challenge: ({})
        function cancelChallenge() { challenge = ({}) }
        function suspend() { shield = false }
    }
    Component { id: shieldComponent; PrivacyShield {} }
    Component { id: pinComponent; SecurePinPrompt {} }
    function cleanup() { control.shield = false; control.challenge = ({}) }
    function test_shield_tracks_loss_and_release() {
        var surface = shieldComponent.createObject(test, {control: control})
        verify(surface !== null)
        verify(!surface.visible)
        control.shield = true
        tryCompare(surface, "visible", true)
        control.suspend()
        tryCompare(surface, "visible", false)
        surface.destroy()
    }
    function test_pin_is_masked_and_clears_on_cancellation() {
        var surface = pinComponent.createObject(test, {control: control})
        verify(surface !== null)
        var input = findChild(surface, "securePinInput")
        verify(input !== null)
        compare(input.echoMode, TextInput.Password)
        control.challenge = ({id: "test", owner: "fictional", operation: "financial.read", resource: "account"})
        surface.show()
        input.text = "123456"
        control.cancelChallenge()
        tryCompare(surface, "visible", false)
        compare(input.text, "")
        surface.destroy()
    }
}
