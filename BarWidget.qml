import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import qs.Commons as Commons

BarWidget {
    id: root
    moduleName: "YAPM"
    readonly property bool opened: panelLoader.item ? panelLoader.item.opened : false
    readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing : false
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    function open() {
        if (panelLoader.item)
            panelLoader.item.open();
    }
    function close() {
        if (panelLoader.item)
            panelLoader.item.close();
    }
    function toggle() {
        if (panelLoader.item)
            panelLoader.item.toggle();
    }
    function setPage(value) {
        if (["installed", "marketplace", "updates"].indexOf(value) === -1 || !panelLoader.item)
            return;
        panelLoader.item.selectTab(value);
    }
    function closeForPopoutSwitch() {
        if (panelLoader.item)
            panelLoader.item.closeForPopoutSwitch();
    }
    function injectPanel() {
        if (!panelLoader.item)
            return;
        panelLoader.item.bar = root.bar;
        panelLoader.item.anchorItem = button;
        panelLoader.item.hostWidget = root;
        panelLoader.item.settings = root.settings;
    }
    onBarChanged: injectPanel()
    onSettingsChanged: injectPanel()

    Commons.Translations {
        id: strings
        directory: Qt.resolvedUrl("i18n")
    }
    ShellIpc {
        target: root.moduleName
        function page(value: string): void {
            const items = root.bar ? root.bar.moduleWidgets(root.moduleName) : [root];
            for (let i = 0; i < items.length; i++)
                items[i].setPage(value);
            Quickshell.execDetached(["omarchy-shell", "shell", "summon", root.moduleName, "{}"]);
        }
    }
    Loader {
        id: panelLoader
        active: true
        visible: false
        source: Qt.resolvedUrl("Panel.qml")
        onLoaded: {
            root.injectPanel();
            Qt.callLater(root.injectPanel);
        }
    }
    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: "\uf1b2"
        // Plugin-owned translated text is escaped before the shared tooltip sink.
        tooltipText: String(strings.t("title")).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        onPressed: function (code) {
            if (code === Qt.LeftButton)
                root.toggle();
        }
    }
}
