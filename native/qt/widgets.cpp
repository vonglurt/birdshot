// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "widgets.hpp"

#include <QCloseEvent>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QPainter>

#include "theme.hpp"

// ------------------------------------------------------------- Accordion --

namespace {

const char kHeaderStyle[] =
    "QToolButton{border:none;border-left:4px solid #2f6f8f;background:#25303a;color:#cfe3ef;"
    "font-weight:700;font-size:14px;padding:8px 10px;text-align:left;border-radius:4px;}"
    "QToolButton:hover{background:#31414f;color:#eaf5fb;border-left:4px solid #4da3cc;}"
    "QToolButton:checked{background:#2f6f8f;color:#ffffff;border-left:4px solid #7fd0f0;}";

const char kHeaderGatedStyle[] =
    "QToolButton{border:none;border-left:4px solid #39414c;background:#20252b;color:#6a7480;"
    "font-weight:700;font-size:14px;padding:8px 10px;text-align:left;border-radius:4px;}";

}  // namespace

Accordion::Accordion(const QString& title, bool expanded, QWidget* parent) : QWidget(parent) {
  auto* outer = new QVBoxLayout(this);
  outer->setContentsMargins(0, 0, 0, 0);
  outer->setSpacing(2);

  header_ = new QToolButton;
  header_->setText(title);
  header_->setCheckable(true);
  header_->setChecked(expanded);
  header_->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
  header_->setArrowType(expanded ? Qt::DownArrow : Qt::RightArrow);
  header_->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
  header_->setMinimumHeight(38);
  header_->setCursor(Qt::PointingHandCursor);
  header_->setStyleSheet(kHeaderStyle);
  outer->addWidget(header_);

  summary_ = new QLabel;
  summary_->setStyleSheet("color:#93a3ad;font-size:11px;padding:2px 0 4px 16px;");
  summary_->setWordWrap(true);
  summary_->setVisible(false);  // shown by applyOpen once setSummary gives it text
  outer->addWidget(summary_);

  body_ = new QFrame;
  body_->setFrameShape(QFrame::NoFrame);
  bodyLayout_ = new QVBoxLayout(body_);
  bodyLayout_->setContentsMargins(14, 2, 2, 8);
  body_->setVisible(expanded);
  outer->addWidget(body_);

  auto* rule = new QFrame;
  rule->setFrameShape(QFrame::HLine);
  rule->setStyleSheet("color:#333;");
  outer->addWidget(rule);

  connect(header_, &QToolButton::toggled, this, [this](bool open) {
    if (gated_ && open) {
      header_->setChecked(false);
      return;
    }
    applyOpen(open);
    emit toggledOpen(open);
  });
}

void Accordion::applyOpen(bool open) {
  header_->setArrowType(open ? Qt::DownArrow : Qt::RightArrow);
  body_->setVisible(open);
  summary_->setVisible(!open && !summary_->text().isEmpty());
}

void Accordion::addWidget(QWidget* w) { bodyLayout_->addWidget(w); }
void Accordion::addLayout(QLayout* l) { bodyLayout_->addLayout(l); }

void Accordion::setSummary(const QString& text) {
  if (gated_) return;
  summaryText_ = text;
  summary_->setText(text);
  summary_->setVisible(!isExpanded() && !text.isEmpty());
}

void Accordion::setGated(const QString& reason) {
  if (!reason.isEmpty()) {
    gated_ = true;
    if (isExpanded()) header_->setChecked(false);
    applyOpen(false);
    body_->setEnabled(false);
    header_->setStyleSheet(kHeaderGatedStyle);
    summary_->setStyleSheet("color:#8a7440;font-size:11px;padding:2px 0 4px 16px;");
    summary_->setText(reason);
    summary_->setVisible(true);
  } else if (gated_) {
    gated_ = false;
    body_->setEnabled(true);
    header_->setStyleSheet(kHeaderStyle);
    summary_->setStyleSheet("color:#93a3ad;font-size:11px;padding:2px 0 4px 16px;");
    summary_->setText(summaryText_);
    summary_->setVisible(!isExpanded() && !summaryText_.isEmpty());
  }
}

void Accordion::setExpanded(bool open) {
  if (gated_ && open) return;
  header_->setChecked(open);
}

// ------------------------------------------------------------- ModeTuner --

