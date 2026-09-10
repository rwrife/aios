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
        property var config: test.baseConfig()
        property var lastConfigured: ({})
        property bool busy: false
        property string status: ""
        property var subscription: ({signed_in: true, email: "test@example.com", plan: "plus", models: [
            {id: "test-one", name: "First model"}, {id: "test-two", name: "Second model"}]})
        property string loginUrl: ""
        property string loginCode: ""
        property var operation: []
        signal configured()
        function configure(values) { lastConfigured = values; config = values; configured() }
        function subscriptionAction(action, device) { operation = [action, device]; busy = true; loginUrl = "https://auth.openai.com/codex/device"; loginCode = "TEST-CODE" }
        function stop() { busy = false; loginUrl = ""; loginCode = "" }
    }
    ModelSettings { id: settings; anchors.fill: parent; backend: backend; theme: palette }
    function baseConfig() {
        return {
            mode: "chatgpt",
            subscription_model: "test-two",
            agent_mode: "remote",
            agent_url: "https://agent.example/v1",
            agent_model: "reasoner",
            has_agent_key: true
        }
    }
    function init() {
        backend.config = baseConfig()
        backend.lastConfigured = ({})
        backend.busy = false
        backend.status = ""
        backend.loginUrl = ""
        backend.loginCode = ""
        backend.operation = []
        settings.reload()
    }
    function test_remote_agent_controls_load_without_secret() {
        compare(findChild(settings, "agentProvider").currentIndex, 2)
        compare(findChild(settings, "agentUrl").text, "https://agent.example/v1")
        compare(findChild(settings, "agentModel").text, "reasoner")
        compare(findChild(settings, "agentKey").text, "")
    }
    function test_new_agent_key_is_sent_once_then_cleared() {
        findChild(settings, "agentKey").text = "replacement-secret"
        findChild(settings, "saveModel").clicked()
        compare(backend.lastConfigured.agent_api_key, "replacement-secret")
        compare(findChild(settings, "agentKey").text, "")
    }
    function test_current_agent_provider_saves_without_key() {
        var provider = findChild(settings, "agentProvider")
        provider.currentIndex = 0
        provider.activated(0)
        findChild(settings, "saveModel").clicked()
        compare(backend.lastConfigured.agent_mode, "current")
        verify(!("agent_api_key" in backend.lastConfigured))
    }
    function test_agent_chatgpt_shows_subscription_and_login() {
        backend.config = {
            mode: "local",
            model: "local",
            agent_mode: "chatgpt",
            subscription_model: "test-two"
        }
        settings.reload()
        compare(findChild(settings, "modelProvider").currentIndex, 0)
        compare(findChild(settings, "agentProvider").currentIndex, 1)
        verify(findChild(settings, "subscriptionSettings").visible)
        findChild(settings, "deviceLogin").clicked()
        compare(backend.operation[0], "login")
        compare(backend.operation[1], true)
        verify(findChild(settings, "cancelLogin").visible)
        findChild(settings, "cancelLogin").clicked()
        compare(backend.loginCode, "")
    }
    function test_primary_chatgpt_model_and_login_remain() {
        compare(findChild(settings, "modelProvider").currentIndex, 2)
        var choice = findChild(settings, "subscriptionModel")
        compare(choice.currentIndex, 2)
        choice.currentIndex = 1; choice.activated(1)
        findChild(settings, "saveModel").clicked()
        compare(backend.lastConfigured.mode, "chatgpt")
        compare(backend.lastConfigured.subscription_model, "test-one")
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
