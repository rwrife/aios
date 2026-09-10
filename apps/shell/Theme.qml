import QtQuick

QtObject {
    property string selected: "blue"
    readonly property var choices: [
        {key: "blue", name: "Ocean", hue: 0.57, swatch: "#779bae"},
        {key: "teal", name: "Lagoon", hue: 0.48, swatch: "#78aaa5"},
        {key: "sage", name: "Sage", hue: 0.30, swatch: "#92a78c"},
        {key: "amber", name: "Amber", hue: 0.11, swatch: "#b6a06d"},
        {key: "copper", name: "Copper", hue: 0.055, swatch: "#b28d77"},
        {key: "rose", name: "Rose", hue: 0.96, swatch: "#b48e9d"},
        {key: "violet", name: "Dusk", hue: 0.73, swatch: "#9b90b5"},
        {key: "slate", name: "Slate", hue: 0.60, swatch: "#929aa6"}
    ]
    readonly property int paletteIndex: {
        for (var i = 0; i < choices.length; ++i) if (choices[i].key === selected) return i
        return 0
    }
    readonly property real hue: choices[paletteIndex].hue
    readonly property real saturation: selected === "slate" ? 0.055 : 0.24
    readonly property color night: paletteIndex === 0 ? "#101b27" : Qt.hsla(hue, saturation, 0.10, 1)
    readonly property color horizon: paletteIndex === 0 ? "#354e60" : Qt.hsla(hue, saturation, 0.29, 1)
    readonly property color panel: paletteIndex === 0 ? "#172633" : Qt.hsla(hue, saturation, 0.145, 1)
    readonly property color input: paletteIndex === 0 ? "#203340" : Qt.hsla(hue, saturation, 0.19, 1)
    readonly property color ink: "#f1f5f6"
    readonly property color muted: paletteIndex === 0 ? "#b2c3cd" : Qt.hsla(hue, saturation * 0.55, 0.74, 1)
    readonly property color accent: paletteIndex === 0 ? "#bde4e6" : Qt.hsla(hue, saturation, 0.81, 1)
    readonly property color line: paletteIndex === 0 ? "#4c6574" : Qt.hsla(hue, saturation, 0.38, 1)
    readonly property color wave: paletteIndex === 0 ? "#a7c7d5" : Qt.hsla(hue, saturation, 0.74, 1)
    readonly property real windowRadius: 16
    function waveAlpha(alpha) { return Qt.rgba(wave.r, wave.g, wave.b, alpha) }
}
