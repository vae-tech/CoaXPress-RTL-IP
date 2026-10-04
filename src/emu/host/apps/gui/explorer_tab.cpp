#include "explorer_tab.h"

#include <memory>
#include <set>

#include <QComboBox>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QSplitter>
#include <QTableWidget>
#include <QTreeWidget>
#include <QTreeWidgetItemIterator>
#include <QVBoxLayout>

using namespace cxp;

namespace {

const QStringList kDevCols = {"Vendor", "Model", "Revision", "Link", "Source"};
const QStringList kRegCols = {"Feature", "Value", "Type", "Access", "Address"};

// Python int(s, 0): 0x / 0o / 0b prefixes, optional sign, else decimal.
int64_t parseIntAuto(const QString& text) {
    QString s = text.trimmed().remove('_');
    bool neg = s.startsWith('-');
    if (neg || s.startsWith('+')) s = s.mid(1);
    int base = 10;
    if (s.startsWith("0x", Qt::CaseInsensitive)) base = 16;
    else if (s.startsWith("0o", Qt::CaseInsensitive)) base = 8;
    else if (s.startsWith("0b", Qt::CaseInsensitive)) base = 2;
    if (base != 10) s = s.mid(2);
    bool ok = false;
    qulonglong v = s.toULongLong(&ok, base);
    if (!ok || s.isEmpty()) {
        throw GenIcamError("invalid literal for int() with base 0: '" + text.toStdString() + "'");
    }
    return neg ? -static_cast<int64_t>(v) : static_cast<int64_t>(v);
}

}  // namespace

ExplorerTab::ExplorerTab(const QString& fifo_dir, IoBridge* bridge, QWidget* parent)
    : QWidget(parent), fifo_dir_(fifo_dir), bridge_(bridge) {
    auto* outer = new QVBoxLayout(this);
    outer->setContentsMargins(0, 0, 0, 0);

    auto* bar = new QHBoxLayout();
    scan_btn_ = new QPushButton("Scan devices");
    connect(scan_btn_, &QPushButton::clicked, this, &ExplorerTab::requestScan);
    bar->addWidget(scan_btn_);
    bar->addWidget(new QLabel("  FIFO dir: " + fifo_dir_));
    bar->addStretch(1);
    outer->addLayout(bar);

    auto* splitter = new QSplitter(Qt::Vertical, this);
    splitter->addWidget(buildDevices());
    splitter->addWidget(buildRegisterPanel());
    splitter->setStretchFactor(0, 1);
    splitter->setStretchFactor(1, 3);
    outer->addWidget(splitter, 1);
}

// -- UI construction ----------------------------------------------------------------
QWidget* ExplorerTab::buildDevices() {
    auto* box = new QGroupBox("Devices");
    auto* lay = new QVBoxLayout(box);
    dev_tbl_ = new QTableWidget(0, kDevCols.size(), box);
    dev_tbl_->setHorizontalHeaderLabels(kDevCols);
    dev_tbl_->verticalHeader()->setVisible(false);
    dev_tbl_->setSelectionBehavior(QAbstractItemView::SelectRows);
    dev_tbl_->setSelectionMode(QAbstractItemView::SingleSelection);
    dev_tbl_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    dev_tbl_->horizontalHeader()->setStretchLastSection(true);
    connect(dev_tbl_, &QTableWidget::itemSelectionChanged, this, &ExplorerTab::onDevSelect);
    lay->addWidget(dev_tbl_);
    return box;
}

QWidget* ExplorerTab::buildRegisterPanel() {
    auto* split = new QSplitter(Qt::Horizontal);
    auto* reg_box = new QGroupBox("Register tree (SFNC / GenICam)");
    auto* rlay = new QVBoxLayout(reg_box);
    reg_tv_ = new QTreeWidget();
    reg_tv_->setHeaderLabels(kRegCols);
    reg_tv_->setColumnWidth(0, 260);
    reg_tv_->setColumnWidth(1, 200);
    reg_tv_->setColumnWidth(2, 100);
    reg_tv_->setColumnWidth(3, 70);
    reg_tv_->setRootIsDecorated(true);
    reg_tv_->setUniformRowHeights(true);
    connect(reg_tv_, &QTreeWidget::itemSelectionChanged, this, &ExplorerTab::onFeatSelect);
    rlay->addWidget(reg_tv_);
    split->addWidget(reg_box);
    split->addWidget(buildEditor());
    split->setStretchFactor(0, 3);
    split->setStretchFactor(1, 2);
    return split;
}

