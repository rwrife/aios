import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "FirstRunSetup"
    when: windowShown
    width: 900; height: 750
    Theme { id: palette }
    QtObject {
        id: profileControl
        property bool greetingOnly: true
        property bool personalAvailable: true
        property bool busy: false
        property string error: ""
        property var profiles: []
        signal privacyLost()
        signal enrollmentCompleted(string recovery)
        signal unlocked()
        signal photoCaptured(string preview, string rgb)
        function setSecureInput(active) {}
        function enrollProfile(name, pin, consent, photo) {}
        function enroll(name, pin, consent) {}
        function unlock(name, pin) {}
        function recover(name, secret, pin) {}
        function takeProfilePhoto() {}
    }
    QtObject {
        id: backend
        property var config: ({})
        property bool busy: false
        property bool configuring: false
        property string status: ""
        property var subscription: ({})
        property string loginUrl: ""
        property string loginCode: ""
        property int dismissed: 0
        property int stopped: 0
        property int downloads: 0
        property int logins: 0
        signal changed()
        function dismissSetup() { dismissed++ }
        function stop() { stopped++; busy = false; changed() }
        function setupLocal() { downloads++; busy = true; changed() }
        function subscriptionAction(operation, device) { logins++; busy = true; changed() }
    }
    Component { id: wizardComponent; SetupWizard {} }
    property var wizard
    function init() {
        backend.busy = false; backend.dismissed = 0; backend.stopped = 0
        backend.downloads = 0; backend.logins = 0
        wizard = wizardComponent.createObject(test, {backend: backend, theme: palette, profileControl: profileControl})
        verify(wizard !== null)
        wizard.show(); wait(50)
    }
    function cleanup() { wizard.close(); wizard.destroy() }
    function test_skip_every_step_without_side_effects() {
        mouseClick(findChild(wizard, "setupNext"))
        for (var i = 1; i < 5; ++i) {
            compare(wizard.step, i)
            mouseClick(findChild(wizard, "setupSkip"))
        }
        compare(wizard.step, 5)
        mouseClick(findChild(wizard, "setupNext"))
        compare(wizard.visible, false)
        compare(backend.dismissed, 1)
        compare(backend.downloads, 0)
        compare(backend.logins, 0)
    }
    function test_close_cancels_download_and_reopen_resets_step() {
        wizard.moveTo(4)
        mouseClick(findChild(wizard, "setupModel"))
        compare(backend.downloads, 1)
        verify(backend.busy)
        mouseClick(findChild(wizard, "closeSetup"))
        compare(backend.stopped, 1)
        compare(backend.dismissed, 1)
        wizard.show()
        compare(wizard.step, 0)
    }
    function test_skip_cancels_sign_in() {
        wizard.moveTo(2)
        mouseClick(findChild(wizard, "setupSignIn"))
        compare(backend.logins, 1)
        mouseClick(findChild(wizard, "setupSkip"))
        compare(wizard.step, 3)
        compare(backend.stopped, 1)
    }
    function test_local_account_flow_is_available() {
        wizard.moveTo(2)
        var create = findChild(wizard, "setupLocalAccount")
        verify(create.enabled)
        mouseClick(create)
        tryCompare(findChild(wizard, "profileSubmit"), "visible", true)
    }
    function test_close_does_not_cancel_unrelated_work() {
        backend.busy = true
        wizard.close()
        compare(backend.stopped, 0)
    }
    function test_escape_closes_setup() {
        wizard.requestActivate(); wait(50)
        keyClick(Qt.Key_Escape)
        tryCompare(wizard, "visible", false)
        compare(backend.dismissed, 1)
    }
}
