// Statistics tab: live counter tree, refreshed on a timer while visible
// (cxp/gui/stats_tab.py).
#pragma once

#include <QWidget>

#include "device_model.h"

class QCheckBox;
class QLabel;
class QSpinBox;
class QTimer;
class QTreeWidget;
class QTreeWidgetItem;

class StatsTab : public QWidget {
    Q_OBJECT

public:
    explicit StatsTab(QWidget* parent = nullptr);

public slots:
    void setDevice(DevicePtr dev);
    // Tab-visibility hook: only spin the timer while the user is looking.
    void setActive(bool on);
    void refresh();

private:
    void applyTimerState();
    void addNode(QTreeWidgetItem* parent, const StatNode& node);

    DevicePtr dev_;
    bool active_ = false;
    QTimer* timer_ = nullptr;
    QLabel* label_ = nullptr;
    QCheckBox* auto_chk_ = nullptr;
    QSpinBox* interval_sb_ = nullptr;
    QTreeWidget* tv_ = nullptr;
};