QWidget* ExplorerTab::buildEditor() {
    auto* box = new QGroupBox("Property");
    auto* lay = new QVBoxLayout(box);
    ed_name_ = new QLabel("(no selection)");
    QFont f = ed_name_->font();
    f.setBold(true);
    ed_name_->setFont(f);
    lay->addWidget(ed_name_);

    ed_meta_ = new QLabel("");
    ed_meta_->setStyleSheet("color: gray;");
    lay->addWidget(ed_meta_);

    ed_desc_ = new QLabel("");
    ed_desc_->setWordWrap(true);
    lay->addWidget(ed_desc_);

    ed_entry_ = new QLineEdit();
    ed_combo_ = new QComboBox();
    lay->addWidget(ed_entry_);
    lay->addWidget(ed_combo_);
    ed_entry_->hide();
    ed_combo_->hide();
    connect(ed_entry_, &QLineEdit::returnPressed, this, &ExplorerTab::onApply);

    auto* row = new QHBoxLayout();
    ed_apply_ = new QPushButton("Write");
    ed_apply_->setEnabled(false);
    connect(ed_apply_, &QPushButton::clicked, this, &ExplorerTab::onApply);
    ed_refresh_ = new QPushButton("Read");
    ed_refresh_->setEnabled(false);
    connect(ed_refresh_, &QPushButton::clicked, this, &ExplorerTab::onRefresh);
    row->addWidget(ed_apply_);
    row->addWidget(ed_refresh_);
    row->addStretch(1);
    lay->addLayout(row);
    lay->addStretch(1);
    return box;
}

// -- scanning -----------------------------------------------------------------------
void ExplorerTab::requestScan() {
    scan_btn_->setEnabled(false);
    scan_btn_->setText("Scanning…");
    // Nothing may touch the old sessions while the worker closes them.
    cur_.reset();
    emit deviceChanged(nullptr);
    clearRegisterTree();
    dev_tbl_->setRowCount(0);

    auto old = std::make_shared<std::vector<DevicePtr>>(std::move(devices_));
    devices_.clear();
    auto found = std::make_shared<std::vector<DevicePtr>>();
    const std::string fifo_dir = fifo_dir_.toStdString();
    bridge_->run(
        this,
        [old, found, fifo_dir]() {
            for (auto& d : *old) closeSession(*d);
            *found = candidates(fifo_dir);
            for (auto& d : *found) {
                try {
                    openSession(*d, PROBE_ACK_MS, PROBE_RETRIES);
                } catch (const std::exception& exc) {
                    d->error = exc.what();
                    closeSession(*d);
                }
            }
        },
        [this, found](const QString& err) { scanDone(std::move(*found), err); });
}

void ExplorerTab::scanDone(std::vector<DevicePtr> devs, const QString& err) {
    scan_btn_->setEnabled(true);
    scan_btn_->setText("Scan devices");
    if (!err.isEmpty()) {
        emit log("scan failed: " + err, "err");
        return;
    }
    devices_ = std::move(devs);
    int live = 0;
    dev_tbl_->setRowCount(static_cast<int>(devices_.size()));
    for (int i = 0; i < static_cast<int>(devices_.size()); ++i) {
        const Device& d = *devices_[i];
        QString src = d.kind == Device::Kind::Sim ? "in-process sim" : QString::fromStdString(d.h2c);
        QStringList cells;
        if (d.info) {
            cells << QString::fromStdString(d.info->vendor_name)
                  << QString::fromStdString(d.info->model_name)
                  << QString::asprintf("0x%08X", d.info->revision)
                  << QString::number(d.info->device_link_id) << src;
            ++live;
        } else {
            cells << d.label << "unreachable: " + QString::fromStdString(d.error).left(60)
                  << "—" << "—" << src;
        }
        for (int col = 0; col < cells.size(); ++col) {
            dev_tbl_->setItem(i, col, new QTableWidgetItem(cells[col]));
        }
    }
    emit log(QString("scan complete: %1/%2 device(s) reachable").arg(live).arg(devices_.size()),
             "ok");
}

void ExplorerTab::teardown() {
    cur_.reset();
    emit deviceChanged(nullptr);
    bridge_->waitForDone(3000);
    for (auto& d : devices_) {
        try {
            closeSession(*d);
        } catch (const std::exception&) {
        }
    }
}

// -- device / register tree -----------------------------------------------------------
void ExplorerTab::onDevSelect() {
    const auto rows = dev_tbl_->selectionModel()->selectedRows();
    if (rows.isEmpty()) return;
    int r = rows.first().row();
    if (r < 0 || r >= static_cast<int>(devices_.size())) return;
    DevicePtr dev = devices_[r];
    cur_ = dev;
    emit deviceChanged(dev);
    clearRegisterTree();
    if (!dev->info) {
        emit log(dev->label + ": not reachable (" + QString::fromStdString(dev->error) + ")", "err");
        return;
    }
    if (!dev->tree) {
        emit log(dev->label + ": connected, but no GenICam XML manifest on the device", "err");
        return;
    }
    populateTree(*dev);
    emit log(QString("%1: %2 %3 — %4 features (GenICam %5)")
                 .arg(dev->label, QString::fromStdString(dev->tree->vendor_name),
                      QString::fromStdString(dev->tree->model_name))
                 .arg(dev->tree->features().size())
                 .arg(QString::fromStdString(dev->tree->schema_version)),
             "ok");
}

