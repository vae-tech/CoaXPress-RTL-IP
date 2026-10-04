#include "image_tab.h"

#include <set>

#include <QComboBox>
#include <QFileDialog>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QLabel>
#include <QMessageBox>
#include <QPainter>
#include <QPushButton>
#include <QTimer>
#include <QVBoxLayout>

#include "cxp_regmap.hpp"

using namespace cxp;

namespace {

// Link-test payloads are 0x00..0xFF repeated; one ramp per row.
constexpr int kLinktestPeriod = 256;
constexpr int kSrcVideo = 0;

}  // namespace

QImage linktestToQImage(const Words& data_words, const std::vector<uint32_t>& error_indices) {
    const int n = static_cast<int>(data_words.size() * 4);
    if (n == 0) return {};
    const int width = std::min(kLinktestPeriod, n);
    const int height = (n + width - 1) / width;
    std::set<uint32_t> bad(error_indices.begin(), error_indices.end());
    QImage img(width, height, QImage::Format_RGB888);
    img.fill(Qt::black);  // zero-padded last row
    for (int i = 0; i < n; ++i) {
        const uint32_t wi = static_cast<uint32_t>(i / 4);
        uchar* px = img.scanLine(i / width) + (i % width) * 3;
        if (bad.count(wi)) {
            px[0] = 0xFF;  // red marks a bad byte
            px[1] = px[2] = 0;
        } else {
            // P0 in the LSB lane: bytes laid out left-to-right in lane order.
            px[0] = px[1] = px[2] = static_cast<uchar>(data_words[wi] >> (8 * (i % 4)));
        }
    }
    return img;
}

// -- ImageView ----------------------------------------------------------------------
ImageView::ImageView(QWidget* parent) : QWidget(parent) {
    setMinimumSize(240, 180);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
}

void ImageView::setImage(const QImage& img) {
    img_ = img;
    update();
}

void ImageView::setPlaceholder(const QString& text) {
    placeholder_ = text;
    if (img_.isNull()) update();
}

void ImageView::clear() {
    img_ = QImage();
    update();
}

void ImageView::paintEvent(QPaintEvent*) {
    QPainter p(this);
    p.fillRect(rect(), QColor("#1e1e1e"));
    if (img_.isNull()) {
        p.setPen(QColor("#888888"));
        p.drawText(rect(), Qt::AlignCenter, placeholder_);
        return;
    }
    const QSize scaled = img_.size().scaled(size(), Qt::KeepAspectRatio);
    const QRect target(QPoint((width() - scaled.width()) / 2, (height() - scaled.height()) / 2),
                       scaled);
    p.setRenderHint(QPainter::SmoothPixmapTransform, false);
    p.drawImage(target, img_);
}

// -- ImageTab -----------------------------------------------------------------------
ImageTab::ImageTab(QWidget* parent) : QWidget(parent) {
    timer_ = new QTimer(this);
    timer_->setInterval(200);
    connect(timer_, &QTimer::timeout, this, &ImageTab::refresh);

    auto* lay = new QVBoxLayout(this);
    auto* bar = new QHBoxLayout();
    bar->addWidget(new QLabel("Source:"));
    src_combo_ = new QComboBox();
    src_combo_->addItems({"Video stream", "Link test"});
    connect(src_combo_, QOverload<int>::of(&QComboBox::currentIndexChanged), this,
            &ImageTab::onSourceChanged);
    bar->addWidget(src_combo_);
    bar->addSpacing(12);
    info_ = new QLabel("(no device selected)");
    info_->setStyleSheet("color: gray;");
    bar->addWidget(info_);
    bar->addStretch(1);
    clear_btn_ = new QPushButton("Clear buffer");
    clear_btn_->setToolTip("Drop the buffered frame and flush any frames still queued on the link");
    clear_btn_->setEnabled(false);
    connect(clear_btn_, &QPushButton::clicked, this, &ImageTab::onClear);
    bar->addWidget(clear_btn_);
    save_btn_ = new QPushButton("Save…");
    save_btn_->setToolTip("Save the displayed image as a PNG file");
    save_btn_->setEnabled(false);
    connect(save_btn_, &QPushButton::clicked, this, &ImageTab::onSave);
    bar->addWidget(save_btn_);
    lay->addLayout(bar);

    view_ = new ImageView();
    lay->addWidget(view_, 1);
}

void ImageTab::resetView() {
    seen_video_ = -1;
    seen_linktest_ = -1;
    cur_frame_.reset();
    cur_image_ = QImage();
    view_->clear();
}

