pragma ComponentBehavior: Bound

import QtQuick
import Quickshell.Io
import qs.Ui
import qs.Commons
import qs.Commons as Commons

Panel {
    id: root
    moduleName: "yapm"
    manageIpc: false
    property var anchorItem: null
    property var hostWidget: null
    property string tab: "installed"
    property string query: ""
    property string origin: "official"
    property var collapsedSections: ({})
    readonly property bool enableAfterInstall: setting("enableAfterInstall", false) === true
    property int pageIndex: 0
    property var snapshot: ({
            rows: [],
            total: 0,
            page: 0,
            updates: 0,
            catalogAt: 0,
            checkedAt: 0,
            job: {
                busy: false
            }
        })
    property string errorText: ""
    property var pending: null
    property string positioningId: ""
    property string positionSection: "right"
    property string launchAction: ""
    property string launchId: ""
    property bool launching: false
    readonly property bool busy: launching || snapshot.job.busy === true
    readonly property string backend: decodeURIComponent(String(Qt.resolvedUrl("backend.py")).replace(/^file:\/\//, ""))

    function refresh() {
        if (!reader.running) {
            const collapsed = ["enabled", "disabled"].filter(function (section) {
                return root.sectionCollapsed(section);
            }).join(",");
            reader.command = ["python3", root.backend, "view", root.tab, root.query, String(root.pageIndex), root.origin, collapsed];
            reader.running = true;
        } else
            debounce.restart();
    }
    function selectTab(value) {
        root.tab = value;
        root.pageIndex = 0;
        root.pending = null;
        root.positioningId = "";
        refresh();
    }
    function selectOrigin(value) {
        root.origin = value;
        root.pageIndex = 0;
        root.pending = null;
        root.positioningId = "";
        refresh();
    }
    function sectionCollapsed(section) {
        return root.collapsedSections[root.origin + "." + section] === true;
    }
    function toggleSection(section) {
        const state = Object.assign({}, root.collapsedSections);
        state[root.origin + "." + section] = !root.sectionCollapsed(section);
        root.collapsedSections = state;
        root.pageIndex = 0;
        root.pending = null;
        root.positioningId = "";
        refresh();
    }
    function action(verb, row) {
        if (busy)
            return;
        if (verb === "install" && root.enableAfterInstall)
            verb = "install-enable";
        if (verb === "enable" || verb === "disable") {
            if (verb === "enable" && row.kinds.indexOf("bar") !== -1)
                pending = {
                    verb: verb,
                    row: Object.assign({}, row)
                };
            else
                execute(verb, row.id);
        } else
            pending = {
                verb: verb,
                row: Object.assign({}, row)
            };
    }
    function showPosition(row) {
        if (root.positioningId === row.id) {
            root.positioningId = "";
            return;
        }
        root.positionSection = "right";
        const layout = root.bar ? root.bar.layoutConfig : {};
        const sections = ["left", "centerBegin", "center", "centerEnd", "right", "tray"];
        for (let i = 0; i < sections.length; i++) {
            const entries = layout[sections[i]] || [];
            for (let j = 0; j < entries.length; j++) {
                if ((typeof entries[j] === "string" ? entries[j] : entries[j].id) === row.id)
                    root.positionSection = sections[i];
            }
        }
        root.positioningId = row.id;
        root.pending = null;
    }
    function toggleEnableAfterInstall() {
        if (!root.bar || !root.bar.shell || root.busy)
            return;
        const entry = Object.assign({}, root.settings, {
            id: root.moduleName,
            enableAfterInstall: !root.enableAfterInstall
        });
        root.settings = entry;
        if (root.hostWidget)
            root.hostWidget.settings = entry;
        root.pending = null;
        root.bar.shell.updateEntryInline(root.moduleName, entry);
    }
    function execute(verb, id, repository) {
        if (busy)
            return;
        root.errorText = "";
        root.launching = true;
        root.launchAction = verb.indexOf("move-") === 0 ? "move" : verb;
        root.launchId = id || "";
        root.pending = null;
        root.positioningId = "";
        launcher.command = ["python3", root.backend, "start", verb, id || "", repository || ""];
        launcher.running = true;
    }
    function openSource(row) {
        if (/^https:\/\/[A-Za-z0-9.-]+\/[A-Za-z0-9_.\/-]+$/.test(row.repo || ""))
            Qt.openUrlExternally(row.repo);
    }
    function confirmText() {
        if (!pending)
            return "";
        return strings.t("confirm." + pending.verb).arg(pending.row.name);
    }
    function dateLabel(stamp) {
        return new Date(stamp * 1000).toLocaleString(Qt.locale(Commons.I18n.formatLocale), "ddd d MMM HH:mm");
    }

    Commons.Translations {
        id: strings
        directory: Qt.resolvedUrl("i18n")
    }
    Component.onCompleted: refresh()
    onOpenedChanged: if (opened)
        refresh()
    Timer {
        id: debounce
        interval: 220
        onTriggered: root.refresh()
    }
    Timer {
        interval: 2500
        repeat: true
        running: root.opened || root.busy
        onTriggered: root.refresh()
    }

    Process {
        id: reader
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    const data = JSON.parse(text);
                    if (data.error)
                        root.errorText = data.error;
                    else {
                        root.errorText = "";
                        if (JSON.stringify(data.rows) === JSON.stringify(root.snapshot.rows))
                            data.rows = root.snapshot.rows;
                        root.snapshot = data;
                    }
                } catch (e) {
                    root.errorText = strings.t("readError");
                }
            }
        }
    }
    Process {
        id: launcher
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    const data = JSON.parse(text);
                    if (data.error)
                        root.errorText = data.error;
                } catch (e) {
                    root.errorText = strings.t("readError");
                }
            }
        }
        onExited: {
            root.launching = false;
            root.refresh();
        }
    }

    KeyboardPanel {
        id: panel
        anchorItem: root.anchorItem
        owner: root.hostWidget || root
        bar: root.bar
        open: root.opened
        focusTarget: panelContent
        padding: Style.space(10)
        contentWidth: panel.fittedContentWidth(Style.space(500))
        contentHeight: panel.fittedContentHeight(panelContent.implicitHeight)

        Column {
            id: panelContent
            width: parent.width
            spacing: Style.space(8)
            focus: true
            Keys.onEscapePressed: {
                if (root.pending)
                    root.pending = null;
                else
                    root.close();
            }

            Row {
                width: parent.width
                spacing: Style.space(8)
                PlainText {
                    width: parent.width - closeButton.width - parent.spacing
                    text: strings.t("title")
                    font.pixelSize: Style.font.body
                    font.bold: true
                    anchors.verticalCenter: parent.verticalCenter
                }
                CompactButton {
                    id: closeButton
                    text: "×"
                    focusable: true
                    onClicked: root.close()
                }
            }
            Row {
                width: parent.width
                spacing: Style.space(6)
                CompactButton {
                    text: strings.t("installed")
                    selected: root.tab === "installed"
                    focusable: true
                    onClicked: root.selectTab("installed")
                }
                CompactButton {
                    text: strings.t("marketplace")
                    selected: root.tab === "marketplace"
                    focusable: true
                    onClicked: root.selectTab("marketplace")
                }
                CompactButton {
                    text: strings.t("updates") + (root.snapshot.updates ? " · " + Commons.I18n.formatNumber(root.snapshot.updates) : "")
                    selected: root.tab === "updates"
                    focusable: true
                    onClicked: root.selectTab("updates")
                }
            }
            PlainText {
                id: updateDisclaimer
                width: parent.width
                visible: root.tab === "updates" || root.tab === "marketplace"
                text: strings.t(root.tab === "marketplace" ? "marketplaceDisclaimer" : "updateDisclaimer")
                font.pixelSize: Style.font.bodySmall
                opacity: 0.8
            }
            Row {
                width: parent.width
                spacing: Style.space(8)
                TextField {
                    id: searchField
                    width: parent.width - (clearSearch.visible ? clearSearch.width + parent.spacing : 0)
                    placeholderText: strings.t("search")
                    implicitHeight: Style.space(30)
                    Accessible.name: strings.t("searchLabel")
                    onTextChanged: {
                        root.query = text;
                        root.pageIndex = 0;
                        debounce.restart();
                    }
                    Keys.onEscapePressed: {
                        root.pending = null;
                        root.close();
                    }
                }
                CompactButton {
                    id: clearSearch
                    visible: searchField.text.length > 0
                    text: strings.t("clearSearch")
                    enabled: searchField.text.length > 0
                    focusable: true
                    onClicked: {
                        searchField.clear();
                        searchField.forceActiveFocus();
                    }
                }
            }
            Flow {
                width: parent.width
                spacing: Style.space(6)
                CompactButton {
                    visible: root.tab === "installed"
                    text: strings.t("official") + " · " + Commons.I18n.formatNumber(root.snapshot.officialCount || 0)
                    selected: root.origin === "official"
                    focusable: true
                    onClicked: root.selectOrigin("official")
                }
                CompactButton {
                    visible: root.tab === "installed"
                    text: strings.t("thirdParty") + " · " + Commons.I18n.formatNumber(root.snapshot.thirdPartyCount || 0)
                    selected: root.origin === "third-party"
                    focusable: true
                    onClicked: root.selectOrigin("third-party")
                }
                CompactButton {
                    text: strings.t(root.tab === "marketplace" ? "refreshCatalog" : "checkUpdates")
                    focusable: true
                    enabled: !root.busy
                    onClicked: root.execute(root.tab === "marketplace" ? "catalog" : "check", "")
                }
                CompactButton {
                    visible: root.tab === "marketplace"
                    text: strings.t("verifyReleases")
                    enabled: !root.busy && root.snapshot.rows.some(function (row) {
                        return row.releaseCheckable === true;
                    })
                    onClicked: {
                        const ids = root.snapshot.rows.filter(function (row) {
                            return row.releaseCheckable === true;
                        }).map(function (row) {
                            return row.id;
                        });
                        root.execute("releases", ids.join(","));
                    }
                }
            }
            Row {
                width: parent.width
                visible: root.tab === "marketplace"
                spacing: Style.space(8)
                PlainText {
                    width: parent.width - autoEnableSwitch.width - parent.spacing
                    text: strings.t("enableAfterInstall")
                    font.pixelSize: Style.font.bodySmall
                    anchors.verticalCenter: parent.verticalCenter
                }
                ToggleSwitch {
                    id: autoEnableSwitch
                    checked: root.enableAfterInstall
                    enabled: !root.busy
                    activeFocusOnTab: true
                    hasCursor: activeFocus
                    Accessible.name: strings.t("enableAfterInstall")
                    onToggled: root.toggleEnableAfterInstall()
                    Keys.onSpacePressed: root.toggleEnableAfterInstall()
                    Keys.onReturnPressed: root.toggleEnableAfterInstall()
                }
            }
            PlainText {
                width: parent.width
                visible: root.busy || root.errorText !== "" || root.snapshot.job.ok === false || (root.snapshot.job.ok === true && Date.now() - root.snapshot.job.at * 1000 < 15000)
                text: root.busy ? strings.t("working." + (root.launching ? root.launchAction : root.snapshot.job.action)) : (root.errorText || root.snapshot.job.error || strings.t("done." + root.snapshot.job.action))
                font.pixelSize: Style.font.bodySmall
                maximumLineCount: 4
                elide: Text.ElideRight
            }
            Rectangle {
                width: parent.width
                height: confirmation.implicitHeight + Style.space(20)
                color: Commons.Color.background
                border.color: Commons.Color.accent
                visible: root.pending !== null
                Column {
                    id: confirmation
                    anchors {
                        left: parent.left
                        right: parent.right
                        top: parent.top
                        margins: Style.space(7)
                    }
                    spacing: Style.space(8)
                    PlainText {
                        width: parent.width
                        text: root.confirmText()
                    }
                    PlainText {
                        width: parent.width
                        text: root.pending ? root.pending.row.repo || root.pending.row.id : ""
                        opacity: 0.65
                        font.pixelSize: Style.font.bodySmall
                    }
                    Row {
                        spacing: Style.space(8)
                        CompactButton {
                            text: strings.t("cancel")
                            focusable: true
                            onClicked: root.pending = null
                        }
                        CompactButton {
                            text: strings.t("confirm")
                            focusable: true
                            enabled: !root.busy
                            onClicked: if (root.pending)
                                root.execute(root.pending.verb, root.pending.row.id, root.pending.row.repo)
                        }
                    }
                }
            }
            ListView {
                id: list
                width: parent.width
                height: Math.max(Style.space(90), Math.min(Style.space(280), Math.max(Style.space(90), contentHeight), panel.availableCardHeight - Style.space(300) - (updateDisclaimer.visible ? updateDisclaimer.implicitHeight : 0) - (root.pending ? confirmation.implicitHeight + Style.space(20) : 0)))
                clip: true
                spacing: Style.space(6)
                model: root.snapshot.rows
                boundsBehavior: Flickable.StopAtBounds
                onModelChanged: positionViewAtBeginning()
                delegate: Column {
                    id: entry
                    required property var modelData
                    width: list.width
                    spacing: Style.space(8)
                    CompactButton {
                        width: parent.width
                        visible: entry.modelData.sectionStart === true
                        text: strings.t(entry.modelData.stateSection || "enabled") + " · " + Commons.I18n.formatNumber(entry.modelData.sectionCount || 0)
                        iconText: root.sectionCollapsed(entry.modelData.stateSection) ? "\uf054" : "\uf078"
                        leftAlign: true
                        focusable: true
                        foreground: Commons.Color.accent
                        onClicked: root.toggleSection(entry.modelData.stateSection)
                    }
                    Rectangle {
                        visible: entry.modelData.sectionOnly !== true
                        width: parent.width
                        height: details.implicitHeight + Style.space(14)
                        color: Qt.rgba(Commons.Color.foreground.r, Commons.Color.foreground.g, Commons.Color.foreground.b, 0.035)
                        Column {
                            id: details
                            anchors {
                                left: parent.left
                                right: parent.right
                                top: parent.top
                                margins: Style.space(7)
                            }
                            spacing: Style.space(4)
                            Row {
                                width: parent.width
                                spacing: Style.space(8)
                                PlainText {
                                    width: parent.width - version.width - parent.spacing
                                    text: entry.modelData.name
                                    font.bold: true
                                    maximumLineCount: 1
                                    elide: Text.ElideRight
                                }
                                PlainText {
                                    id: version
                                    text: entry.modelData.version || ""
                                    opacity: 0.55
                                    font.pixelSize: Style.font.bodySmall
                                }
                            }
                            Rectangle {
                                visible: entry.modelData.verifiedMatch === true
                                width: parent.width
                                height: verifiedLabel.implicitHeight + Style.space(8)
                                radius: Style.cornerRadius
                                color: "#302913"
                                border.color: "#b58b2a"
                                border.width: 1
                                PlainText {
                                    id: verifiedLabel
                                    anchors.centerIn: parent
                                    width: parent.width - Style.space(12)
                                    text: strings.t("verifiedRelease").arg(entry.modelData.verifiedReleaseTag || "")
                                    wrapMode: Text.WrapAnywhere
                                    maximumLineCount: 2
                                    elide: Text.ElideRight
                                    color: "#f4cd68"
                                    font.pixelSize: Style.font.bodySmall
                                }
                            }
                            PlainText {
                                width: parent.width
                                text: entry.modelData.description || strings.t("noDescription")
                                opacity: 0.65
                                font.pixelSize: Style.font.bodySmall
                                maximumLineCount: 2
                                elide: Text.ElideRight
                            }
                            PlainText {
                                width: parent.width
                                visible: root.tab !== "marketplace" && ["available", "ahead", "error"].indexOf(entry.modelData.updateState) !== -1
                                text: (entry.modelData.firstParty ? strings.t("builtin") : strings.t(entry.modelData.enabled ? "enabled" : "disabled")) + (entry.modelData.updateState === "available" ? " · " + strings.t("available") : "") + (entry.modelData.updateState === "current" ? " · " + strings.t("current") : "") + (entry.modelData.updateState === "ahead" ? " · " + strings.t("ahead") : "") + (entry.modelData.updateState === "error" ? " · " + strings.t("checkFailed") : "")
                                color: entry.modelData.updateState === "available" ? Commons.Color.accent : Commons.Color.foreground
                                opacity: 0.8
                                font.pixelSize: Style.font.bodySmall
                            }
                            PlainText {
                                width: parent.width
                                visible: entry.modelData.updateState === "error"
                                text: entry.modelData.updateError || ""
                                font.pixelSize: Style.font.bodySmall
                                maximumLineCount: 2
                                elide: Text.ElideRight
                            }
                            Flow {
                                width: parent.width
                                spacing: Style.space(6)
                                CompactButton {
                                    visible: root.tab === "marketplace"
                                    text: strings.t(entry.modelData.installed ? "installed" : entry.modelData.installable ? "install" : "manualSetup")
                                    enabled: !root.busy && entry.modelData.installable === true && !entry.modelData.installed
                                    focusable: true
                                    onClicked: root.action("install", entry.modelData)
                                }
                                CompactButton {
                                    visible: root.tab !== "marketplace" && (!entry.modelData.enabled || entry.modelData.canDisable === true)
                                    text: strings.t(entry.modelData.enabled ? "disable" : "enable")
                                    enabled: !root.busy && entry.modelData.manageable === true && (!entry.modelData.enabled || entry.modelData.canDisable === true)
                                    focusable: true
                                    onClicked: root.action(entry.modelData.enabled ? "disable" : "enable", entry.modelData)
                                }
                                PlainText {
                                    visible: root.tab !== "marketplace" && entry.modelData.firstParty === true && entry.modelData.enabled === true && entry.modelData.canDisable !== true
                                    text: strings.t("required")
                                    font.pixelSize: Style.font.bodySmall
                                    opacity: 0.6
                                    topPadding: Style.space(4)
                                }
                                CompactButton {
                                    visible: root.tab === "installed" && entry.modelData.positionable === true
                                    text: strings.t("position")
                                    enabled: !root.busy
                                    selected: root.positioningId === entry.modelData.id
                                    onClicked: root.showPosition(entry.modelData)
                                }
                                CompactButton {
                                    visible: root.tab !== "marketplace" && entry.modelData.updatable === true && entry.modelData.updateState === "available"
                                    text: strings.t("update")
                                    enabled: !root.busy
                                    focusable: true
                                    onClicked: root.action("update", entry.modelData)
                                }
                                CompactButton {
                                    visible: root.tab !== "marketplace" && entry.modelData.firstParty !== true && entry.modelData.manageable === true
                                    text: strings.t("remove")
                                    enabled: !root.busy
                                    focusable: true
                                    onClicked: root.action("remove", entry.modelData)
                                }
                                CompactButton {
                                    visible: !!entry.modelData.repo
                                    text: strings.t("source")
                                    focusable: true
                                    onClicked: root.openSource(entry.modelData)
                                }
                            }
                            Row {
                                width: parent.width
                                visible: root.tab === "installed" && entry.modelData.positionable === true && root.positioningId === entry.modelData.id
                                spacing: Style.space(6)
                                Dropdown {
                                    width: parent.width - placeButton.width - parent.spacing
                                    showLabel: false
                                    label: strings.t("position")
                                    rowHeight: Style.space(28)
                                    value: root.positionSection
                                    enabled: !root.busy
                                    options: [
                                        {
                                            value: "left",
                                            label: strings.t("position.left")
                                        },
                                        {
                                            value: "centerBegin",
                                            label: strings.t("position.centerBegin")
                                        },
                                        {
                                            value: "center",
                                            label: strings.t("position.center")
                                        },
                                        {
                                            value: "centerEnd",
                                            label: strings.t("position.centerEnd")
                                        },
                                        {
                                            value: "right",
                                            label: strings.t("position.right")
                                        },
                                        {
                                            value: "tray",
                                            label: strings.t("position.tray")
                                        }
                                    ]
                                    onChanged: function (value) {
                                        root.positionSection = value;
                                    }
                                }
                                CompactButton {
                                    id: placeButton
                                    text: strings.t(entry.modelData.enabled ? "applyPosition" : "enableHere")
                                    enabled: !root.busy
                                    onClicked: root.execute("move-" + root.positionSection, entry.modelData.id)
                                }
                            }
                        }
                    }
                }
                PlainText {
                    anchors.centerIn: parent
                    width: parent.width - Style.space(20)
                    horizontalAlignment: Text.AlignHCenter
                    visible: root.snapshot.rows.length === 0
                    text: root.tab === "marketplace" && !root.snapshot.catalogAt ? strings.t("fetchFirst") : root.tab === "updates" && !root.snapshot.checkedAt ? strings.t("checkFirst") : strings.t("empty")
                    opacity: 0.6
                }
            }
            Row {
                width: parent.width
                spacing: Style.space(8)
                CompactButton {
                    text: "‹"
                    enabled: root.snapshot.page > 0
                    focusable: true
                    onClicked: {
                        root.pageIndex = root.snapshot.page - 1;
                        root.refresh();
                    }
                }
                PlainText {
                    width: parent.width - previousWidth.width * 2 - parent.spacing * 2
                    text: strings.tn("page", root.snapshot.total).arg(Commons.I18n.formatNumber(root.snapshot.page + 1)).arg(Commons.I18n.formatNumber(root.snapshot.pages || 1)).arg(Commons.I18n.formatNumber(root.snapshot.total))
                    horizontalAlignment: Text.AlignHCenter
                    anchors.verticalCenter: parent.verticalCenter
                    opacity: 0.65
                    font.pixelSize: Style.font.bodySmall
                }
                CompactButton {
                    id: previousWidth
                    text: "›"
                    enabled: root.snapshot.page + 1 < (root.snapshot.pages || 1)
                    focusable: true
                    onClicked: {
                        root.pageIndex = root.snapshot.page + 1;
                        root.refresh();
                    }
                }
            }
            PlainText {
                width: parent.width
                text: root.snapshot.catalogAt ? strings.t("catalogDate").arg(root.dateLabel(root.snapshot.catalogAt)) : strings.t("offlineHint")
                opacity: 0.45
                font.pixelSize: Style.font.bodySmall
            }
        }
    }
}