ModeTuner::ModeTuner(const QStringList& labels, int current, int fontPx, QWidget* parent)
    : QWidget(parent), fontPx_(fontPx) {
  index_ = qBound(0, current, static_cast<int>(labels.size()) - 1);
  why_ = QStringList();
  for (int i = 0; i < labels.size(); ++i) why_ << QString();

  auto* lay = new QHBoxLayout(this);
  lay->setContentsMargins(0, 0, 0, 0);
  lay->setSpacing(4);

  auto arrow = [&](Qt::ArrowType type, int delta) {
    auto* b = new QToolButton;
    b->setArrowType(type);
    b->setFixedWidth(34);
    b->setMinimumHeight(46);
    b->setCursor(Qt::PointingHandCursor);
    connect(b, &QToolButton::clicked, this, [this, delta] { step(delta); });
    return b;
  };
  lay->addWidget(arrow(Qt::LeftArrow, -1));

  for (int i = 0; i < labels.size(); ++i) {
    auto* b = new QToolButton;
    b->setText(labels[i]);
    b->setCheckable(true);
    b->setMinimumHeight(46);
    b->setCursor(Qt::PointingHandCursor);
    b->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    connect(b, &QToolButton::clicked, this, [this, i] { setIndex(i); });
    buttons_ << b;
    lay->addWidget(b, 1);
  }
  lay->addWidget(arrow(Qt::RightArrow, +1));
  restyle();
}

void ModeTuner::restyle() {
  for (int i = 0; i < buttons_.size(); ++i) {
    const bool on = i == index_;
    buttons_[i]->setChecked(on);
    buttons_[i]->setStyleSheet(QStringLiteral(
        "QToolButton{border-radius:5px;font-size:%1px;padding:6px 4px;%2}"
        "QToolButton:hover{background:%3;color:#eaf5fb;}"
        "QToolButton:disabled{background:#1d2126;color:#4a545e;border:1px dashed #333a44;}")
        .arg(fontPx_)
        .arg(on ? "background:#1f7a3f;color:#ffffff;font-weight:800;border:2px solid #7fe3a2;"
                : "background:#232830;color:#93a3ad;font-weight:600;border:1px solid #39414c;")
        .arg(on ? "#25904a" : "#2d3540"));
  }
}

void ModeTuner::setIndex(int i) {
  i = qBound(0, i, static_cast<int>(buttons_.size()) - 1);
  if (i == index_) {
    restyle();  // a stray click can uncheck the checked button
    return;
  }
  index_ = i;
  restyle();
  emit changed(index_);
}

void ModeTuner::setAvailable(const QStringList& why) {
  why_ = why;
  for (int i = 0; i < buttons_.size() && i < why.size(); ++i) {
    buttons_[i]->setEnabled(why[i].isEmpty());
    buttons_[i]->setToolTip(why[i]);
  }
  restyle();
}

void ModeTuner::step(int delta) {
  int i = index_;
  for (int tries = 0; tries < buttons_.size(); ++tries) {
    i = (i + delta + buttons_.size()) % buttons_.size();
    if (i < why_.size() && !why_[i].isEmpty()) continue;
    setIndex(i);
    return;
  }
}

// --------------------------------------------------------------- FaceBar --

FaceBar::FaceBar(const QString& version, QWidget* parent) : QWidget(parent) {
  setFixedHeight(44);
  setStyleSheet("background:#20252b;border-bottom:1px solid #333a44;");

  auto* lay = new QHBoxLayout(this);
  lay->setContentsMargins(14, 0, 10, 0);
  lay->setSpacing(12);

  auto* mark = new QLabel(QStringLiteral("birdshot"));
  mark->setStyleSheet("font-size:15px;font-weight:800;color:#cfe3ef;border:none;");
  lay->addWidget(mark);

  if (!version.isEmpty()) {
    auto* ver = new QLabel(version);
    ver->setStyleSheet("font-size:11px;color:#7a8791;border:none;");
    lay->addWidget(ver);
  }
  lay->addStretch(1);

  auto* seg = new QWidget;
  seg->setStyleSheet("background:transparent;border:none;");
  auto* segLay = new QHBoxLayout(seg);
  segLay->setContentsMargins(0, 6, 0, 6);
  segLay->setSpacing(0);

  const QStringList faces{QStringLiteral("camera"), QStringLiteral("field"),
                          QStringLiteral("bench"), QStringLiteral("library")};
  for (const QString& name : faces) {
    auto* b = new QToolButton;
    QString label = name;
    label[0] = label[0].toUpper();
    b->setText(label);
    b->setCheckable(true);
    b->setCursor(Qt::PointingHandCursor);
    b->setMinimumHeight(30);
    connect(b, &QToolButton::clicked, this, [this, name] { emit facePicked(name); });
    buttons_ << b;
    segLay->addWidget(b);
  }
  lay->addWidget(seg);
  setActive(QStringLiteral("bench"));
}

