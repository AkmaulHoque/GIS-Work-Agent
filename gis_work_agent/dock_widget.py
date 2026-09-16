# -*- coding: utf-8 -*-
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .compat import LEFT_DOCK, RIGHT_DOCK


class GISWorkAgentDock(QDockWidget):
    def __init__(self, parent=None, icon_path=None):
        super().__init__("GIS Work Agent", parent)
        self.setObjectName("GISWorkAgentDock")

        root = QWidget(self)
        layout = QVBoxLayout(root)

        header = QHBoxLayout()
        self.brand_icon = QLabel()
        if icon_path:
            pix = QPixmap(icon_path)
            if not pix.isNull():
                self.brand_icon.setPixmap(pix.scaled(48, 48))
        self.brand_icon.setFixedSize(52, 52)
        header.addWidget(self.brand_icon)

        title = QLabel("<b>GIS Work Agent</b><br><span style='color:#667;'>plain text → GIS workflow → output</span>")
        title.setWordWrap(True)
        header.addWidget(title, 1)
        self.status_label = QLabel("● Ready")
        self.status_label.setToolTip("Agent status")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.context_label = QLabel("Project context: waiting for layer scan…")
        self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)

        history_row = QHBoxLayout()
        history_row.addWidget(QLabel("Recent:"))
        self.history = QComboBox()
        self.history.addItem("Recent prompts…")
        history_row.addWidget(self.history, 1)
        self.load_history_btn = QPushButton("Load")
        history_row.addWidget(self.load_history_btn)
        layout.addLayout(history_row)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Type normal words — GIS vocabulary is optional. Examples:\n"
            "• tell me the area in hectares\n"
            "• how big is this layer?\n"
            "• make a 500 m ring around Roads\n"
            "• cut Landuse using District_Boundary then calculate area in hectares\n"
            "• keep features in Villages where STATE_UT = ASSAM\n"
            "• calculate slope from DEM then polygonize the result\n"
            "• calculate NDVI using NIR B8 and Red B4\n"
            "• run native:buffer on Roads DISTANCE=250 DISSOLVE=true"
        )
        self.prompt.setMinimumHeight(130)
        layout.addWidget(self.prompt)

        suggest_row = QHBoxLayout()
        suggest_row.addWidget(QLabel("Suggested prompts"))
        suggest_row.addStretch(1)
        self.suggest_btn = QPushButton("Suggest Prompts")
        suggest_row.addWidget(self.suggest_btn)
        layout.addLayout(suggest_row)

        self.suggestions = QListWidget()
        self.suggestions.setMinimumHeight(135)
        self.suggestions.setToolTip("Double-click a suggestion to place it in the prompt box.")
        layout.addWidget(self.suggestions)

        suggestion_actions = QHBoxLayout()
        self.use_suggestion_btn = QPushButton("Use Selected Suggestion")
        self.append_suggestion_btn = QPushButton("Append")
        suggestion_actions.addWidget(self.use_suggestion_btn)
        suggestion_actions.addWidget(self.append_suggestion_btn)
        suggestion_actions.addStretch(1)
        layout.addLayout(suggestion_actions)

        row = QHBoxLayout()
        row.addWidget(QLabel("Output:"))
        self.output_mode = QComboBox()
        self.output_mode.addItems(["Temporary layer", "Save to project folder"])
        row.addWidget(self.output_mode, 1)
        self.auto_add = QCheckBox("Add outputs to map")
        self.auto_add.setChecked(True)
        row.addWidget(self.auto_add)
        layout.addLayout(row)

        btns = QHBoxLayout()
        self.run_btn = QPushButton("Run Agent")
        self.plan_btn = QPushButton("Preview Plan")
        self.clear_btn = QPushButton("Clear")
        btns.addWidget(self.run_btn)
        btns.addWidget(self.plan_btn)
        btns.addWidget(self.clear_btn)
        layout.addLayout(btns)

        hint = QLabel(
            "Advanced: use an installed Processing algorithm ID and optional PARAM=value hints. "
            "Example: native:buffer INPUT=Roads DISTANCE=100 DISSOLVE=true"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(QLabel("Agent report / diagnostics"))
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(False)
        layout.addWidget(self.report, 1)

        self.setWidget(root)
        self.setAllowedAreas(LEFT_DOCK | RIGHT_DOCK)

    def set_status(self, state):
        states = {
            "ready": ("● Ready", "#2D7DD2"),
            "running": ("● Running", "#D98B00"),
            "success": ("✓ Done", "#2E8B57"),
            "error": ("! Error", "#C43D3D"),
        }
        text, color = states.get(state, states["ready"])
        self.status_label.setText(text)
        self.status_label.setStyleSheet("font-weight:600; color:%s;" % color)

    def set_busy(self, busy):
        self.run_btn.setEnabled(not busy)
        self.plan_btn.setEnabled(not busy)
        self.suggest_btn.setEnabled(not busy)
        if busy:
            self.set_status("running")