void ExplorerTab::clearRegisterTree() {
    reg_tv_->clear();
    sel_feat_ = nullptr;
    showEditor(nullptr);
}

void ExplorerTab::populateTree(const Device& dev) {
    std::set<std::string> seen;
    std::function<void(const Feature*, QTreeWidgetItem*)> insert =
        [&](const Feature* feat, QTreeWidgetItem* parent) {
            if (!seen.insert(feat->name).second) return;
            auto* item = new QTreeWidgetItem(rowColumns(*feat));
            item->setData(0, Qt::UserRole, QVariant::fromValue<quintptr>(quintptr(feat)));
            if (parent) {
                parent->addChild(item);
            } else {
                reg_tv_->addTopLevelItem(item);
            }
            item->setExpanded(true);
            if (feat->kind == FeatureKind::Category) {
                for (const Feature* ch : feat->children) insert(ch, item);
            }
        };
    if (dev.tree->root()) insert(dev.tree->root(), nullptr);
}

QString ExplorerTab::addrStr(const Feature& feat) {
    return feat.reg ? QString::asprintf("%05llX", (unsigned long long)feat.reg->address) : QString();
}

QString ExplorerTab::fmt(const Feature& feat, const Value& v) {
    if (feat.kind == FeatureKind::Integer && v.type == Value::Type::Int && v.i >= 0) {
        return QString("%1  (0x%2)").arg(v.i).arg(QString::number(v.i, 16).toUpper());
    }
    return QString::fromStdString(v.str());
}

QStringList ExplorerTab::rowColumns(const Feature& feat) const {
    const QString kind = featureKindName(feat.kind);
    const QString access = QString::fromStdString(feat.effAccess());
    const QString name = QString::fromStdString(feat.name);
    const QString addr = addrStr(feat);
    if (feat.kind == FeatureKind::Category) return {name, "", kind, "", addr};
    if (feat.kind == FeatureKind::Command) return {name, "‹command›", kind, "W", addr};
    // Values come from the discovery sweep; the GUI thread never does register I/O.
    auto it = cur_ ? cur_->params.find(feat.name) : decltype(cur_->params)::const_iterator();
    if (!cur_ || it == cur_->params.end()) {
        if (feat.const_value) return {name, fmt(feat, *feat.const_value), kind, access, addr};
        return {name, "", kind, access, addr};
    }
    if (!it->second.ok()) {
        return {name, "!! " + QString::fromStdString(it->second.error), kind, access, addr};
    }
    return {name, fmt(feat, *it->second.value), kind, access, addr};
}

const Feature* ExplorerTab::itemFeature(QTreeWidgetItem* item) const {
    return item ? reinterpret_cast<const Feature*>(item->data(0, Qt::UserRole).value<quintptr>())
                : nullptr;
}

void ExplorerTab::refreshRow(const Feature* feat, const QString& value) {
    for (QTreeWidgetItemIterator it(reg_tv_); *it; ++it) {
        if (itemFeature(*it) == feat) {
            (*it)->setText(1, value);
            (*it)->setText(2, featureKindName(feat->kind));
            (*it)->setText(3, QString::fromStdString(feat->effAccess()));
            (*it)->setText(4, addrStr(*feat));
            return;
        }
    }
}

// -- editor ---------------------------------------------------------------------------
void ExplorerTab::onFeatSelect() {
    const auto items = reg_tv_->selectedItems();
    sel_feat_ = items.isEmpty() ? nullptr : itemFeature(items.first());
    showEditor(sel_feat_);
}