void FaceBar::setActive(const QString& face) {
  const QStringList faces{QStringLiteral("camera"), QStringLiteral("field"),
                          QStringLiteral("bench"), QStringLiteral("library")};
  for (int i = 0; i < buttons_.size(); ++i) {
    const bool active = faces[i] == face;
    buttons_[i]->setChecked(active);
    QString radius;
    if (i == 0) radius = "border-top-left-radius:5px;border-bottom-left-radius:5px;";
    if (i == buttons_.size() - 1)
      radius = "border-top-right-radius:5px;border-bottom-right-radius:5px;";
    buttons_[i]->setStyleSheet(QStringLiteral(
        "QToolButton{padding:5px 16px;font-size:12px;border:1px solid #39414c;%1%2}"
        "QToolButton:hover{background:%3;color:#eaf5fb;}")
        .arg(radius)
        .arg(active ? "background:#2f6f8f;color:#ffffff;font-weight:800;"
                    : "background:#232830;color:#93a3ad;font-weight:600;")
        .arg(active ? "#38809f" : "#2d3540"));
  }
}

// ------------------------------------------------------- BlockingOverlay --

BlockingOverlay::BlockingOverlay(QWidget* parent) : QWidget(parent) { hide(); }

void BlockingOverlay::showMessage(const QString& title, const QString& detail,
                                  const QColor& accent) {
  title_ = title;
  detail_ = detail;
  if (accent.isValid()) accent_ = accent;
  if (parentWidget()) setGeometry(parentWidget()->rect());
  raise();
  show();
  update();
}

void BlockingOverlay::paintEvent(QPaintEvent*) {
  QPainter p(this);
  p.fillRect(rect(), QColor(12, 6, 4, 235));
  const QRect band = rect().adjusted(40, rect().height() / 4, -40, -rect().height() / 4);
  p.fillRect(band, QColor(30, 14, 10, 240));
  p.setPen(accent_);
  for (int i = 0; i < 3; ++i) p.drawRect(band.adjusted(i * 2, i * 2, -i * 2, -i * 2));
  p.setPen(QColor(255, 255, 255));
  p.setFont(theme::sans(30, true));
  p.drawText(band.adjusted(30, 24, -30, 0), Qt::AlignTop | Qt::AlignHCenter, title_);
  p.setPen(QColor(235, 225, 220));
  p.setFont(theme::mono(12));
  p.drawText(band.adjusted(30, 92, -30, -24), Qt::AlignTop | Qt::AlignLeft | Qt::TextWordWrap,
             detail_);
}

// ----------------------------------------------------- FullscreenPreview --

FullscreenPreview::FullscreenPreview(QWidget* preview, QWidget* parent)
    : QWidget(parent, Qt::Window) {
  setWindowTitle(QStringLiteral("birdshot - fullscreen"));
  setStyleSheet("background:#000;");
  auto* lay = new QVBoxLayout(this);
  lay->setContentsMargins(0, 0, 0, 0);
  lay->addWidget(preview);

  auto* hint = new QLabel(QStringLiteral("Esc or F11 to exit"), this);
  hint->setStyleSheet(
      "color:#aaa;background:rgba(0,0,0,150);padding:4px 8px;border-radius:4px;");
  hint->move(16, 16);
  hint->adjustSize();
}

void FullscreenPreview::keyPressEvent(QKeyEvent* e) {
  if (e->key() == Qt::Key_Escape || e->key() == Qt::Key_F11) {
    close();
    return;
  }
  QWidget::keyPressEvent(e);
}

void FullscreenPreview::mouseDoubleClickEvent(QMouseEvent*) { close(); }

void FullscreenPreview::closeEvent(QCloseEvent* e) {
  emit closed();
  QWidget::closeEvent(e);
}
