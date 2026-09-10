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
        signal cameraReleaseRequested()
        function setCameraPreviewActive(active) {}
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
        property var localModels: ({models: [{id: "qwen3-0.6b", name: "Qwen3 0.6B Q4_K_M", bundled: true, installed: true, available: true, tool_use: true, bytes: 484220320, ram_gib: 2, note: "Starter", license: "Apache-2.0"}], ram_bytes: 4294967296, free_disk_bytes: 9999999999})
        function refreshLocalModels() {}
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
        property string selectedId: ""
        signal changed()
        function dismissSetup() { dismissed++ }
        function stop() { stopped++; busy = false; changed() }
        function setupLocal(id) { selectedId = id; downloads++; busy = true; changed() }
        function subscriptionAction(operation, device) { logins++; busy = true; changed() }
    }
    Component { id: wizardComponent; SetupWizard {} }
    property var wizard
    function init() {
        backend.busy = false; backend.dismissed = 0; backend.stopped = 0
        backend.downloads = 0; backend.logins = 0; backend.selectedId = ""
        wizard = wizardComponent.createObject(test, {backend: backend, theme: palette, profileControl: profileControl})
        verify(wizard !== null)
        wizard.show(); wait(50)
        compare(findChild(wizard, "setupWindowSurface").radius, palette.windowRadius)
        compare(findChild(wizard, "setupWindowTitle").font.pixelSize, 22)
        compare(findChild(wizard, "closeSetup").implicitWidth, 36)
        compare(findChild(wizard, "closeSetup").implicitHeight, 36)
        compare(findChild(wizard, "closeSetup").background.radius, 8)
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
        compare(backend.selectedId, "qwen3-0.6b")
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
    function test_camera_preview_uses_bounded_format_without_runtime_enums() {
        var selected = wizard.previewFormat({videoFormats: [
            {resolution: {width: 640, height: 360}, mode: "raw"},
            {resolution: {width: 1920, height: 1080}, mode: "large"},
            {resolution: {width: 640, height: 360}, mode: "compressed"}
        ]})
        compare(selected.resolution.width, 640)
        compare(selected.resolution.height, 360)
        compare(selected.mode, "compressed")
        wizard.moveTo(3)
        wait(50)
        var preview = findChild(wizard, "cameraPreview")
        verify(Math.abs(preview.width / preview.height - 16 / 9) < 0.02)
        verify(Math.abs(preview.width / preview.parent.width - 0.75) < 0.02)
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
