// Image tab: live view of the most recent received frame
// (cxp/gui/image_view.py).
//
// Two frame sources share one painter:
//
// * Video stream — frames the StreamParser reassembles from the CXP §9.4
//   in-band stream (Table 38 header + pixel payload).
// * Link test — CXP §8.7 connection-test packets (TYPE 0x04).  The payload
//   is a deterministic 0x00..0xFF counter ramp, so a clean gradient means a
//   good link; any word that mismatched the ramp is painted red.
//
// The tab polls the device statistics on a timer and only re-renders when
// the relevant sequence counter advances.
#pragma once

#include <QImage>
#include <QWidget>

#include "device_model.h"

class QComboBox;
class QLabel;
class QPushButton;
class QTimer;

// Render a §8.7 connection-test payload as a counter-ramp image (256 bytes
// per row, erroneous words painted red).  Null image for an empty payload.
QImage linktestToQImage(const cxp::Words& data_words, const std::vector<uint32_t>& error_indices);

// Canvas that paints a QImage scaled to fit, aspect-correct, nearest-neighbour
// (a crisp pixel grid beats a smoothed picture in a debug viewer).
class ImageView : public QWidget {
    Q_OBJECT

public:
    explicit ImageView(QWidget* parent = nullptr);
    void setImage(const QImage& img);
    void setPlaceholder(const QString& text);
    void clear();

protected:
    void paintEvent(QPaintEvent* ev) override;

private:
    QImage img_;
    QString placeholder_ = "no frame received yet";
};

class ImageTab : public QWidget {
    Q_OBJECT

public:
    explicit ImageTab(QWidget* parent = nullptr);

public slots:
    void setDevice(DevicePtr dev);
    void setActive(bool on);
    void refresh();

private slots:
    void onSourceChanged();
    void onClear();
    void onSave();

private:
    void resetView();
    void refreshVideo(LinkStats& stats);
    void refreshLinktest(LinkStats& stats);

    DevicePtr dev_;
    bool active_ = false;
    int64_t seen_video_ = -1;
    int64_t seen_linktest_ = -1;
    cxp::FramePtr cur_frame_;
    QImage cur_image_;

    QTimer* timer_ = nullptr;
    QComboBox* src_combo_ = nullptr;
    QLabel* info_ = nullptr;
    QPushButton* clear_btn_ = nullptr;
    QPushButton* save_btn_ = nullptr;
    ImageView* view_ = nullptr;
};
