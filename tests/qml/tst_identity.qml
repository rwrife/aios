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
        property string authority: "recognized"
        property string error: ""
        property var sessions: []
        property var messages: []
        property bool olderMessages: false
        property bool busy: false
        signal privacyLost()
        signal documentLoaded(string content)
        signal documentSaved()
        signal enrollmentCompleted(string recovery)
        signal unlocked()
        property int enrollments: 0
        function enroll(name, pin, consent) {
            if (name === "Test profile" && pin === "123456" && consent) enrollments++
        }
        function cancelChallenge() { challenge = ({}) }
        function suspend() { shield = false }
    }
    Component { id: shieldComponent; PrivacyShield {} }
    Component { id: pinComponent; SecurePinPrompt {} }
    Component { id: statusComponent; IdentityStatus {} }
    Component { id: enrollmentComponent; EnrollmentFlow {} }
    function test_enrollment_submits_masked_pin_and_clears_it() {
        var surface = enrollmentComponent.createObject(test, {control: control, creating: true})
        surface.open()
        var name = findChild(surface, "profileName")
        var pin = findChild(surface, "enrollmentPin")
        var consent = findChild(surface, "profileConsent")
        var submit = findChild(surface, "profileSubmit")
        name.text = "Test profile"
        pin.text = "123456"
        consent.checked = true
        compare(pin.echoMode, TextInput.Password)
        var before = control.enrollments
        tryCompare(submit, "enabled", true)
        mouseClick(submit)
        compare(control.enrollments, before + 1)
        compare(pin.text, "")
        surface.close()
        surface.destroy()
    }
    function test_session_drafts_clear_on_privacy_loss() {
        var surface = statusComponent.createObject(test, {control: control})
        verify(surface !== null)
        var title = findChild(surface, "sessionTitle")
        var note = findChild(surface, "sessionNote")
        title.text = "Private résumé"
        note.text = "Private draft"
        control.privacyLost()
        compare(title.text, "")
        compare(note.text, "")
        surface.destroy()
    }
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
