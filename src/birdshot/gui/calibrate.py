# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Startup calibration wizard.

Asks the user to point the camera at the two things that define this scene's
dynamic range -- open sky and the treeline on the horizon -- and optionally at
the subject itself. For each, it lets the exposure loop settle and records the
exposure it converged on.

From those we get the range the scene actually spans, which sets how much
highlight headroom the metering has to leave. A 7 EV sky-to-treeline gap needs a
much more conservative target than a 3 EV one, and guessing that from a single
frame is exactly what gets birds silhouetted.

Results persist in the config, so this runs once and is only repeated when the
light changes materially.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

SETTLE_TIMEOUT_S = 8.0

STEPS = [
    ("sky", "Point the camera at open sky",
     "Fill the frame with bright, empty sky -- no trees, no roofline.\n"
     "This measures the brightest thing you will be shooting against."),
    ("treeline", "Point the camera at the treeline",
     "Frame the horizon or treeline where you expect birds to perch or pass.\n"
     "This measures the shadow end that the bird's colour profile lives in."),
    ("subject", "Optional: point at a subject or grey card",
     "A grey card, a branch, or anything at the distance you will be shooting.\n"
     "Skip this if you have nothing suitable -- it only refines the target."),
]


def ev_of(exposure_us: float, gain: float) -> float:
    """Exposure value of a shutter/gain pair, log2 of total light gathered."""
    return math.log2(max(exposure_us, 1.0) * max(gain, 0.01))


class CalibrationDialog(QDialog):
    def __init__(self, parent, cfg, engine):
        super().__init__(parent)
        self.cfg = cfg
        self.engine = engine
        self.setWindowTitle("Camera calibration")
        self.setModal(True)
        self.resize(520, 300)

        self._step = 0
        self._measuring = False
        self._started = 0.0
        self._results: Dict[str, Dict[str, Any]] = {}
        self._last_payload: Optional[Dict[str, Any]] = None

        self.title = QLabel()
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.live = QLabel("--")
        self.live.setStyleSheet("font-family: monospace; color: #8ab;")
        self.progress = QProgressBar()
        self.progress.setRange(0, len(STEPS))
        self.progress.setTextVisible(False)

        self.measure_btn = QPushButton("Measure")
        self.measure_btn.clicked.connect(self._measure)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.clicked.connect(self._skip)

        row = QHBoxLayout()
        row.addWidget(self.measure_btn)
        row.addWidget(self.skip_btn)
        row.addStretch(1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.title)
        layout.addWidget(self.body)
        layout.addStretch(1)
        layout.addWidget(QLabel("Live reading:"))
        layout.addWidget(self.live)
        layout.addLayout(row)
        layout.addWidget(self.buttons)

        self._render()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check_timeout)
        self._timer.start(250)

    # ------------------------------------------------------------------
    def _render(self) -> None:
        if self._step >= len(STEPS):
            self._finish()
            return
        key, title, body = STEPS[self._step]
        self.progress.setValue(self._step)
        self.title.setText("Step %d of %d - %s" % (self._step + 1, len(STEPS), title))
        self.body.setText(body)
        self.skip_btn.setEnabled(key == "subject")
        self.measure_btn.setEnabled(True)
        self.measure_btn.setText("Measure")

    def _measure(self) -> None:
        self._measuring = True
        self._started = time.time()
        self.measure_btn.setEnabled(False)
        self.measure_btn.setText("Settling...")
        # Let the controller converge from scratch on whatever is in frame now.
        self.engine.send("reset_ae")

    def _skip(self) -> None:
        self._step += 1
        self._render()

    # ------------------------------------------------------------------
    def handle_preview(self, payload: Dict[str, Any]) -> None:
        """Fed from the main window's preview stream."""
        self._last_payload = payload
        stats = payload.get("stats")
        exposure = payload.get("shutter_us") or 0
        gain = payload.get("gain") or 1.0
        lux = payload.get("lux")
        if stats is not None:
            self.live.setText(
                "shutter %8d us   gain %5.2f   lux %8s   p50 %3d   clip %.2f%%"
                % (exposure, gain, ("%.0f" % lux) if lux else "-",
                   int(stats.p50), stats.clip_hi * 100.0)
            )

        if not self._measuring:
            return
        decision = payload.get("decision")
        settled = bool(decision is not None and getattr(decision, "settled", False))
        if settled:
            self._record(payload)

    def _check_timeout(self) -> None:
        if self._measuring and (time.time() - self._started) > SETTLE_TIMEOUT_S:
            # Take the reading anyway; a scene that will not settle is usually
            # one at the end of the exposure range, which is still useful data.
            if self._last_payload is not None:
                self._record(self._last_payload, timed_out=True)
            else:
                self._measuring = False
                self._render()

    def _record(self, payload: Dict[str, Any], timed_out: bool = False) -> None:
        self._measuring = False
        key = STEPS[self._step][0]
        stats = payload.get("stats")
        exposure = float(payload.get("shutter_us") or 1.0)
        gain = float(payload.get("gain") or 1.0)
        lux = float(payload.get("lux") or 0.0)
        ev = ev_of(exposure, gain)
        self._results[key] = {
            "ev": ev,
            "luma": float(stats.p50) if stats else 0.0,
            "lux": lux,
            "clip": float(stats.clip_hi) if stats else 0.0,
            "timed_out": timed_out,
        }
        self.cfg.set_calibration(key, ev, self._results[key]["luma"], lux)
        self._step += 1
        self._render()

    # ------------------------------------------------------------------
    def _finish(self) -> None:
        sky = self._results.get("sky") or (self.cfg["calibration"] or {}).get("sky")
        tree = self._results.get("treeline") or (self.cfg["calibration"] or {}).get("treeline")
        self.progress.setValue(len(STEPS))
        self.title.setText("Calibration complete")

        lines = []
        if sky and tree:
            # Sky needs *less* exposure than treeline, so its EV is lower; the
            # gap between them is the range the sensor has to straddle.
            dr = abs(float(sky["ev"]) - float(tree["ev"]))
            lines.append("Sky to treeline range: %.1f EV" % dr)
            if dr > 7.0:
                lines.append(
                    "That is a very wide range. Metering will hold the target low to\n"
                    "protect the sky; expect the treeline to read dark."
                )
            elif dr < 2.5:
                lines.append("Narrow range -- flat light. Exposure should be easy to hold.")
            else:
                lines.append("Comfortable range for a single exposure.")
            self.cfg["max_clip_frac"] = max(0.005, min(0.08, 0.01 + 0.005 * dr))
            lines.append("Highlight tolerance set to %.1f%% of frame."
                         % (self.cfg["max_clip_frac"] * 100.0))
        else:
            lines.append("Incomplete -- sky and treeline are both needed for a range.")

        subject = self._results.get("subject")
        if subject and subject.get("luma"):
            target = max(70.0, min(160.0, float(subject["luma"])))
            self.cfg["target_luma"] = target
            lines.append("Metering target set from your subject reading: %.0f" % target)

        self.cfg.save()
        self.body.setText("\n".join(lines))
        self.live.setText("Saved to %s" % self.cfg.path)
        self.measure_btn.setEnabled(False)
        self.measure_btn.setText("Done")
        self.skip_btn.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Close).setDefault(True)
