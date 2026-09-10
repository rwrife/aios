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
            visible: false
            appTitle: "Test Calculator"
        }
    }

    property var host

    function init() {
        host = createTemporaryObject(hostComponent, test)
        verify(host !== null)
        host.reset()
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

    function test_divide_by_zero_and_digit_reset() {
        host.press("8")
        host.press("÷")
        host.press("0")
        host.press("=")
        compare(host.displayValue, "Error")
        host.press("7")
        compare(host.displayValue, "7")
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
}