void ImageTab::setDevice(DevicePtr dev) {
    dev_ = std::move(dev);
    resetView();
    refresh();
}

void ImageTab::setActive(bool on) {
    active_ = on;
    if (on) {
        refresh();
        timer_->start();
    } else {
        timer_->stop();
    }
}

void ImageTab::onSourceChanged() {
    resetView();
    refresh();
}

void ImageTab::refresh() {
    if (!dev_ || !dev_->stats) {
        info_->setText(dev_ ? dev_->label + ": not connected" : "(no device selected)");
        save_btn_->setEnabled(false);
        clear_btn_->setEnabled(false);
        view_->setPlaceholder("no device connected");
        return;
    }
    clear_btn_->setEnabled(true);
    if (src_combo_->currentIndex() == kSrcVideo) {
        refreshVideo(*dev_->stats);
    } else {
        refreshLinktest(*dev_->stats);
    }
}

void ImageTab::refreshVideo(LinkStats& stats) {
    view_->setPlaceholder("no video frame received yet");
    const uint64_t seq = stats.videoSeq();
    if (seq == 0) {
        info_->setText("waiting for video frames…");
        save_btn_->setEnabled(false);
        return;
    }
    if (static_cast<int64_t>(seq) == seen_video_) return;
    seen_video_ = static_cast<int64_t>(seq);
    FramePtr frame = stats.lastVideo();
    if (!frame) return;
    cur_frame_ = frame;
    cur_image_ = frameToQImage(*frame);
    view_->setImage(cur_image_);
    save_btn_->setEnabled(!cur_image_.isNull());
    const ImageHeader& hdr = frame->header;
    const QString geom = (hdr.width && hdr.height)
                             ? QString("%1×%2").arg(hdr.width).arg(hdr.height)
                             : QString("unknown size");
    info_->setText(QString("frame #%1 · %2 · %3 · %4  (%5 received)")
                       .arg(hdr.frame_id)
                       .arg(geom, pixelFormatName(hdr.pixel_format),
                            frame->complete() ? "complete" : "INCOMPLETE")
                       .arg(seq));
}

void ImageTab::refreshLinktest(LinkStats& stats) {
    view_->setPlaceholder(QString::asprintf("no link-test packets received yet\n"
                                            "(device must be in TestMode — bootstrap 0x%04X)",
                                            cxp::reg::TEST_MODE));
    const uint64_t seq = stats.linktestSeq();
    if (seq == 0) {
        info_->setText("waiting for link-test packets…");
        save_btn_->setEnabled(false);
        return;
    }
    if (static_cast<int64_t>(seq) == seen_linktest_) return;
    seen_linktest_ = static_cast<int64_t>(seq);
    auto snap = stats.lastLinktest();
    if (!snap) return;
    cur_frame_.reset();
    cur_image_ = linktestToQImage(snap->data_words, snap->error_indices);
    view_->setImage(cur_image_);
    save_btn_->setEnabled(!cur_image_.isNull());
    const size_t n_err = snap->error_indices.size();
    const QString verdict = n_err == 0 ? QString("clean") : QString("%1 word error(s)").arg(n_err);
    info_->setText(QString("link test · %1 words · %2  (%3 received)")
                       .arg(snap->data_words.size())
                       .arg(verdict)
                       .arg(seq));
}

// Drop the buffered frame and flush stream frames still queued on the link.
// Session statistics counters are deliberately left untouched.
void ImageTab::onClear() {
    if (!dev_ || !dev_->stats) return;
    const size_t drained = dev_->stats->clearFrames();
    resetView();
    save_btn_->setEnabled(false);
    info_->setText(QString("buffer cleared — %1 queued frame(s) dropped").arg(drained));
}

void ImageTab::onSave() {
    if (cur_image_.isNull()) return;
    const QString def = cur_frame_ ? QString("frame_%1.png").arg(cur_frame_->header.frame_id)
                                   : QString("linktest.png");
    QString path = QFileDialog::getSaveFileName(
        this, "Save image as PNG", def,
        "PNG image (*.png);;BMP image (*.bmp);;JPEG image (*.jpg)");
    if (path.isEmpty()) return;
    if (QFileInfo(path).suffix().isEmpty()) path += ".png";  // default to PNG
    if (cur_image_.save(path)) {
        info_->setText("saved " + path);
    } else {
        QMessageBox::warning(this, "Save failed", "Qt could not write " + path);
    }
}
