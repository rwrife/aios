import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Window {
    id: calculator
    width: 360
    height: 520
    minimumWidth: 320
    minimumHeight: 460
    visible: true
    color: "#101b27"
    title: appTitle

    property string appTitle: "Calculator"
    property string displayValue: "0"
    property real storedValue: 0
    property string pendingOperator: ""
    property bool replaceDisplay: true
    property bool errorState: false

    function reset() {
        displayValue = "0"
        storedValue = 0
        pendingOperator = ""
        replaceDisplay = true
        errorState = false
    }

    function showError() {
        displayValue = "Error"
        storedValue = 0
        pendingOperator = ""
        replaceDisplay = true
        errorState = true
    }

    function formatNumber(value) {
        if (!isFinite(value)) {
            showError()
            return false
        }
        var rounded = Math.round(value * 1000000000000) / 1000000000000
        displayValue = rounded === 0 ? "0" : String(rounded)
        return true
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
        if (replaceDisplay || displayValue === "0") {
            displayValue = digit
            replaceDisplay = false
            return
        }
        if (displayValue.length < 16)
            displayValue += digit
    }

    function inputDecimal() {
        if (errorState)
            reset()
        if (replaceDisplay) {
            displayValue = "0."
            replaceDisplay = false
        } else if (displayValue.indexOf(".") < 0) {
            displayValue += "."
        }
    }

    function chooseOperator(operator) {
        if (errorState)
            return
        var operand = Number(displayValue)
        if (pendingOperator !== "" && !replaceDisplay) {
            if (!applyPending(operand))
                return
        } else {
            storedValue = operand
        }
        pendingOperator = operator
        replaceDisplay = true
    }

    function calculate() {
        if (errorState || pendingOperator === "" || replaceDisplay)
            return
        if (!applyPending(Number(displayValue)))
            return
        pendingOperator = ""
        replaceDisplay = true
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
        if (displayValue === "0")
            return
        displayValue = displayValue.charAt(0) === "-"
            ? displayValue.slice(1) : "-" + displayValue
        replaceDisplay = false
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
            keyboardFocus.forceActiveFocus()
        }
    }

    FocusScope {
        id: keyboardFocus
        anchors.fill: parent
        focus: true
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
