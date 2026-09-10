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
        property bool secureInput: false
        property bool personalAvailable: true
        property bool greetingOnly: false
        property var profile: ({})
        property var profiles: [{id: "test-id", name: "Test profile"}]
        function listProfiles() {}
        function setSecureInput(active) { secureInput = active }
        signal privacyLost()
        signal documentLoaded(string content)
        signal documentSaved()
        signal enrollmentCompleted(string recovery)
        signal photoCaptured(string preview, string rgb)
        signal unlocked()
        signal accountDeleted(string id)
        property int deletions: 0
        function deleteAccount(id, pin) {
            if (id === "test-id" && pin === "1234") { deletions++; accountDeleted(id) }
        }
        property int enrollments: 0
        property int signins: 0
        property int photosTaken: 0
        function takeProfilePhoto() { photosTaken++ }
        function unlock(name, pin) { if (name === "Test profile" && pin === "1234") { signins++; profile = {name: name}; unlocked() } }
        property int recoveries: 0
        function recover(name, secret, pin) {
            if (name === "Test profile" && secret === "test-recovery-secret" && pin === "246802") recoveries++
        }
        function enroll(name, pin, consent) {
            if (name.toLowerCase() === "test profile" && (pin === "123456" || pin === "1234") && consent) {
                enrollments++
                if (greetingOnly) { profile = {name: name}; unlocked() }
            }
        }
        function cancelChallenge() { challenge = ({}) }
        function suspend() { shield = false }
    }
    Component { id: shieldComponent; PrivacyShield {} }
    Component { id: pinComponent; SecurePinPrompt {} }
    Component { id: statusComponent; IdentityStatus {} }
    Component { id: enrollmentComponent; EnrollmentFlow {} }
    Component { id: bubbleComponent; UserBubble {} }
    Component { id: accountsComponent; AccountSettings {} }
    Component { id: themeComponent; Theme {} }

    function test_new_account_hover_and_focus_colors_data() {
        return [{tag: "Ocean", selected: "blue"}, {tag: "Sage", selected: "sage"}]
    }

    function test_new_account_hover_and_focus_colors(data) {
        var theme = createTemporaryObject(themeComponent, test, {selected: data.selected})
        var bubble = createTemporaryObject(bubbleComponent, test, {
            control: control, x: 200, y: 80, ink: theme.ink, surface: theme.input
        })
        bubble.openPicker()
        var menu = findChild(bubble, "profilePicker")
        tryCompare(menu, "opened", true)
        waitForRendering(menu.contentItem)
        var create = findChild(menu.contentItem, "createUser")
        var choose = findChild(menu.contentItem, "chooseProfile")
        mouseMove(create, create.width / 2, create.height / 2)
        tryCompare(create, "hovered", true)
        compare(create.background.color, menu.palette.highlight)
        compare(create.contentItem.color, theme.ink)
        verify(create.background.color !== create.contentItem.color)
        mousePress(create)
        verify(create.down)
        compare(create.background.color, menu.palette.highlight)
        mouseMove(choose, choose.width / 2, choose.height / 2)
        mouseRelease(choose)
        create.forceActiveFocus(Qt.TabFocusReason)
        tryCompare(create, "activeFocus", true)
        compare(create.background.color, menu.palette.highlight)
        compare(create.background.border.width, 1)
        create.enabled = false
        compare(create.contentItem.opacity, 0.5)
        menu.close()
    }

    function test_settings_delete_requires_pin_and_clears_prompt() {
        control.greetingOnly = true
        var panel = accountsComponent.createObject(test.parent, {control: control, width: 420, height: 440})
        waitForRendering(panel)
        var button = findChild(panel, "deleteAccount")
        verify(button !== null)
        verify(button.visible)
        mouseClick(button)
        var prompt = findChild(panel, "deleteAccountDialog")
        tryCompare(prompt, "opened", true); waitForRendering(prompt.contentItem)
        var pin = findChild(prompt, "deleteAccountPin")
        var confirm = findChild(prompt, "confirmDeleteAccount")
        compare(confirm.enabled, false)
        compare(pin.echoMode, TextInput.Password)
        var before = control.deletions
        pin.forceActiveFocus(); typeKeys("1234"); keyClick(Qt.Key_Return)
        compare(control.deletions, before + 1)
        tryCompare(prompt, "opened", false)
        compare(pin.text, "")
        panel.destroy()
    }
    function typeKeys(text) {
        for (var i = 0; i < text.length; ++i) {
            var letter = text[i]
            keyClick(letter.toUpperCase().charCodeAt(0), /[A-Z]/.test(letter) ? Qt.ShiftModifier : Qt.NoModifier)
        }
    }
    function test_keyboard_only_create_four_digit_pin_and_select_account() {
        control.greetingOnly = true
        var bubble = bubbleComponent.createObject(test, {control: control})
        bubble.createAccount()
        var form = findChild(bubble, "bubbleEnrollment")
        tryCompare(form, "opened", true); waitForRendering(form.contentItem)
        var name = findChild(bubble, "profileName")
        var pin = findChild(bubble, "enrollmentPin")
        tryCompare(name, "activeFocus", true)
        typeKeys("test profile")
        compare(name.text, "test profile")
        keyClick(Qt.Key_Tab)
        tryCompare(pin, "activeFocus", true)
        typeKeys("123")
        keyClick(Qt.Key_Return)
        verify(form.validationError.indexOf("4 digits") >= 0)
        compare(form.opened, true)
        typeKeys("4")
        compare(pin.text, "1234")
        keyClick(Qt.Key_Return)
        tryCompare(form, "opened", false)
        compare(bubble.name, "test profile")
        compare(control.photosTaken, 0)
        bubble.openPicker()
        var menu = findChild(bubble, "profilePicker")
        tryCompare(menu, "opened", true); waitForRendering(menu.contentItem)
        keyClick(Qt.Key_Down); keyClick(Qt.Key_Return)
        tryCompare(form, "opened", true)
        tryCompare(pin, "activeFocus", true)
        compare(name.text, "Test profile")
        typeKeys("1234")
        var before = control.signins
        keyClick(Qt.Key_Return)
        compare(control.signins, before + 1)
        compare(control.photosTaken, 0)
        bubble.destroy(); control.greetingOnly = false; control.profile = {}
    }
    function test_desktop_bubble_opens_name_pin_and_greets_after_create() {
        control.greetingOnly = true
        var bubble = bubbleComponent.createObject(test, {control: control})
        bubble.openPicker()
        var menu = findChild(bubble, "profilePicker")
        tryCompare(menu, "opened", true); waitForRendering(menu.contentItem)
        var create = findChild(menu.contentItem, "createUser")
        verify(create !== null)
        mouseClick(create)
        var form = findChild(bubble, "bubbleEnrollment")
        tryCompare(form, "opened", true)
        waitForRendering(form.contentItem)
        findChild(bubble, "profileName").text = "Test profile"
        findChild(bubble, "enrollmentPin").text = "123456"
        var submit = findChild(bubble, "profileSubmit")
        tryCompare(submit, "enabled", true)
        mouseClick(submit)
        tryCompare(form, "opened", false)
        compare(bubble.greeting, "Hello, Test profile. How may I help you?")
        bubble.destroy()
        control.greetingOnly = false; control.profile = {}
    }
    function test_profile_bubble_greeting_and_fallback() {
        var bubble = bubbleComponent.createObject(test, {control: control})
        control.profile = {name: "Alice", detected: true, photo: ""}
        compare(bubble.greeting, "Hello, Alice. How may I help you?")
        control.profile = {}
        verify(bubble.greeting.indexOf("Set up an account") >= 0)
        bubble.openPicker()
        var picker = findChild(bubble, "profilePicker")
        tryCompare(picker, "opened", true)
        waitForRendering(picker.contentItem)
        var choose = findChild(picker.contentItem, "chooseProfile")
        verify(choose !== null)
        mouseClick(choose)
        var enrollment = findChild(bubble, "bubbleEnrollment")
        compare(enrollment.creating, false)
        var name = findChild(bubble, "profileName")
        tryCompare(name, "text", "Test profile")
        var pin = findChild(bubble, "enrollmentPin")
        compare(pin.echoMode, TextInput.Password)
        compare(pin.text, "")
        control.privacyLost()
        bubble.destroy()
    }
    function test_recovery_submits_and_clears_both_secrets() {
        var surface = enrollmentComponent.createObject(test, {control: control, recovering: true})
        surface.open()
        var name = findChild(surface, "profileName")
        var pin = findChild(surface, "enrollmentPin")
        var secret = findChild(surface, "recoveryInput")
        var submit = findChild(surface, "profileSubmit")
        name.text = "Test profile"
        pin.text = "246802"
        secret.text = "test-recovery-secret"
        compare(secret.echoMode, TextInput.Password)
        var before = control.recoveries
        tryCompare(submit, "enabled", true)
        mouseClick(submit)
        compare(control.recoveries, before + 1)
        compare(pin.text, "")
        compare(secret.text, "")
        secret.text = "private"
        control.privacyLost()
        tryCompare(secret, "text", "")
        surface.destroy()
    }
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
    function cleanup() { control.shield = false; control.challenge = ({}); control.greetingOnly = false; control.profile = {} }
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
