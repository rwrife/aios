import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "SubscriptionSettings"
    when: windowShown
    visible: true
    width: 650; height: 750
    Theme { id: palette }
    QtObject {
        id: backend
        property var config: ({mode: "chatgpt", subscription_model: "test-two"})
        property bool busy: false
        property string status: ""
        property var subscription: ({signed_in: true, email: "test@example.com", plan: "plus", models: [
            {id: "test-one", name: "First model"}, {id: "test-two", name: "Second model"}]})
        property string loginUrl: ""
        property string loginCode: ""
        property var operation: []
        signal configured()
        function configure(values) { config = values; configured() }
        function subscriptionAction(action, device) { operation = [action, device]; busy = true; loginUrl = "https://auth.openai.com/codex/device"; loginCode = "TEST-CODE" }
        function stop() { busy = false; loginUrl = ""; loginCode = "" }
    }
    ModelSettings { id: settings; anchors.fill: parent; backend: backend; theme: palette }
    function test_provider_model_and_login() {
        compare(findChild(settings, "modelProvider").currentIndex, 2)
        var choice = findChild(settings, "subscriptionModel")
        compare(choice.currentIndex, 2)
        choice.currentIndex = 1; choice.activated(1)
        findChild(settings, "saveModel").clicked()
        compare(backend.config.mode, "chatgpt")
        compare(backend.config.subscription_model, "test-one")
        findChild(settings, "deviceLogin").clicked()
        compare(backend.operation[0], "login")
        compare(backend.operation[1], true)
        verify(!findChild(settings, "saveModel").enabled)
        verify(findChild(settings, "cancelLogin").visible)
        findChild(settings, "cancelLogin").clicked()
        compare(backend.loginCode, "")
        verify(findChild(settings, "saveModel").enabled)
    }
}