void ExplorerTab::showEditor(const Feature* feat) {
    ed_entry_->hide();
    ed_combo_->hide();
    if (!feat) {
        ed_name_->setText("(no selection)");
        ed_meta_->setText("");
        ed_desc_->setText("");
        ed_apply_->setEnabled(false);
        ed_refresh_->setEnabled(false);
        return;
    }
    ed_name_->setText(QString::fromStdString(feat->name));
    ed_meta_->setText(QString("%1 · %2").arg(featureKindName(feat->kind),
                                             QString::fromStdString(feat->effAccess())));
    ed_desc_->setText(QString::fromStdString(feat->description));
    if (feat->kind == FeatureKind::Category) {
        ed_apply_->setEnabled(false);
        ed_apply_->setText("Write");
        ed_refresh_->setEnabled(false);
        return;
    }
    if (feat->kind == FeatureKind::Command) {
        ed_entry_->setText("");
        ed_apply_->setEnabled(true);
        ed_apply_->setText("Execute");
        ed_refresh_->setEnabled(false);
        return;
    }
    ed_apply_->setText("Write");
    bool use_combo = false;
    if (feat->kind == FeatureKind::Enumeration) {
        ed_combo_->clear();
        for (const auto& kv : feat->enum_entries) ed_combo_->addItem(QString::fromStdString(kv.first));
        use_combo = true;
    } else if (feat->kind == FeatureKind::Boolean) {
        ed_combo_->clear();
        ed_combo_->addItems({"True", "False"});
        use_combo = true;
    }
    (use_combo ? static_cast<QWidget*>(ed_combo_) : ed_entry_)->show();

    const auto items = reg_tv_->selectedItems();
    QString cur = items.isEmpty() ? QString() : items.first()->text(1);
    if (feat->kind == FeatureKind::Integer && cur.contains('(')) {
        cur = cur.section('(', 0, 0).trimmed();
    }
    if (cur.startsWith("!!")) cur.clear();
    if (use_combo) {
        int idx = ed_combo_->findText(cur);
        ed_combo_->setCurrentIndex(idx >= 0 ? idx : 0);
    } else {
        ed_entry_->setText(cur);
    }
    ed_apply_->setEnabled(feat->isWritable());
    ed_refresh_->setEnabled(feat->isReadable());
}

QString ExplorerTab::editorValue() const {
    return (ed_entry_->isVisible() ? ed_entry_->text() : ed_combo_->currentText()).trimmed();
}

void ExplorerTab::onApply() {
    const Feature* feat = sel_feat_;
    if (!feat || !cur_ || !ed_apply_->isEnabled()) return;
    const QString fname = QString::fromStdString(feat->name);
    DevicePtr dev = cur_;  // keeps the tree alive until the job finishes
    if (feat->kind == FeatureKind::Command) {
        emit log("executing " + fname + " …", "");
        bridge_->run(this, [dev, feat] { feat->execute(); },
                     [this, fname](const QString& err) {
                         if (!err.isEmpty()) emit log(fname + ": write failed — " + err, "err");
                         else emit log(fname + ": write OK", "ok");
                     });
        return;
    }
    Value val;
    const QString s = editorValue();
    try {
        switch (feat->kind) {
        case FeatureKind::Integer: val = Value::ofInt(parseIntAuto(s)); break;
        case FeatureKind::Float: {
            bool ok = false;
            double d = s.toDouble(&ok);
            if (!ok) throw GenIcamError("could not convert string to float: '" + s.toStdString() + "'");
            val = Value::ofFloat(d);
            break;
        }
        case FeatureKind::Boolean: val = Value::ofBool(s == "True"); break;
        default: val = Value::ofString(s.toStdString()); break;
        }
    } catch (const std::exception& exc) {
        emit log(fname + ": bad input — " + exc.what(), "err");
        return;
    }
    emit log(QString("writing %1 = %2 …").arg(fname, QString::fromStdString(val.repr())), "");
    bridge_->run(this, [dev, feat, val] { feat->setValue(val); },
                 [this, fname, feat, dev](const QString& err) {
                     if (!err.isEmpty()) {
                         emit log(fname + ": write failed — " + err, "err");
                         return;
                     }
                     emit log(fname + ": write OK", "ok");
                     if (cur_ == dev) reread(feat);
                 });
}

void ExplorerTab::onRefresh() {
    if (sel_feat_) reread(sel_feat_);
}

void ExplorerTab::reread(const Feature* feat) {
    auto result = std::make_shared<Value>();
    DevicePtr dev = cur_;
    const QString fname = QString::fromStdString(feat->name);
    bridge_->run(this, [dev, feat, result] { *result = feat->getValue(); },
                 [this, dev, feat, fname, result](const QString& err) {
                     if (cur_ != dev) return;  // device switched / rescanned meanwhile
                     if (!err.isEmpty()) {
                         emit log(fname + ": re-read failed — " + err, "err");
                         return;
                     }
                     auto it = dev->params.find(feat->name);
                     if (it != dev->params.end()) it->second = {*result, {}};
                     refreshRow(feat, fmt(*feat, *result));
                     if (feat == sel_feat_) {
                         const QString text = QString::fromStdString(result->str());
                         if (ed_entry_->isVisible()) {
                             ed_entry_->setText(text);
                         } else {
                             int idx = ed_combo_->findText(text);
                             if (idx >= 0) ed_combo_->setCurrentIndex(idx);
                         }
                     }
                     emit log(fname + " = " + QString::fromStdString(result->repr()), "ok");
                 });
}
