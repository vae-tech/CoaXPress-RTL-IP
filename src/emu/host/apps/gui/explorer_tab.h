// Explorer: device table, SFNC register tree, per-feature editor
// (cxp/gui/explorer_tab.py).
#pragma once

#include <string>
#include <vector>

#include <QWidget>

#include "device_model.h"
#include "io_bridge.h"

class QComboBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QTableWidget;
class QTreeWidget;
class QTreeWidgetItem;

class ExplorerTab : public QWidget {
    Q_OBJECT

public:
    ExplorerTab(const QString& fifo_dir, IoBridge* bridge, QWidget* parent = nullptr);

    // Close every open session (blocking; call once at shutdown).
    void teardown();

public slots:
    void requestScan();

signals:
    // The *current* device changed (row selected, or cleared for a rescan).
    void deviceChanged(DevicePtr dev);
    // Status line; level is "", "ok" or "err".
    void log(const QString& msg, const QString& level);

private slots:
    void onDevSelect();
    void onFeatSelect();
    void onApply();
    void onRefresh();

private:
    QWidget* buildDevices();
    QWidget* buildRegisterPanel();
    QWidget* buildEditor();

    void scanDone(std::vector<DevicePtr> devs, const QString& err);
    void clearRegisterTree();
    void populateTree(const Device& dev);
    QStringList rowColumns(const cxp::Feature& feat) const;
    void refreshRow(const cxp::Feature* feat, const QString& value);
    void showEditor(const cxp::Feature* feat);
    QString editorValue() const;
    void reread(const cxp::Feature* feat);
    const cxp::Feature* itemFeature(QTreeWidgetItem* item) const;

    static QString fmt(const cxp::Feature& feat, const cxp::Value& v);
    static QString addrStr(const cxp::Feature& feat);

    QString fifo_dir_;
    IoBridge* bridge_;
    std::vector<DevicePtr> devices_;
    DevicePtr cur_;
    const cxp::Feature* sel_feat_ = nullptr;

    QPushButton* scan_btn_ = nullptr;
    QTableWidget* dev_tbl_ = nullptr;
    QTreeWidget* reg_tv_ = nullptr;
    QLabel* ed_name_ = nullptr;
    QLabel* ed_meta_ = nullptr;
    QLabel* ed_desc_ = nullptr;
    QLineEdit* ed_entry_ = nullptr;
    QComboBox* ed_combo_ = nullptr;
    QPushButton* ed_apply_ = nullptr;
    QPushButton* ed_refresh_ = nullptr;
};
