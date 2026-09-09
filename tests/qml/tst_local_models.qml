import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "LocalModels"
    when: windowShown
    visible: true
    width: 640; height: 600
    Theme { id: palette }
    QtObject {
        id: backend
        property bool busy: false
        property bool configuring: false
        property string selectedId: ""
        property var localModels: ({ram_bytes: 4 * 1073741824, free_disk_bytes: 20 * 1073741824, models: [
            {id: "qwen3-0.6b", name: "Qwen3 0.6B Q4_K_M", bundled: true, installed: true, available: true, tool_use: true, bytes: 484220320, ram_gib: 2, license: "Apache-2.0", note: "Basic chat"},
            {id: "qwen3-1.7b", name: "Qwen3 1.7B Q4_K_M", installed: false, available: true, tool_use: true, bytes: 1282439584, ram_gib: 4, license: "Apache-2.0", note: "Tools"},
            {id: "qwen3-8b", name: "Qwen3 8B Q4_K_M", installed: false, available: false, tool_use: true, bytes: 5027784224, ram_gib: 12, license: "Apache-2.0", note: "Tools", reason: "Needs about 12 GiB total RAM."}
        ]})
        function refreshLocalModels() {}
        function setupLocal(id) { selectedId = id }
    }
    LocalModels { id: picker; width: 600; backend: backend; theme: palette }
    function init() {
        backend.busy = false; backend.configuring = false; backend.selectedId = ""
        findChild(picker, "localModelChoice").currentIndex = 0
    }
    function test_select_and_install_specific_model() {
        findChild(picker, "localModelChoice").currentIndex = 1
        var button = findChild(picker, "installLocalModel")
        compare(button.text, "Download and use model")
        mouseClick(button)
        compare(backend.selectedId, "qwen3-1.7b")
    }
    function test_resource_and_busy_guards() {
        findChild(picker, "localModelChoice").currentIndex = 2
        var button = findChild(picker, "installLocalModel")
        verify(!button.enabled)
        verify(findChild(picker, "localModelReason").text.indexOf("12 GiB") >= 0)
        findChild(picker, "localModelChoice").currentIndex = 0
        compare(button.text, "Use selected model")
        verify(button.enabled)
        backend.busy = true
        verify(!button.enabled)
        backend.busy = false; backend.configuring = true
        verify(!button.enabled)
    }
}
