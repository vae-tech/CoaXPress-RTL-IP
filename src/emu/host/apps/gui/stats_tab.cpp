#include "stats_tab.h"

#include <QCheckBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QPushButton>
#include <QScrollBar>
#include <QSpinBox>
#include <QTimer>
#include <QTreeWidget>
#include <QVBoxLayout>

StatsTab::StatsTab(QWidget* parent) : QWidget(parent) {
    timer_ = new QTimer(this);
    timer_->setInterval(1000);
    connect(timer_, &QTimer::timeout, this, &StatsTab::refresh);

    auto* lay = new QVBoxLayout(this);
    auto* bar = new QHBoxLayout();
    label_ = new QLabel("(no device selected)");
    label_->setStyleSheet("color: gray;");
    bar->addWidget(label_);
    bar->addStretch(1);

    auto_chk_ = new QCheckBox("Auto-refresh");
    auto_chk_->setChecked(true);
    connect(auto_chk_, &QCheckBox::toggled, this, [this](bool) { applyTimerState(); });
    bar->addWidget(auto_chk_);

    interval_sb_ = new QSpinBox();
    interval_sb_->setRange(1, 60);
    interval_sb_->setValue(1);
    interval_sb_->setSuffix(" s");
    connect(interval_sb_, QOverload<int>::of(&QSpinBox::valueChanged), this,
            [this](int secs) { timer_->setInterval(std::max(1, secs) * 1000); });
    bar->addWidget(interval_sb_);

    auto* refresh_btn = new QPushButton("Refresh now");
    connect(refresh_btn, &QPushButton::clicked, this, &StatsTab::refresh);
    bar->addWidget(refresh_btn);
    lay->addLayout(bar);

    tv_ = new QTreeWidget();
    tv_->setHeaderLabels({"Statistic", "Value"});
    tv_->setColumnWidth(0, 320);
    tv_->setColumnWidth(1, 260);
    tv_->setUniformRowHeights(true);
    lay->addWidget(tv_, 1);
}

void StatsTab::setDevice(DevicePtr dev) {
    dev_ = std::move(dev);
    refresh();
}

void StatsTab::setActive(bool on) {
    active_ = on;
    if (on) {
        refresh();
        applyTimerState();
    } else {
        timer_->stop();
    }
}

void StatsTab::applyTimerState() {
    if (active_ && auto_chk_->isChecked()) {
        timer_->start();
    } else {
        timer_->stop();
    }
}

void StatsTab::addNode(QTreeWidgetItem* parent, const StatNode& node) {
    auto* item = new QTreeWidgetItem(QStringList{node.name, node.value});
    if (parent) {
        parent->addChild(item);
    } else {
        tv_->addTopLevelItem(item);
    }
    for (const auto& k : node.kids) addNode(item, k);
}

void StatsTab::refresh() {
    if (!dev_) {
        label_->setText("(no device selected)");
    } else if (!dev_->stats) {
        label_->setText(dev_->label + ": not connected");
    } else {
        label_->setText(dev_->label + ": live counters");
    }
    // The hierarchy never changes shape, so a full rebuild + expandAll keeps
    // the view stable; only the scroll position needs carrying over.
    const int scroll = tv_->verticalScrollBar()->value();
    tv_->setUpdatesEnabled(false);
    tv_->clear();
    if (dev_ && dev_->stats) {
        for (const auto& node : dev_->stats->tree()) addNode(nullptr, node);
        tv_->expandAll();
    }
    tv_->setUpdatesEnabled(true);
    tv_->verticalScrollBar()->setValue(scroll);
}
