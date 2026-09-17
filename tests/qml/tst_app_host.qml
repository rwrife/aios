import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "AppHost"
    when: windowShown
    width: 500
    height: 650

    Component {
        id: hostComponent
        AppHost {
            visible: true
            appTitle: "Test Calculator"
        }
    }

    property var host

    function init() {
        host = createTemporaryObject(hostComponent, test)
        verify(host !== null)
        tryCompare(host, "visible", true)
        host.requestActivate()
        waitForRendering(host.contentItem)
        wait(50)
        host.reset()
    }

    function cleanup() {
        if (host) {
            host.close()
            host.destroy()
            host = null
        }
    }

    function pressDigits(digits) {
        for (var i = 0; i < digits.length; ++i)
            host.press(digits.charAt(i))
    }

    function findButton(item, keyValue) {
        if (!item)
            return null
        if (item.keyValue === keyValue)
            return item
        var children = item.children || []
        for (var i = 0; i < children.length; ++i) {
            var found = findButton(children[i], keyValue)
            if (found)
                return found
        }
        return null
    }

    function test_addition() {
        host.press("1")
        host.press("2")
        host.press("+")
        host.press("3")
        host.press("=")
        compare(host.displayValue, "15")
    }

    function test_chained_arithmetic() {
        host.press("2")
        host.press("+")
        host.press("3")
        host.press("×")
        host.press("4")
        host.press("=")
        compare(host.displayValue, "20")
    }

    function test_decimal() {
        host.press("1")
        host.press(".")
        host.press("5")
        host.press("+")
        host.press("0")
        host.press(".")
        host.press("2")
        host.press("5")
        host.press("=")
        compare(host.displayValue, "1.75")
    }

    function test_large_integer_results_are_formatted_exactly() {
        var cases = [
            {lhs: "12345678", operator: "×", rhs: "9", expected: "111111102"},
            {lhs: "123456789", operator: "÷", rhs: "3", expected: "41152263"},
            {lhs: "10000002", operator: "×", rhs: "9", expected: "90000018"},
            {lhs: "1234567890123", operator: "+", rhs: "0", expected: "1234567890123"},
            {lhs: "999999999999999", operator: "+", rhs: "0", expected: "999999999999999"}
        ]
        for (var i = 0; i < cases.length; ++i) {
            var testCase = cases[i]
            host.reset()
            pressDigits(testCase.lhs)
            host.press(testCase.operator)
            pressDigits(testCase.rhs)
            host.press("=")
            compare(host.displayValue, testCase.expected)
        }
    }

    function test_unsafe_integer_operand_enters_error() {
        pressDigits("9007199254740993")
        host.press("−")
        compare(host.displayValue, "Error")
        compare(host.errorState, true)
    }

    function test_divide_by_zero_and_digit_reset() {
        host.press("8")
        host.press("÷")
        host.press("0")
        host.press("=")
        compare(host.displayValue, "Error")
        host.press("7")
        compare(host.displayValue, "7")
    }

    function test_huge_multiplication_enters_error_and_resets_cleanly() {
        host.displayValue = "1e308"
        host.storedValue = 1e308
        host.pendingOperator = "×"
        host.replaceDisplay = false
        host.calculate()
        compare(host.displayValue, "Error")
        compare(host.errorState, true)
        host.press("7")
        compare(host.displayValue, "7")
        compare(host.errorState, false)
    }

    function test_clear_and_backspace() {
        host.press("1")
        host.press("2")
        host.press("3")
        host.press("⌫")
        compare(host.displayValue, "12")
        host.press("C")
        compare(host.displayValue, "0")
    }

    function test_sign_toggle_participates_in_arithmetic() {
        host.press("5")
        host.press("±")
        host.press("+")
        host.press("2")
        host.press("=")
        compare(host.displayValue, "-3")
    }

    function test_keyboard_mapping() {
        verify(host.handleKey(Qt.Key_9, "9"))
        verify(host.handleKey(Qt.Key_Asterisk, "*"))
        verify(host.handleKey(Qt.Key_3, "3"))
        verify(host.handleKey(Qt.Key_Return, ""))
        compare(host.displayValue, "27")
        verify(host.handleKey(Qt.Key_Escape, ""))
        compare(host.displayValue, "0")
        verify(host.handleKey(Qt.Key_4, "4"))
        verify(host.handleKey(Qt.Key_Backspace, ""))
        compare(host.displayValue, "0")
    }

    function test_toggle_sign_preserves_fresh_entry_after_equals_and_operator() {
        host.press("5")
        host.press("+")
        host.press("2")
        host.press("=")
        compare(host.displayValue, "7")
        host.press("±")
        compare(host.displayValue, "-7")
        host.press("3")
        compare(host.displayValue, "-3")

        host.reset()
        host.press("8")
        host.press("+")
        host.press("±")
        compare(host.displayValue, "-0")
        host.press("4")
        compare(host.displayValue, "-4")
        host.press("=")
        compare(host.displayValue, "4")

        host.reset()
        host.press("2")
        host.press("+")
        host.press("±")
        host.press("×")
        host.press("3")
        host.press("=")
        compare(host.displayValue, "6")
    }

    function test_clicked_button_keeps_focus_and_real_key_events_still_work() {
        var button = findButton(host.contentItem, "7")
        verify(button !== null)
        host.requestActivate()
        wait(50)
        mouseClick(button)
        tryCompare(button, "activeFocus", true)
        compare(host.displayValue, "7")
        keyClick(Qt.Key_Plus)
        keyClick(Qt.Key_3)
        keyClick(Qt.Key_Return)
        compare(host.displayValue, "10")
        keyClick(Qt.Key_Escape)
        compare(host.displayValue, "0")
        keyClick(Qt.Key_4)
        keyClick(Qt.Key_Backspace)
        compare(host.displayValue, "0")
    }

    Theme {
        id: ocean
        selected: "blue"
    }

    Theme {
        id: sage
        selected: "sage"
    }

    function test_default_palette_is_ocean() {
        compare(host.color, ocean.night)
        compare(host.appTheme, "blue")
        var display = findChild(host.contentItem, "calculatorDisplay")
        verify(display !== null)
        compare(display.color, ocean.ink)
        var button = findButton(host.contentItem, "7")
        verify(button !== null)
        compare(button.background.color, ocean.input)
    }

    function test_selected_palette_follows_the_chat_theme() {
        var themed = createTemporaryObject(themeHostComponent, test)
        verify(themed !== null)
        tryCompare(themed, "visible", true)
        waitForRendering(themed.contentItem)
        themed.requestActivate()
        wait(50)
        compare(themed.color, sage.night)
        verify(themed.color !== ocean.night)
        var display = findChild(themed.contentItem, "calculatorDisplay")
        verify(display !== null)
        compare(display.color, sage.ink)
        var panel = display.parent
        compare(panel.color, sage.panel)
        compare(panel.border.color, sage.line)
        var button = findButton(themed.contentItem, "7")
        verify(button !== null)
        compare(button.background.color, sage.input)
        compare(button.background.border.color, sage.line)
        themed.close()
        themed.destroy()
    }

    Component {
        id: themeHostComponent
        AppHost {
            visible: true
            appTitle: "Themed Calculator"
            appTheme: "sage"
        }
    }
}
