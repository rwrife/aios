import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "ChatScroll"
    when: windowShown
    width: 900; height: 750
    QtObject {
        id: theme
        property color panel: "#172633"
        property color muted: "#b2c3cd"
        property color ink: "#f1f5f6"
        property color input: "#203340"
        property color accent: "#bde4e6"
        property color line: "#4c6574"
        property color night: "#101b27"
        property color horizon: "#354e60"
    }
    QtObject {
        id: backend
        property var config: ({mode: "local", model_path: "/starter.gguf"})
        property bool busy: false
        property string status: ""
        property var subscription: ({})
        property string loginUrl: ""
        property string loginCode: ""
        signal configured()
    }
    QtObject {
        id: session
        property var messages: []
        property bool busy: true
        property bool recording: false
        property bool speaking: false
        property var attachments: []
        property string status: "Replying…"
        signal changed()
        signal transcribed(string text)
        function closeSession() {}
        function send(text) { messages = messages.concat([{role: "user", content: text}, {role: "assistant", content: ""}]); changed() }
    }
    Component { id: chatComponent; ChatWindow {} }
    property var chat
    property var view
    function init() {
        session.messages = []; session.busy = true
        chat = chatComponent.createObject(test, {backend: backend, session: session, theme: theme})
        verify(chat !== null)
        view = findChild(chat, "conversation")
        verify(view !== null)
        wait(50)
    }
    function cleanup() { chat.destroy(); wait(20) }
    function seed() {
        var messages = []
        for (var i = 0; i < 16; ++i)
            messages.push({role: i % 2 ? "assistant" : "user", content: "Message " + i + "\n" + "A line of content.\n".repeat(4)})
        session.messages = messages; session.changed()
        wait(100)
        tryVerify(function() { return view.atYEnd }, 2000)
    }
    function stream(lines) {
        var messages = JSON.parse(JSON.stringify(session.messages))
        messages[messages.length - 1].content += "More streamed text, wrapping across the available width. ".repeat(lines)
        session.messages = messages; session.changed()
    }
    function test_stream_follows_growing_reply() {
        seed()
        var row = view.itemAtIndex(15)
        verify(row !== null)
        var originalHeight = row.height
        for (var i = 0; i < 8; ++i) { stream(3); wait(30); tryVerify(function() { return view.atYEnd }, 1000) }
        compare(view.itemAtIndex(15), row, "Streaming must preserve the delegate")
        verify(row.height > originalHeight, "Streamed content must grow the visible reply")
        session.busy = false; session.changed()
        tryVerify(function() { return view.atYEnd }, 1000)
        chat.height -= 70
        tryVerify(function() { return view.atYEnd }, 1000)
    }
    function test_empty_chat_setup_button_opens_account_form() {
        compare(view.count, 0)
        var button = findChild(chat, "setupAccount")
        verify(button !== null)
        waitForRendering(button)
        mouseClick(button)
        var form = findChild(chat, "bubbleEnrollment")
        verify(form !== null)
        tryCompare(form, "opened", true)
        compare(form.creating, true)
        form.close()
    }
    function test_reading_history_and_resuming() {
        seed()
        mouseWheel(view, view.width / 2, view.height / 2, 0, 240)
        wait(300)
        compare(view.followLatest, false)
        var position = view.contentY
        stream(20); wait(100)
        fuzzyCompare(view.contentY, position, 1)
        view.forceLayout(); view.positionViewAtEnd(); wait(50); view.movementEnded()
        compare(view.followLatest, true)
        stream(5)
        tryVerify(function() { return view.atYEnd }, 1000)
    }
    function test_send_returns_to_latest() {
        seed()
        mouseWheel(view, view.width / 2, view.height / 2, 0, 240)
        wait(300)
        compare(view.followLatest, false)
        session.busy = false
        findChild(chat, "composer").text = "A new message"
        chat.submit()
        wait(100)
        compare(view.count, 18)
        compare(view.followLatest, true)
        tryVerify(function() { return view.atYEnd }, 1000)
    }
}
