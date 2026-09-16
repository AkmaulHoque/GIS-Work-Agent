# -*- coding: utf-8 -*-
import html
import os
import traceback

from qgis.PyQt.QtCore import QSettings, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsMessageLog, QgsProject, QgsRasterLayer, QgsVectorLayer

from .agent_engine import AgentExecutionError, GISAgentEngine
from .compat import QAction, RIGHT_DOCK, qgis_message_level
from .dock_widget import GISWorkAgentDock
from .prompt_suggester import PromptSuggester


class GISWorkAgentPlugin:
    SETTINGS_HISTORY = "GISWorkAgent/prompt_history"

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dock = None
        self.engine = GISAgentEngine(iface)
        self.suggester = PromptSuggester(iface)
        self._icon_paths = {
            "idle": os.path.join(self.plugin_dir, "icons", "icon_idle.png"),
            "running": os.path.join(self.plugin_dir, "icons", "icon_running.png"),
            "success": os.path.join(self.plugin_dir, "icons", "icon_success.png"),
            "error": os.path.join(self.plugin_dir, "icons", "icon_error.png"),
        }

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.png"))
        self.action = QAction(icon, "GIS Work Agent", self.iface.mainWindow())
        self.action.setToolTip("Open prompt-driven GIS Work Agent")
        self.action.triggered.connect(self.show_dock)
        self.iface.addPluginToMenu("&GIS Work Agent", self.action)
        self.iface.addToolBarIcon(self.action)

        try:
            self.iface.currentLayerChanged.connect(self._on_context_changed)
        except Exception:
            pass
        try:
            QgsProject.instance().layersAdded.connect(self._on_context_changed)
            QgsProject.instance().layersRemoved.connect(self._on_context_changed)
        except Exception:
            pass

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&GIS Work Agent", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        try:
            self.iface.currentLayerChanged.disconnect(self._on_context_changed)
        except Exception:
            pass
        try:
            QgsProject.instance().layersAdded.disconnect(self._on_context_changed)
            QgsProject.instance().layersRemoved.disconnect(self._on_context_changed)
        except Exception:
            pass
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def _set_icon_state(self, state, reset_after_ms=None):
        path = self._icon_paths.get(state, self._icon_paths["idle"])
        if self.action is not None and os.path.exists(path):
            self.action.setIcon(QIcon(path))
        if self.dock is not None:
            dock_state = {"idle": "ready", "running": "running", "success": "success", "error": "error"}.get(state, "ready")
            self.dock.set_status(dock_state)
        if reset_after_ms:
            QTimer.singleShot(reset_after_ms, lambda: self._set_icon_state("idle"))

    def show_dock(self):
        if self.dock is None:
            self.dock = GISWorkAgentDock(self.iface.mainWindow(), os.path.join(self.plugin_dir, "icon.png"))
            self.dock.run_btn.clicked.connect(self.run_agent)
            self.dock.plan_btn.clicked.connect(self.preview_plan)
            self.dock.clear_btn.clicked.connect(self.clear_ui)
            self.dock.suggest_btn.clicked.connect(self.refresh_suggestions)
            self.dock.use_suggestion_btn.clicked.connect(self.use_selected_suggestion)
            self.dock.append_suggestion_btn.clicked.connect(lambda: self.use_selected_suggestion(append=True))
            self.dock.suggestions.itemDoubleClicked.connect(lambda _item: self.use_selected_suggestion())
            self.dock.load_history_btn.clicked.connect(self.load_history_item)
            self.iface.addDockWidget(RIGHT_DOCK, self.dock)
            self._load_history()
        self._update_context_label()
        self.refresh_suggestions()
        self._set_icon_state("idle")
        self.dock.show()
        self.dock.raise_()

    def _on_context_changed(self, *args):
        if self.dock is not None:
            self._update_context_label()
            self.refresh_suggestions()

    def _update_context_label(self):
        if not self.dock:
            return
        layers = list(QgsProject.instance().mapLayers().values())
        vectors = sum(isinstance(x, QgsVectorLayer) for x in layers)
        rasters = sum(isinstance(x, QgsRasterLayer) for x in layers)
        try:
            active = self.iface.activeLayer()
        except Exception:
            active = None
        active_name = active.name() if active is not None else "None"
        self.dock.context_label.setText(
            "Project context: %d vector, %d raster, %d other | Active: <b>%s</b>" %
            (vectors, rasters, max(0, len(layers) - vectors - rasters), html.escape(active_name))
        )

    def refresh_suggestions(self):
        if not self.dock:
            return
        query = self.dock.prompt.toPlainText().strip()
        suggestions = self.suggester.project_prompts(query=query, limit=22)
        self.dock.suggestions.clear()
        for text in suggestions:
            self.dock.suggestions.addItem(text)
        if suggestions:
            self.dock.suggestions.setCurrentRow(0)

    def use_selected_suggestion(self, append=False):
        if not self.dock:
            return
        item = self.dock.suggestions.currentItem()
        if item is None:
            return
        text = item.text()
        current = self.dock.prompt.toPlainText().strip()
        if append and current:
            self.dock.prompt.setPlainText(current + " then " + text)
        else:
            self.dock.prompt.setPlainText(text)
        self.dock.prompt.setFocus()

    def clear_ui(self):
        if self.dock:
            self.dock.prompt.clear()
            self.dock.report.clear()
            self.refresh_suggestions()

    def _load_history(self):
        if not self.dock:
            return
        values = QSettings().value(self.SETTINGS_HISTORY, [], type=list)
        self.dock.history.clear()
        self.dock.history.addItem("Recent prompts…")
        for p in values[:20]:
            self.dock.history.addItem(str(p))

    def _remember_prompt(self, prompt):
        prompt = (prompt or "").strip()
        if not prompt:
            return
        settings = QSettings()
        history = settings.value(self.SETTINGS_HISTORY, [], type=list)
        history = [str(x) for x in history if str(x).strip() and str(x) != prompt]
        history.insert(0, prompt)
        settings.setValue(self.SETTINGS_HISTORY, history[:20])
        self._load_history()

    def load_history_item(self):
        if not self.dock:
            return
        idx = self.dock.history.currentIndex()
        if idx <= 0:
            return
        self.dock.prompt.setPlainText(self.dock.history.currentText())

    @staticmethod
    def _param_summary(params):
        pieces = []
        for key, value in params.items():
            if key.upper() in {"OUTPUT", "RESULT", "DESTINATION"}:
                continue
            if hasattr(value, "name") and callable(value.name):
                shown = value.name()
            elif isinstance(value, (list, tuple)):
                shown_vals = []
                for v in value:
                    if hasattr(v, "name") and callable(v.name):
                        shown_vals.append(v.name())
                    else:
                        shown_vals.append(str(v))
                shown = ", ".join(shown_vals)
            else:
                shown = str(value)
            if len(shown) > 90:
                shown = shown[:87] + "..."
            pieces.append("%s=%s" % (key, shown))
        return "; ".join(pieces)

    def preview_plan(self):
        prompt = self.dock.prompt.toPlainText().strip()
        if not prompt:
            self.refresh_suggestions()
            self.dock.report.setHtml("<p>Type a GIS request or choose one of the suggested prompts.</p>")
            return
        self._remember_prompt(prompt)
        try:
            plan = self.engine.plan(prompt)
            out = ["<h3>Planned GIS workflow</h3>"]
            if plan.messages:
                out.append("<p>%s</p>" % "<br>".join(html.escape(x) for x in plan.messages))
            if not plan.steps:
                out.append("<p>No executable step found. Try a suggested prompt or mention the exact layer name.</p>")
            for i, step in enumerate(plan.steps, 1):
                out.append("<p><b>%d. %s</b><br><code>%s</code>" % (
                    i, html.escape(step.label), html.escape(step.algorithm_id)))
                params = self._param_summary(step.params)
                if params:
                    out.append("<br><small>%s</small>" % html.escape(params))
                if step.notes:
                    out.append("<br>%s" % "<br>".join(html.escape(x) for x in step.notes))
                out.append("</p>")
            out.append(
                "<p><b>Safety:</b> the prompt is translated into QGIS Processing parameters; "
                "it is never executed as Python or a shell command.</p>"
            )
            self.dock.report.setHtml("".join(out))
        except Exception as exc:
            self._show_error(exc)

    def run_agent(self):
        prompt = self.dock.prompt.toPlainText().strip()
        if not prompt:
            self.iface.messageBar().pushWarning("GIS Work Agent", "Enter a GIS task or choose a suggested prompt first.")
            self.refresh_suggestions()
            return
        self._remember_prompt(prompt)
        self.dock.set_busy(True)
        self._set_icon_state("running")
        try:
            temporary = self.dock.output_mode.currentIndex() == 0
            _plan, results = self.engine.execute(
                prompt,
                temporary=temporary,
                add_outputs=self.dock.auto_add.isChecked(),
            )
            out = ["<h3>Workflow completed</h3>"]
            for i, (step, result) in enumerate(results, 1):
                out.append("<p><b>%d. %s</b> — completed<br><code>%s</code>" % (
                    i, html.escape(step.label), html.escape(step.algorithm_id)))
                if step.notes:
                    out.append("<br>%s" % "<br>".join(html.escape(x) for x in step.notes))
                if isinstance(result, dict) and result.get("TEXT"):
                    out.append("<br><pre>%s</pre>" % html.escape(str(result.get("TEXT"))))
                else:
                    keys = ", ".join(result.keys()) if isinstance(result, dict) else "result"
                    out.append("<br>Outputs: %s" % html.escape(keys))
                out.append("</p>")
            self.dock.report.setHtml("".join(out))
            self.iface.messageBar().pushMessage(
                "GIS Work Agent", "GIS workflow completed.",
                level=qgis_message_level("Success"), duration=5
            )
            self._update_context_label()
            self.refresh_suggestions()
            self._set_icon_state("success", reset_after_ms=2200)
        except Exception as exc:
            self._show_error(exc)
        finally:
            self.dock.set_busy(False)

    def _show_error(self, exc):
        self._set_icon_state("error", reset_after_ms=3200)
        is_execution = isinstance(exc, AgentExecutionError)
        if is_execution:
            detail = exc.diagnostic_text()
        else:
            detail = "%s: %s" % (type(exc).__name__, exc)

        safe = html.escape(detail).replace("\n", "<br>")
        if self.dock:
            if is_execution:
                body = (
                    "<h3>QGIS could not finish this step</h3><p>%s</p>"
                    "<p>The prompt was understood, but QGIS rejected an input, parameter, geometry, CRS, "
                    "or Processing algorithm. The diagnostic above identifies the failed step.</p>"
                ) % safe
            else:
                body = (
                    "<h3>I need one more clue</h3><p>%s</p>"
                    "<p><b>Use very simple words if you like.</b> Examples: "
                    "<i>tell me the area in hectares</i>, <i>make a 100 m ring around this layer</i>, "
                    "<i>cut this map with district</i>, <i>fix my layer</i>, or <i>show the steep places</i>.</p>"
                    "<p>If you say <i>this layer</i> or do not name a layer, the Agent tries the active compatible layer first.</p>"
                ) % safe
            self.dock.report.setHtml(body)
        QgsMessageLog.logMessage(
            detail + "\n\n" + traceback.format_exc(),
            "GIS Work Agent",
            qgis_message_level("Critical"),
        )
        try:
            self.iface.messageBar().pushCritical("GIS Work Agent", str(exc))
        except Exception:
            pass
