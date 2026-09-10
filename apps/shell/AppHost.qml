import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import "WindowSizing.js" as WindowSizing

Window {
    id: calculator
    width: WindowSizing.extent(360, Screen.width, Screen.desktopAvailableWidth)
    height: WindowSizing.extent(520, Screen.height, Screen.desktopAvailableHeight)
    minimumWidth: WindowSizing.extent(320, Screen.width, Screen.desktopAvailableWidth)
    minimumHeight: WindowSizing.extent(460, Screen.height, Screen.desktopAvailableHeight)
    visible: true
    color: "#101b27"
    title: appTitle

    property string appTitle: "Calculator"
    property string displayValue: "0"
    property real storedValue: 0
    property string pendingOperator: ""
    property bool replaceDisplay: true
    property bool pendingNegativeEntry: false
    property bool errorState: false

    function reset() {
        displayValue = "0"
        storedValue = 0
        pendingOperator = ""
        replaceDisplay = true
        pendingNegativeEntry = false
        errorState = false
    }

    function showError() {
        displayValue = "Error"
        storedValue = 0
        pendingOperator = ""
        replaceDisplay = true
        pendingNegativeEntry = false
        errorState = true
    }

    function formatNumber(value) {
        if (!isFinite(value)) {
            showError()
            return false
        }
        var normalized
        if (Math.floor(value) === value) {
            if (Math.abs(value) > 9007199254740991) {
                showError()
                return false
            }
            normalized = value
        } else {
            normalized = Number(value.toPrecision(15))
        }
        if (!isFinite(normalized)) {
            showError()
            return false
        }
        var formatted = String(normalized)
        if (formatted === "Infinity" || formatted === "-Infinity" || formatted === "NaN") {
            showError()
            return false
        }
        displayValue = normalized === 0 ? "0" : formatted
        return true
    }

    function parseOperand() {
        var value = Number(displayValue)
        if (!isFinite(value) || Math.abs(value) > 9007199254740991) {
            showError()
            return null
        }
        return value
    }

    function applyPending(operand) {
        var result = storedValue
        if (pendingOperator === "+")
            result += operand
        else if (pendingOperator === "−")
            result -= operand
        else if (pendingOperator === "×")
            result *= operand
        else if (pendingOperator === "÷") {
            if (operand === 0) {
                showError()
                return false
            }
            result /= operand
        }
        if (!formatNumber(result))
            return false
        storedValue = result
        return true
    }

    function inputDigit(digit) {
        if (errorState)
            reset()
        if (replaceDisplay || displayValue === "0" || displayValue === "-0") {
            displayValue = pendingNegativeEntry ? "-" + digit : digit
            replaceDisplay = false
            pendingNegativeEntry = false
            return
        }
        if (displayValue.length < 16)
            displayValue += digit
    }

    function inputDecimal() {
        if (errorState)
            reset()
        if (replaceDisplay) {
            displayValue = pendingNegativeEntry ? "-0." : "0."
            replaceDisplay = false
            pendingNegativeEntry = false
        } else if (displayValue.indexOf(".") < 0) {
            displayValue += "."
        }
    }

    function chooseOperator(operator) {
        if (errorState)
            return
        if (pendingOperator !== "" && replaceDisplay) {
            pendingOperator = operator
            pendingNegativeEntry = false
            formatNumber(storedValue)
            return
        }
        var operand = parseOperand()
        if (operand === null)
            return
        if (pendingOperator !== "" && !replaceDisplay) {
            if (!applyPending(operand))
                return
        } else {
            storedValue = operand
        }
        pendingOperator = operator
        replaceDisplay = true
        pendingNegativeEntry = false
    }

    function calculate() {
        if (errorState || pendingOperator === "" || replaceDisplay)
            return
        var operand = parseOperand()
        if (operand === null || !applyPending(operand))
            return
        pendingOperator = ""
        replaceDisplay = true
        pendingNegativeEntry = false
    }

    function backspace() {
        if (errorState) {
            reset()
            return
        }
        if (replaceDisplay)
            return
        if (displayValue.length <= 1
                || (displayValue.charAt(0) === "-" && displayValue.length === 2)) {
            displayValue = "0"
            replaceDisplay = true
        } else {
            displayValue = displayValue.slice(0, -1)
        }
    }

    function toggleSign() {
        if (errorState)
            return
        if (replaceDisplay) {
            if (pendingOperator !== "") {
                pendingNegativeEntry = !pendingNegativeEntry
                displayValue = pendingNegativeEntry ? "-0" : "0"
                return
            }
            if (displayValue === "0")
                return
            displayValue = displayValue.charAt(0) === "-"
                ? displayValue.slice(1) : "-" + displayValue
            pendingNegativeEntry = displayValue.charAt(0) === "-"
            return
        }
        if (displayValue === "0")
            return
        displayValue = displayValue.charAt(0) === "-"
            ? displayValue.slice(1) : "-" + displayValue
        pendingNegativeEntry = false
    }

    function press(key) {
        var value = String(key)
        if (/^[0-9]$/.test(value))
            inputDigit(value)
        else if (value === ".")
            inputDecimal()
        else if (value === "+" || value === "−" || value === "×" || value === "÷")
            chooseOperator(value)
        else if (value === "-")
            chooseOperator("−")
        else if (value === "*")
            chooseOperator("×")
        else if (value === "/")
            chooseOperator("÷")
        else if (value === "=")
            calculate()
        else if (value === "C")
            reset()
        else if (value === "⌫")
            backspace()
        else if (value === "±")
            toggleSign()
    }

    function handleKey(key, text) {
        if (/^[0-9]$/.test(text)) {
            press(text)
            return true
        }
        if (text === "." || text === "+" || text === "-" || text === "*"
                || text === "/" || text === "=") {
            press(text)
            return true
        }
        if (key === Qt.Key_Return || key === Qt.Key_Enter) {
            press("=")
            return true
        }
        if (key === Qt.Key_Escape || key === Qt.Key_C) {
            press("C")
            return true
        }
        if (key === Qt.Key_Backspace) {
            press("⌫")
            return true
        }
        return false
    }

    component CalcButton: Button {
        required property string keyValue
        required property string accessibleName
        Layout.fillWidth: true
        Layout.fillHeight: true
        focusPolicy: Qt.StrongFocus
        text: keyValue
        font.pixelSize: 22
        Accessible.role: Accessible.Button
        Accessible.name: accessibleName
        background: Rectangle {
            radius: 12
            color: parent.down ? "#4c6574" : parent.hovered ? "#354e60" : "#203340"
            border.color: "#4c6574"
        }
        contentItem: Text {
            text: parent.text
            color: "#f1f5f6"
            font: parent.font
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            textFormat: Text.PlainText
        }
        onClicked: {
            calculator.press(keyValue)
        }
    }

    FocusScope {
        id: keyboardFocus
        anchors.fill: parent
        focus: true
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
            event.accepted = calculator.handleKey(event.key, event.text)
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            Label {
                Layout.fillWidth: true
                text: calculator.appTitle
                textFormat: Text.PlainText
                color: "#b2c3cd"
                font.pixelSize: 18
                elide: Text.ElideRight
                Accessible.role: Accessible.Heading
                Accessible.name: text
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 96
                radius: 14
                color: "#172633"
                border.color: "#4c6574"

                Label {
                    objectName: "calculatorDisplay"
                    anchors.fill: parent
                    anchors.margins: 18
                    text: calculator.displayValue
                    textFormat: Text.PlainText
                    color: "#f1f5f6"
                    font.pixelSize: 38
                    horizontalAlignment: Text.AlignRight
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideLeft
                    Accessible.role: Accessible.StaticText
                    Accessible.name: "Calculator display " + text
                }
            }

            GridLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                columns: 4
                rowSpacing: 8
                columnSpacing: 8

                CalcButton { keyValue: "C"; accessibleName: "Clear" }
                CalcButton { keyValue: "±"; accessibleName: "Toggle sign" }
                CalcButton { keyValue: "⌫"; accessibleName: "Backspace" }
                CalcButton { keyValue: "÷"; accessibleName: "Divide" }
                CalcButton { keyValue: "7"; accessibleName: "Seven" }
                CalcButton { keyValue: "8"; accessibleName: "Eight" }
                CalcButton { keyValue: "9"; accessibleName: "Nine" }
                CalcButton { keyValue: "×"; accessibleName: "Multiply" }
                CalcButton { keyValue: "4"; accessibleName: "Four" }
                CalcButton { keyValue: "5"; accessibleName: "Five" }
                CalcButton { keyValue: "6"; accessibleName: "Six" }
                CalcButton { keyValue: "−"; accessibleName: "Subtract" }
                CalcButton { keyValue: "1"; accessibleName: "One" }
                CalcButton { keyValue: "2"; accessibleName: "Two" }
                CalcButton { keyValue: "3"; accessibleName: "Three" }
                CalcButton { keyValue: "+"; accessibleName: "Add" }
                CalcButton { keyValue: "0"; accessibleName: "Zero" }
                CalcButton { keyValue: "."; accessibleName: "Decimal point" }
                CalcButton { keyValue: "="; accessibleName: "Equals"; Layout.columnSpan: 2 }
            }
        }
    }

    Component.onCompleted: keyboardFocus.forceActiveFocus()
}
