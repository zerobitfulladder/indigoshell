import QtQuick

// The agent's effects over a page it drives (effects.py, which sets every
// property: nothing animates here, so the scene draws only when Python
// changes something). `fluxShaders` is the folder of the .qsb files, set
// by effects.py before the scene is made.
Item {
    id: root
    property string mode: "off"         // off | glitch | holo

    property vector4d tint: Qt.vector4d(0, 0, 0, 0)
    property vector4d line: Qt.vector4d(0, 0, 0, 0)
    property vector4d bg: Qt.vector4d(0, 0, 0, 1)
    property vector4d keep: Qt.vector4d(0, 0, 0, 0)
    property real pitch: 3

    // The page's pixels, as last read back: the glitch's still image and
    // the scan's source.
    Image {
        id: page
        objectName: "page"
        visible: false
        cache: false
        asynchronous: false
        smooth: false
        mipmap: false
    }

    ShaderEffect {
        id: glitch
        objectName: "glitch"
        anchors.fill: parent
        visible: root.mode === "glitch"
        blending: true
        property int statusCode: status     // PySide cannot convert ShaderEffect.Status
        property variant src: page
        property size res: Qt.size(width, height)
        property real t: 0
        property real appear: 0
        property real rows: 30
        property real tear: 0
        property real split: 0
        property vector4d spark: Qt.vector4d(0, 0, 0, 0)
        property real noise: 0
        property real seed: 0
        property vector4d tint: root.tint
        property vector4d line: root.line
        property real pitch: root.pitch
        property vector4d bg: root.bg
        property real baseFade: 1
        property vector4d keep: root.keep
        fragmentShader: fluxShaders + "scanlock.frag.qsb"
    }

    ShaderEffect {
        id: holo
        objectName: "holo"
        anchors.fill: parent
        visible: root.mode === "holo"
        blending: true
        property int statusCode: status
        property variant strip: page
        property size res: Qt.size(width, height)
        property real scanning: 0
        property real head: -1000
        property real t: 0
        property real seed: 0
        property real head_px: 11
        property real wake: 100
        property real shift: 9
        property real split: 2.5
        property real rate: 18
        property real pitch: root.pitch
        property real stripTop: 0
        property real stripH: 1
        property real texTop: 0
        property real texH: 1
        property vector4d tint: root.tint
        property vector4d line: root.line
        property vector4d bg: root.bg
        property vector4d keep: root.keep
        fragmentShader: fluxShaders + "holo.frag.qsb"
    }
}
