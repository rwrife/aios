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
        property var localModels: baselineModels()
        function baselineModels() {
            return {ram_bytes: 4 * 1073741824, free_disk_bytes: 20 * 1073741824, models: [
            {id: "qwen3-0.6b", name: "Qwen3 0.6B Q4_K_M", bundled: true, installed: true, selected: true, available: true, tool_use: true, bytes: 484220320, ram_gib: 2, license: "Apache-2.0", note: "Basic chat"},
            {id: "qwen3-1.7b", name: "Qwen3 1.7B Q4_K_M", installed: false, selected: false, available: true, tool_use: true, bytes: 1282439584, ram_gib: 4, license: "Apache-2.0", note: "Tools"},
            {id: "qwen3-8b", name: "Qwen3 8B Q4_K_M", installed: false, selected: false, available: false, tool_use: true, bytes: 5027784224, ram_gib: 12, license: "Apache-2.0", note: "Tools", reason: "Needs about 12 GiB total RAM."}
        ]}
        }
        function refreshLocalModels() {}
        function setupLocal(id) { selectedId = id }
        // Mirrors the shell: after an install finishes, the refreshed
        // inventory marks the new model as the configured selection.
        function completeInstall(id) {
            var models = []
            for (var i = 0; i < localModels.models.length; ++i) {
                var entry = localModels.models[i]
                models.push({id: entry.id, name: entry.name, bundled: entry.bundled,
                             installed: entry.id === id ? true : entry.installed,
                             selected: entry.id === id,
                             available: entry.available, tool_use: entry.tool_use,
                             bytes: entry.bytes, ram_gib: entry.ram_gib,
                             license: entry.license, note: entry.note, reason: entry.reason})
            }
            localModels = {ram_bytes: localModels.ram_bytes,
                           free_disk_bytes: localModels.free_disk_bytes,
                           models: models}
        }
    }
    LocalModels { id: picker; width: 600; backend: backend; theme: palette }
    function init() {
        backend.busy = false; backend.configuring = false; backend.selectedId = ""
        backend.localModels = backend.baselineModels()
        findChild(picker, "localModelChoice").currentIndex = 0
    }
    function test_select_and_install_specific_model() {
        findChild(picker, "localModelChoice").currentIndex = 1
        var button = findChild(picker, "installLocalModel")
        compare(button.text, "Download and use model")
        mouseClick(button)
        compare(backend.selectedId, "qwen3-1.7b")
    }

    function test_completed_download_becomes_the_selection() {
        // Reproduce issue #78: after "Download and use model" finishes, the
        // refreshed inventory must show the new model as selected, not the
        // previously configured one.
        var choice = findChild(picker, "localModelChoice")
        compare(choice.currentIndex, 0)
        choice.currentIndex = 1
        mouseClick(findChild(picker, "installLocalModel"))
        compare(backend.selectedId, "qwen3-1.7b")
        backend.completeInstall("qwen3-1.7b")
        wait(50)
        compare(choice.currentIndex, 1)
        verify(picker.selected.selected)
        compare(picker.selected.id, "qwen3-1.7b")
        compare(findChild(picker, "installLocalModel").text, "Use selected model")
    }

    function test_plain_refresh_keeps_browsed_selection() {
        // Replacing the inventory without changing the configured model is a
        // plain refresh; whatever the user is browsing stays untouched.
        var choice = findChild(picker, "localModelChoice")
        backend.completeInstall("qwen3-0.6b")
        wait(50)
        compare(choice.currentIndex, 0)
        choice.currentIndex = 2
        backend.completeInstall("qwen3-0.6b")
        wait(50)
        compare(choice.currentIndex, 2)
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
