import QtQuick

import QGroundControl
import QGroundControl.Controls
import QGroundControl.FlyView
import FlyBlind

// FlyBlind custom Fly View layer: a stock pass-through of the tool insets (so the
// standard QGroundControl Fly View is unchanged) plus the only addition — the C2
// state-freshness "STALE STATE" alarm (second contribution). No other UI change.
Item {
    id: _root

    property var parentToolInsets               // Available screen real estate from the parent
    property var totalToolInsets:   _toolInsets  // Pass-through: our overlay reserves no extra insets
    property var mapControl

    QGCToolInsets {
        id:                     _toolInsets
        leftEdgeTopInset:       parentToolInsets.leftEdgeTopInset
        leftEdgeCenterInset:    parentToolInsets.leftEdgeCenterInset
        leftEdgeBottomInset:    parentToolInsets.leftEdgeBottomInset
        rightEdgeTopInset:      parentToolInsets.rightEdgeTopInset
        rightEdgeCenterInset:   parentToolInsets.rightEdgeCenterInset
        rightEdgeBottomInset:   parentToolInsets.rightEdgeBottomInset
        topEdgeLeftInset:       parentToolInsets.topEdgeLeftInset
        topEdgeCenterInset:     parentToolInsets.topEdgeCenterInset
        topEdgeRightInset:      parentToolInsets.topEdgeRightInset
        bottomEdgeLeftInset:    parentToolInsets.bottomEdgeLeftInset
        bottomEdgeCenterInset:  parentToolInsets.bottomEdgeCenterInset
        bottomEdgeRightInset:   parentToolInsets.bottomEdgeRightInset
    }

    //-------------------------------------------------------------------------
    //-- FlyBlind C2 state-freshness alarm.
    //   Lights up when the GCS state ages past the threshold — the silent attack
    //   that the native link/interruption monitors (heartbeat) do not catch.
    Rectangle {
        id:                 flyBlindStaleAlarm
        anchors.centerIn:   parent
        width:              staleColumn.width  + ScreenTools.defaultFontPixelWidth  * 8
        height:             staleColumn.height + ScreenTools.defaultFontPixelHeight * 3
        radius:             ScreenTools.defaultFontPixelWidth
        color:              "#D0000000"
        border.color:       "red"
        border.width:       4
        z:                  10000
        visible:            FlyBlindDetector.telemetryStale

        Column {
            id:                 staleColumn
            anchors.centerIn:   parent
            spacing:            ScreenTools.defaultFontPixelHeight

            QGCLabel {
                anchors.horizontalCenter: parent.horizontalCenter
                text:           "⚠ STALE STATE"
                color:          "red"
                font.bold:      true
                font.pointSize: ScreenTools.largeFontPointSize * 1.6
            }
            QGCLabel {
                anchors.horizontalCenter: parent.horizontalCenter
                color:          "white"
                text:           qsTr("C2 TIMELINESS ALARM — position age %1 s")
                                    .arg((FlyBlindDetector.telemetryAgeMs / 1000).toFixed(1))
            }
        }

        SequentialAnimation on opacity {
            running:    flyBlindStaleAlarm.visible
            loops:      Animation.Infinite
            NumberAnimation { to: 0.4; duration: 500 }
            NumberAnimation { to: 1.0;  duration: 500 }
        }
    }
}
