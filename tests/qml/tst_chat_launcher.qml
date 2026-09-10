import QtQuick
import QtQuick.Window
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "ChatLauncher"
    when: windowShown
    width: 1200
    height: 900

    QtObject {
        id: sessionControl
        property bool enabled: false
        property bool shield: false
        property bool simulator: true
        property bool busy: false
        property bool personalAvailable: true
        property bool greetingOnly: true
        property bool secureInput: false
        property var profile: ({})
        property var profiles: []
        property var messages: []
        property bool olderMessages: false
        signal privacyLost()
        signal documentLoaded(string content)
        signal documentSaved()
        function chatProfile() { return sessionControl }
        function dispose() {}
        function setSecureInput(active) { secureInput = active }
        property var challenge: ({})
        property string authority: "Anonymous"
        property var sessions: []
        property string error: ""
        function cancelChallenge() { challenge = ({}) }
        function suspend() { shield = false }
        function verify(pin) {}
        function simulate(identity) {}
        function activate(title) {}
        function search() {}
        function resume(sessionId) {}
        function launch(target) {}
        function protectedResource() {}
    }

    Component {
        id: sessionComponent
        QtObject {
            property var messages: []
            property bool busy: false
            property bool recording: false
            property bool speaking: false
            property var attachments: []
            property string status: ""
            property int closeCalls: 0
            signal changed()
            signal transcribed(string text)
            function closeSession() { closeCalls += 1 }
            function send(text) {}
            function copy(text) {}
            function readReply(text) {}
            function removeAttachment(index) {}
            function attach(file) {}
            function setVoiceActive(active) { recording = active }
            function cancelRecording() { recording = false }
            function stop() { busy = false }
        }
    }

    QtObject {
        id: backend
        property var config: ({
            theme_color: "blue",
            reduced_motion: true,
            mode: "local",
            model_path: "/starter.gguf",
            live: false,
            voice_mode: "local",
            voice_url: ""
        })
        property bool busy: false
        property bool configuring: false
        property string status: ""
        property var subscription: ({})
        property string loginUrl: ""
        property string loginCode: ""
        property int volume: 65
        property bool muted: false
        property bool volumeAvailable: true
        property var systemInfo: ({
            version: "0.1.0", build: "test", commit: "test",
            os: "AIOS Test", kernel: "Linux", architecture: "x86_64",
            cpu: "Test CPU", memory: "4.0 GiB"
        })
        property var createdSessions: []
        property int createSessionCalls: 0
        property int refreshVolumeCalls: 0
        property var setVolumeCalls: []
        property var setMutedCalls: []
        signal configured()
        signal loaded()
        signal changed()
        property bool needsSetup: false
        function setupPending() { return needsSetup }
        function dismissSetup() { needsSetup = false }
        function createSession() {
            var session = sessionComponent.createObject(test)
            createdSessions = createdSessions.concat([session])
            createSessionCalls += 1
            return session
        }
        function configure(values) {}
        function power(action) {}
        function terminal() {}
        function openSystemSettings(section) {}
        function stop() {}
        function subscriptionAction(action, device) {}
        function refreshVolume() { refreshVolumeCalls += 1 }
        function setVolume(value) { setVolumeCalls = setVolumeCalls.concat([value]) }
        function setMuted(value) { setMutedCalls = setMutedCalls.concat([value]) }
    }

    Theme { id: palette; selected: "blue" }
    Component { id: launcherComponent; ChatOrb {} }
    Component { id: mainComponent; Main {} }
    property var launcher
    property var desktop

    function init() {
        backend.needsSetup = false
        launcher = null
        desktop = null
        backend.createdSessions = []
        backend.createSessionCalls = 0
        backend.volume = 65
        backend.muted = false
        backend.volumeAvailable = true
        backend.refreshVolumeCalls = 0
        backend.setVolumeCalls = []
        backend.setMutedCalls = []
    }

    function cleanup() {
        if (desktop) {
            desktop.destroy()
            desktop = null
            wait(100)
        }
        if (launcher) {
            launcher.destroy()
            launcher = null
            wait(20)
        }
        for (var i = 0; i < backend.createdSessions.length; ++i) {
            if (backend.createdSessions[i])
                backend.createdSessions[i].destroy()
        }
        backend.createdSessions = []
    }

    function createLauncher() {
        launcher = launcherComponent.createObject(test, {
            theme: palette,
            reducedMotion: true
        })
        verify(launcher !== null)
        return launcher
    }

    function createDesktop() {
        desktop = mainComponent.createObject(test, {
            backendApi: backend,
            sessionControlApi: sessionControl
        })
        verify(desktop !== null)
        wait(100)
        return desktop
    }

    function test_first_run_and_settings_relaunch() {
        backend.needsSetup = true
        createDesktop()
        compare(desktop.setupWindow, null)
        backend.loaded()
        verify(desktop.setupWindow !== null)
        verify(desktop.setupWindow.visible)
        desktop.setupWindow.close()
        verify(!backend.needsSetup)
        backend.loaded()
        verify(!desktop.setupWindow.visible)
        desktop.openSettings()
        mouseClick(findChild(desktop.settingsWindow, "launchSetup"))
        verify(desktop.setupWindow.visible)
        verify(!desktop.settingsWindow.visible)
    }

    function test_existing_setup_does_not_open_automatically() {
        createDesktop()
        backend.loaded()
        compare(desktop.setupWindow, null)
        compare(findChild(desktop, "volumePopup").background.radius, palette.windowRadius)
        compare(findChild(desktop, "powerDialog").background.radius, palette.windowRadius)
    }

    function test_power_actions_are_balanced_and_prominent() {
        createDesktop()
        var dialog = findChild(desktop, "powerDialog")
        dialog.open()
        tryCompare(dialog, "opened", true)

        var restart = findChild(desktop, "restartAction")
        var shutdown = findChild(desktop, "shutdownAction")
        verify(restart !== null)
        verify(shutdown !== null)
        fuzzyCompare(restart.width, shutdown.width, 0.1)
        compare(restart.height, shutdown.height)

        var restartIcon = findChild(desktop, "restartActionIcon")
        var shutdownIcon = findChild(desktop, "powerActionIcon")
        compare(restartIcon.width, 34)
        compare(restartIcon.height, 34)
        compare(restartIcon.strokeWidth, 2.5)
        compare(shutdownIcon.width, 34)
        compare(shutdownIcon.height, 34)
        compare(shutdownIcon.strokeWidth, 2.5)
        dialog.close()
    }

    function test_volume_button_opens_vertical_slider_and_mutes() {
        createDesktop()
        var button = findChild(desktop, "volumeButton")
        verify(button !== null)
        compare(button.Accessible.name, "Volume · 65%")
        var glyph = findChild(desktop, "volumeButtonGlyph")
        compare(glyph.width, 22)
        compare(glyph.height, 22)
        fuzzyCompare(glyph.x + glyph.width / 2, glyph.parent.width / 2, 0.1)
        fuzzyCompare(glyph.y + glyph.height / 2, glyph.parent.height / 2, 0.1)
        mouseClick(button)

        var popup = findChild(desktop, "volumePopup")
        tryCompare(popup, "opened", true)
        compare(backend.refreshVolumeCalls, 1)

        var slider = findChild(desktop, "volumeSlider")
        compare(slider.orientation, Qt.Vertical)
        compare(slider.from, 0)
        compare(slider.to, 100)
        compare(popup.syncingVolume, false)

        var up = findChild(desktop, "volumeUpButton")
        var down = findChild(desktop, "volumeDownButton")
        var mute = findChild(desktop, "muteButton")
        verify(up.y < slider.y)
        verify(slider.y < down.y)
        verify(down.y < mute.y)
        compare(up.background.border.width, 0)
        compare(down.background.border.width, 0)
        compare(mute.background.border.width, 0)
        var muteGlyph = findChild(desktop, "muteButtonGlyph")
        compare(muteGlyph.width, 22)
        compare(muteGlyph.height, 22)
        mouseMove(up, up.width / 2, up.height / 2)
        tryCompare(up.background, "color", palette.input)

        mouseClick(up)
        compare(slider.value, 70)
        compare(backend.setVolumeCalls.length, 1)
        compare(backend.setVolumeCalls[0], 70)
        mouseClick(down)
        compare(slider.value, 65)
        compare(backend.setVolumeCalls.length, 2)
        compare(backend.setVolumeCalls[1], 65)

        var oldValue = slider.value
        mouseDrag(slider, slider.handle.x + slider.handle.width / 2,
                  slider.handle.y + slider.handle.height / 2, 0, -40, Qt.LeftButton)
        wait(200)
        compare(backend.setVolumeCalls.length, 3)
        verify(slider.value > oldValue)
        compare(backend.setVolumeCalls[2], Math.round(slider.value))

        mouseClick(mute)
        compare(backend.setMutedCalls.length, 1)
        compare(backend.setMutedCalls[0], true)
        popup.close()
    }

    function minimize(window) {
        window.showMinimized()
        tryCompare(window, "visibility", Window.Minimized)
    }

    function findVisibleText(root, text) {
        if (!root)
            return null
        if (root.visible === true && root.text === text)
            return root
        var children = root.children || []
        for (var i = 0; i < children.length; ++i) {
            var match = findVisibleText(children[i], text)
            if (match)
                return match
        }
        var data = root.data || []
        for (var j = 0; j < data.length; ++j) {
            var dataMatch = findVisibleText(data[j], text)
            if (dataMatch)
                return dataMatch
        }
        return null
    }

    function test_orb_keeps_accessibility_without_visual_tooltip() {
        var orb = createLauncher()
        compare(orb.Accessible.name, "Start a new chat")
        compare(orb.Accessible.description, "Open a new conversation")
        orb.forceActiveFocus()
        tryCompare(orb, "activeFocus", true)
        wait(1000)
        compare(findVisibleText(test.Window.window, "New chat"), null)
    }

    function test_visible_chats_do_not_prevent_new_sessions() {
        var main = createDesktop()
        var first = main.openChat()
        var second = main.openChat()
        compare(findChild(first, "chatWindowSurface").radius, palette.windowRadius)
        compare(findChild(first, "chatWindowTitle").font.pixelSize, 22)
        var header = findChild(first, "chatHeader")
        var profile = findChild(first, "chatProfile")
        verify(header !== null)
        verify(profile !== null)
        compare(profile.parent, header)
        tryCompare(header, "height", 44)
        fuzzyCompare(header.mapToItem(first.contentItem, 0, 0).y, 24, 0.5)
        compare(findChild(first, "chatCloseButton").implicitWidth, 36)
        compare(findChild(first, "chatCloseButton").implicitHeight, 36)
        compare(findChild(first, "chatCloseButton").background.radius, 8)
        compare(backend.createSessionCalls, 2)
        verify(first !== null && first !== undefined)
        verify(second !== null && second !== undefined)
        verify(first !== second)
        verify(first.visibility !== Window.Minimized)
        verify(second.visibility !== Window.Minimized)
    }

    function test_profile_stays_top_center_when_resized_data() {
        return [
            {tag: "compact", width: 480, height: 480},
            {tag: "default", width: 740, height: 650},
            {tag: "wide", width: 1040, height: 720}
        ]
    }

    function test_profile_stays_top_center_when_resized(data) {
        var chat = createDesktop().openChat()
        var profile = findChild(chat, "chatProfile")
        var header = findChild(chat, "chatHeader")
        var controls = findChild(chat, "chatWindowControls")
        chat.width = data.width
        chat.height = data.height
        tryCompare(header, "width", chat.width - 48)
        tryCompare(header, "height", 44)
        var center = profile.mapToItem(chat.contentItem, profile.width / 2, profile.height / 2)
        fuzzyCompare(center.x, chat.width / 2, 0.5)
        fuzzyCompare(center.y, 46, 0.5)
        verify(profile.x + profile.width < controls.x)
        var title = findChild(chat, "chatWindowTitle")
        verify(title.mapToItem(header, title.width, 0).x < profile.x)
    }

    function test_minimized_chats_restore_newest_first_without_new_sessions() {
        var main = createDesktop()
        var first = main.openChat()
        var second = main.openChat()
        minimize(first)
        minimize(second)
        compare(main.minimizedChatCount, 2)

        compare(main.openChat(), second)
        compare(backend.createSessionCalls, 2)
        compare(main.minimizedChatCount, 1)
        tryVerify(function() { return second.visibility !== Window.Minimized }, 1000)

        compare(main.openChat(), first)
        compare(backend.createSessionCalls, 2)
        compare(main.minimizedChatCount, 0)
        tryVerify(function() { return first.visibility !== Window.Minimized }, 1000)
    }

    function test_closing_minimized_chat_removes_it_from_tracking() {
        var main = createDesktop()
        var chat = main.openChat()
        var session = backend.createdSessions[0]
        minimize(chat)
        compare(main.minimizedChatCount, 1)

        chat.close()
        tryCompare(session, "closeCalls", 1)
        compare(main.minimizedChatCount, 0)

        verify(main.openChat() !== null)
        compare(backend.createSessionCalls, 2)
    }
}
