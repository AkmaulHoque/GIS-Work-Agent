# -*- coding: utf-8 -*-
import re
from difflib import SequenceMatcher

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer


class PromptSuggester:
    """Generate plain-language prompts from the current QGIS project context."""

    def __init__(self, iface):
        self.iface = iface

    def _layers(self):
        return list(QgsProject.instance().mapLayers().values())

    def _active(self):
        try:
            return self.iface.activeLayer()
        except Exception:
            return None

    @staticmethod
    def _is_vector(layer):
        return isinstance(layer, QgsVectorLayer)

    @staticmethod
    def _is_raster(layer):
        return isinstance(layer, QgsRasterLayer)

    @staticmethod
    def _safe_name(layer):
        return layer.name() if layer is not None else "active layer"

    def project_prompts(self, query="", limit=18):
        layers = self._layers()
        vectors = [x for x in layers if self._is_vector(x)]
        rasters = [x for x in layers if self._is_raster(x)]
        active = self._active()

        prompts = [
            "List all layers in the project",
            "Describe the active layer",
            "Clear selection from all vector layers",
        ]

        if active is not None:
            name = self._safe_name(active)
            prompts.append("Zoom to %s" % name)
            if self._is_vector(active):
                prompts.extend([
                    "How big is this layer in hectares?",
                    "Tell me the area in hectares",
                    "Make a 100 m ring around this layer",
                    "Fix my layer",
                    "Give me the middle points",
                    "Fix geometries of %s" % name,
                    "Buffer %s by 100 m" % name,
                    "Dissolve %s" % name,
                    "Reproject %s to EPSG:4326" % name,
                    "Calculate area in hectares for %s" % name,
                    "Calculate length in km for %s" % name,
                ])
                fields = [f.name() for f in active.fields()]
                if fields:
                    prompts.append("Dissolve %s by %s" % (name, fields[0]))
                    prompts.append("Keep features in %s where %s = VALUE" % (name, fields[0]))
            elif self._is_raster(active):
                prompts.extend([
                    "Show me the steep places",
                    "Make a shadow map",
                    "Make elevation lines every 10 m",
                    "Calculate slope from %s" % name,
                    "Create hillshade from %s" % name,
                    "Create contours from %s at 10 m interval" % name,
                    "Polygonize %s" % name,
                    "Reproject raster %s to EPSG:4326" % name,
                ])

        if len(vectors) >= 2:
            a, b = vectors[0].name(), vectors[1].name()
            prompts.extend([
                "Clip %s by %s" % (a, b),
                "Intersect %s with %s" % (a, b),
                "Erase %s using %s" % (a, b),
                "Join attributes by location from %s to %s" % (b, a),
                "Merge %s and %s" % (a, b),
            ])

        if rasters and vectors:
            prompts.extend([
                "Clip raster %s by %s" % (rasters[0].name(), vectors[0].name()),
                "Calculate zonal mean of %s using %s" % (rasters[0].name(), vectors[0].name()),
            ])

        if len(rasters) >= 2:
            prompts.append("Merge raster %s and %s" % (rasters[0].name(), rasters[1].name()))
            # Common remote sensing naming convention hint only; the engine validates at run time.
            names = [r.name().lower() for r in rasters]
            nir = next((rasters[i] for i, n in enumerate(names) if any(k in n for k in ["nir", "b8", "band8"])), None)
            red = next((rasters[i] for i, n in enumerate(names) if any(k in n for k in ["red", "b4", "band4"])), None)
            if nir and red:
                prompts.append("Calculate NDVI using NIR %s and Red %s" % (nir.name(), red.name()))

        query = (query or "").strip()
        if query:
            prompts.extend(self.algorithm_prompts(query, active, limit=8))

        # Stable de-duplication and soft query ranking.
        unique = []
        seen = set()
        for p in prompts:
            key = p.lower()
            if key not in seen:
                seen.add(key)
                unique.append(p)

        if query:
            q = query.lower()
            unique.sort(key=lambda p: self._prompt_score(q, p.lower()), reverse=True)
        return unique[:limit]

    @staticmethod
    def _prompt_score(query, prompt):
        q_words = set(re.findall(r"[a-z0-9]+", query))
        p_words = set(re.findall(r"[a-z0-9]+", prompt))
        overlap = len(q_words & p_words) / max(1, len(q_words))
        seq = SequenceMatcher(None, query, prompt).ratio()
        return 0.7 * overlap + 0.3 * seq

    def algorithm_prompts(self, query, active=None, limit=8):
        """Suggest installed Processing algorithms related to free text.

        These suggestions intentionally expose the algorithm ID so advanced users
        can run uncommon tools without the plugin needing a hard-coded parser.
        """
        q = (query or "").strip().lower()
        if len(q) < 2:
            return []
        q_words = {w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2}
        scored = []
        for alg in QgsApplication.processingRegistry().algorithms():
            name = (alg.displayName() or "").strip()
            alg_id = (alg.id() or "").strip()
            hay = (name + " " + alg_id.replace(":", " ").replace("_", " ")).lower()
            words = set(re.findall(r"[a-z0-9]+", hay))
            overlap = len(q_words & words) / max(1, len(q_words))
            seq = SequenceMatcher(None, q, name.lower()).ratio()
            score = 0.75 * overlap + 0.25 * seq
            if score >= 0.24:
                scored.append((score, alg_id, name))
        scored.sort(reverse=True)

        active_name = self._safe_name(active) if active is not None else "active layer"
        out = []
        for _, alg_id, name in scored[:limit]:
            out.append("Run %s (%s) on %s" % (name, alg_id, active_name))
        return out
