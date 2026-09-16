# -*- coding: utf-8 -*-
import ast
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

import processing
from qgis.core import (
    QgsApplication,
    QgsMapLayer,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from .prompt_parser import AgentStep, PromptParser


@dataclass
class PlanResult:
    steps: List[AgentStep]
    messages: List[str]


class AgentExecutionError(RuntimeError):
    def __init__(self, message, step=None, params=None, original=None):
        super().__init__(message)
        self.step = step
        self.params = params or {}
        self.original = original

    def diagnostic_text(self):
        lines = [str(self)]
        if self.step is not None:
            lines.append("Algorithm: %s" % self.step.algorithm_id)
            lines.append("Operation: %s" % self.step.label)
        if self.params:
            lines.append("Parameters:")
            for key, value in sorted(self.params.items()):
                if isinstance(value, QgsMapLayer):
                    value = "%s (%s)" % (value.name(), value.id())
                elif isinstance(value, (list, tuple)):
                    value = ["%s (%s)" % (v.name(), v.id()) if isinstance(v, QgsMapLayer) else v for v in value]
                lines.append("  %s = %r" % (key, value))
        if self.original is not None:
            lines.append("Original error: %s: %s" % (type(self.original).__name__, self.original))
        return "\n".join(lines)


class GISAgentEngine:
    """Prompt-to-Processing workflow engine.

    High-confidence natural-language requests are mapped explicitly. Everything
    else is matched against the live QGIS Processing registry, so installed
    native/GDAL/GRASS/SAGA/plugin providers can be used without hard-coding
    every algorithm in this plugin.
    """

    AGENT_ACTIONS = {"agent:listlayers", "agent:describelayer", "agent:zoomtolayer", "agent:clearselection"}

    def __init__(self, iface):
        self.iface = iface
        self.parser = PromptParser()

    def _layers(self):
        return list(QgsProject.instance().mapLayers().values())

    def _active(self):
        try:
            return self.iface.activeLayer()
        except Exception:
            return None

    def _named_layers(self, text: str):
        """Resolve layer references from both exact and beginner-style names.

        Exact layer names always win.  If the user types only a meaningful part
        such as ``district`` for ``District_Boundary``, a conservative token
        matcher is used.  Generic tokens shared by many layers are ignored.
        """
        low = (text or "").lower()
        layers = self._layers()
        matches = []

        # 1) Exact full-name occurrence.
        for lyr in layers:
            name = (lyr.name() or "").strip()
            if name and name.lower() in low and lyr not in matches:
                matches.append(lyr)

        # 2) Partial human-friendly references (roads -> Roads_2025).
        prompt_words = set(re.findall(r"[a-z0-9]+", low))
        generic = {
            "layer", "map", "data", "file", "vector", "raster", "shape",
            "boundary", "polygon", "line", "point", "image", "dem", "grid",
            "the", "this", "my", "active", "current", "new", "output",
        }
        token_owners = {}
        layer_tokens = {}
        for lyr in layers:
            toks = [w for w in re.findall(r"[a-z0-9]+", (lyr.name() or "").lower())
                    if len(w) >= 3 and w not in generic]
            layer_tokens[lyr.id()] = toks
            for tok in set(toks):
                token_owners.setdefault(tok, []).append(lyr)

        scored = []
        for lyr in layers:
            if lyr in matches:
                continue
            toks = layer_tokens.get(lyr.id(), [])
            if not toks:
                continue
            present = [t for t in toks if t in prompt_words]
            if not present:
                continue
            coverage = len(present) / max(1, len(set(toks)))
            unique_hits = sum(1 for t in present if len(token_owners.get(t, [])) == 1)
            # One unique descriptive word is enough; otherwise require most of
            # the layer name to be represented in the prompt.
            if unique_hits or coverage >= 0.60:
                scored.append((unique_hits, coverage, len(present), lyr))

        scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        for _u, _c, _n, lyr in scored:
            if lyr not in matches:
                matches.append(lyr)
        return matches

    @staticmethod
    def _is_vector(layer):
        return isinstance(layer, QgsVectorLayer)

    @staticmethod
    def _is_raster(layer):
        return isinstance(layer, QgsRasterLayer)

    def _ordered_named_layers(self, text, want_vector=None):
        low = (text or "").lower()
        layers = self._named_layers(text)
        layers = sorted(layers, key=lambda lyr: low.find(lyr.name().lower()))
        if want_vector is True:
            layers = [x for x in layers if self._is_vector(x)]
        elif want_vector is False:
            layers = [x for x in layers if self._is_raster(x)]
        return layers

    def _available_by_type(self, vector=True):
        if vector:
            return [x for x in self._layers() if self._is_vector(x)]
        return [x for x in self._layers() if self._is_raster(x)]

    def _candidate_layers(self, text, vector=True):
        named = self._ordered_named_layers(text, want_vector=vector)
        active = self._active()
        result = list(named)
        if active is not None:
            ok = self._is_vector(active) if vector else self._is_raster(active)
            if ok and active not in result:
                result.append(active)
        for lyr in self._available_by_type(vector=vector):
            if lyr not in result:
                result.append(lyr)
        return result

    def _find_role_raster(self, text, role, candidates):
        low = (text or "").lower()
        role_words = {
            "nir": ["nir", "near infrared", "near-infrared", "b8", "band 8", "band8"],
            "red": ["red", "b4", "band 4", "band4"],
        }[role]

        for lyr in candidates:
            lname = lyr.name().lower()
            if any(k in lname for k in role_words):
                return lyr

        role_pos = min([low.find(w) for w in role_words if low.find(w) >= 0] or [-1])
        if role_pos >= 0:
            best = None
            best_dist = 10 ** 9
            for lyr in candidates:
                p = low.find(lyr.name().lower())
                if p >= role_pos:
                    dist = p - role_pos
                    if dist < best_dist:
                        best, best_dist = lyr, dist
            if best is not None:
                return best
        return None

    @staticmethod
    def _raster_ref(layer):
        # Native raster calculator references raster layers as "layer_name@band".
        safe_name = (layer.name() or "raster").replace('"', '\\"')
        return '"%s@1"' % safe_name

    def _choose_layers(self, text: str, step: AgentStep):
        vector = self._candidate_layers(text, vector=True)
        raster = self._candidate_layers(text, vector=False)
        named = self._named_layers(text)
        alg = step.algorithm_id
        p = dict(step.params)
        assumptions = []

        if alg in self.AGENT_ACTIONS:
            if alg in {"agent:describelayer", "agent:zoomtolayer"}:
                all_candidates = self._ordered_named_layers(text)
                target = all_candidates[0] if all_candidates else self._active()
                if target is None:
                    raise ValueError("No target layer is available in the project.")
                p["LAYER"] = target
                assumptions.append("Target layer: %s" % target.name())
            return p, assumptions

        if alg == "agent:ndvi":
            named_rasters = [x for x in self._ordered_named_layers(text, want_vector=False)]
            candidates = named_rasters or raster
            if len(candidates) < 2:
                raise ValueError("NDVI needs at least two raster layers (NIR and Red).")
            nir = self._find_role_raster(text, "nir", candidates)
            red = self._find_role_raster(text, "red", candidates)
            if nir is None or red is None or nir == red:
                nir, red = candidates[0], candidates[1]
                assumptions.append("NIR/Red roles were inferred from raster order. Preview before running.")
            expr = "(%s - %s) / (%s + %s)" % (
                self._raster_ref(nir), self._raster_ref(red), self._raster_ref(nir), self._raster_ref(red)
            )
            p.update({"INPUT": [nir, red], "EXPRESSION": expr})
            assumptions.append("NIR: %s; Red: %s" % (nir.name(), red.name()))
            assumptions.append("NDVI expression: %s" % expr)
            return p, assumptions

        single_vector = {
            "native:buffer", "native:dissolve", "native:fixgeometries", "native:centroids",
            "native:convexhull", "native:multiparttosingleparts", "native:simplifygeometries",
            "native:reprojectlayer", "native:extractbyexpression", "native:fieldcalculator",
            "native:deletecolumn", "native:retainfields",
        }
        if alg in single_vector:
            if not vector:
                raise ValueError("No vector layer is available in the project.")
            p["INPUT"] = vector[0]
            assumptions.append("Input layer: %s" % vector[0].name())

        elif alg in {"native:clip", "native:intersection", "native:union", "native:difference"}:
            if len(vector) < 2:
                raise ValueError("This operation needs two vector layers.")
            p["INPUT"], p["OVERLAY"] = vector[0], vector[1]
            assumptions.append("Input: %s; overlay/mask: %s" % (vector[0].name(), vector[1].name()))

        elif alg == "native:joinattributesbylocation":
            if len(vector) < 2:
                raise ValueError("Spatial join needs two vector layers.")
            named_vec = self._ordered_named_layers(text, want_vector=True)
            if len(named_vec) >= 2 and re.search(r"\bfrom\b.+\bto\b", text, flags=re.I):
                p["JOIN"], p["INPUT"] = named_vec[0], named_vec[1]
            else:
                p["INPUT"], p["JOIN"] = vector[0], vector[1]
            assumptions.append("Input: %s; join layer: %s" % (p["INPUT"].name(), p["JOIN"].name()))

        elif alg == "native:mergevectorlayers":
            chosen = [x for x in named if self._is_vector(x)]
            if len(chosen) < 2:
                chosen = vector
            if len(chosen) < 2:
                raise ValueError("Merge needs at least two vector layers.")
            p["LAYERS"] = chosen
            assumptions.append("Merging: %s" % ", ".join(x.name() for x in chosen))

        elif alg in {"gdal:slope", "gdal:hillshade", "gdal:contour", "gdal:polygonize", "gdal:warpreproject"}:
            if not raster:
                raise ValueError("No raster layer is available in the project.")
            p["INPUT"] = raster[0]
            assumptions.append("Input raster: %s" % raster[0].name())

        elif alg == "gdal:merge":
            chosen = [x for x in named if self._is_raster(x)]
            if len(chosen) < 2:
                chosen = raster
            if len(chosen) < 2:
                raise ValueError("Raster merge needs at least two raster layers.")
            p["INPUT"] = chosen
            assumptions.append("Merging rasters: %s" % ", ".join(x.name() for x in chosen))

        elif alg == "gdal:cliprasterbymasklayer":
            if not raster or not vector:
                raise ValueError("Raster clip needs a raster and a polygon mask layer.")
            p["INPUT"] = raster[0]
            p["MASK"] = vector[0]
            assumptions.append("Raster: %s; mask: %s" % (raster[0].name(), vector[0].name()))

        elif alg == "native:zonalstatisticsfb":
            if not raster or not vector:
                raise ValueError("Zonal statistics needs a polygon zone layer and a raster layer.")
            p["INPUT"] = vector[0]
            p["INPUT_RASTER"] = raster[0]
            assumptions.append("Zones: %s; raster: %s" % (vector[0].name(), raster[0].name()))

        elif alg == "native:rastercalc":
            chosen = [x for x in named if self._is_raster(x)] or raster
            if chosen:
                p["INPUT"] = chosen
                assumptions.append("Raster calculator inputs: %s" % ", ".join(x.name() for x in chosen))

        return p, assumptions

    _GENERIC_STOPWORDS = {
        "the", "a", "an", "to", "of", "by", "using", "use", "from", "on", "for", "and",
        "then", "after", "that", "layer", "layers", "raster", "rasters", "vector", "vectors",
        "calculate", "compute", "create", "make", "generate", "run", "apply", "do", "please",
        "tool", "operation", "output", "result", "results", "with", "active", "my", "this",
        "current", "selected", "data", "dataset", "file", "files", "qgis", "gis", "want", "need",
        "would", "like", "can", "you", "me", "it", "into", "new", "based", "all"
    }

    _GENERIC_SYNONYMS = {
        "crop": "clip", "cut": "clip", "mask": "clip", "trim": "clip",
        "erase": "difference", "subtract": "difference", "remove": "difference",
        "combine": "merge", "mosaic": "merge", "stitch": "merge", "append": "merge",
        "repair": "fix geometries", "invalid": "fix geometries", "valid": "fix geometries",
        "filter": "extract expression", "subset": "extract expression", "query": "extract expression",
        "select": "extract expression", "where": "extract expression",
        "project": "reproject", "projection": "reproject", "crs": "reproject",
        "center": "centroid", "centre": "centroid", "centers": "centroids", "centres": "centroids",
        "outline": "boundary", "boundaries": "boundary",
        "densify": "densify geometries", "simplify": "simplify geometries",
        "vectorize": "polygonize", "vectorise": "polygonize",
        "rasterize": "rasterize", "rasterise": "rasterize",
        "terrain": "slope", "gradient": "slope", "relief": "hillshade",
        "isolines": "contour", "isoline": "contour",
        "nearest": "nearest neighbour", "closest": "nearest neighbour",
        "statistics": "statistics", "stats": "statistics",
    }

    def _generic_operation_text(self, text: str):
        """Return operation-focused text for Processing registry matching.

        Layer names, paths, numbers, EPSG codes and polite filler often dominate a
        natural-language prompt and previously pushed otherwise obvious matches
        below the confidence threshold.  Removing them makes matching much more
        tolerant while still requiring an operation-level similarity.
        """
        low = (text or "").lower()
        # Strip exact project layer names before matching against tool names.
        for lyr in sorted(self._layers(), key=lambda x: len(x.name() or ""), reverse=True):
            name = (lyr.name() or "").strip().lower()
            if name:
                low = low.replace(name, " ")
        low = re.sub(r"\b[a-z0-9_]+:[a-z0-9_]+\b", " ", low)
        low = re.sub(r"\bepsg\s*[:=]?\s*\d{4,6}\b", " ", low)
        low = re.sub(r"[a-z]:[\\/][^,;]+", " ", low)
        low = re.sub(r"\b-?\d+(?:\.\d+)?\s*(?:m|km|cm|mm|ha|degrees?|percent|%)?\b", " ", low)
        words = [w for w in re.findall(r"[a-z][a-z0-9]+", low) if w not in self._GENERIC_STOPWORDS]
        expanded = []
        for w in words:
            expanded.append(w)
            repl = self._GENERIC_SYNONYMS.get(w)
            if repl:
                expanded.extend(repl.split())
        # preserve order but remove duplicate noise
        seen = set()
        out = []
        for w in expanded:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return " ".join(out), set(out)

    @staticmethod
    def _algorithm_search_text(alg):
        pieces = [alg.displayName() or "", alg.id() or ""]
        for attr in ("group", "groupId"):
            try:
                value = getattr(alg, attr)()
                if value:
                    pieces.append(str(value))
            except Exception:
                pass
        try:
            tags = alg.tags()
            if tags:
                pieces.extend(str(x) for x in tags)
        except Exception:
            pass
        return " ".join(pieces).lower().replace(":", " ").replace("_", " ")

    def _rank_generic_algorithms(self, text: str, limit=5):
        reg = QgsApplication.processingRegistry()
        explicit = re.search(r"\b([a-zA-Z0-9_]+:[a-zA-Z0-9_]+)\b", text or "")
        if explicit:
            alg = reg.algorithmById(explicit.group(1))
            if alg:
                return [(alg, 1.0)]

        prompt_norm, prompt_words = self._generic_operation_text(text)
        if not prompt_words:
            return []

        ranked = []
        for alg in reg.algorithms():
            name = (alg.displayName() or "").lower()
            hay = self._algorithm_search_text(alg)
            words = {w for w in re.findall(r"[a-z0-9]+", hay)
                     if w not in self._GENERIC_STOPWORDS and len(w) > 1}
            common = prompt_words & words
            overlap = len(common) / max(1, len(prompt_words | words))
            coverage = len(common) / max(1, len(prompt_words))
            seq_name = SequenceMatcher(None, prompt_norm, name).ratio()
            seq_hay = SequenceMatcher(None, prompt_norm, hay[:160]).ratio()
            phrase_bonus = 0.0
            if prompt_norm and (prompt_norm in hay or name in prompt_norm):
                phrase_bonus = 0.30
            elif any(len(w) >= 5 and w in name for w in prompt_words):
                phrase_bonus = 0.12
            score = min(1.0, 0.47 * coverage + 0.20 * overlap +
                        0.18 * seq_name + 0.08 * seq_hay + phrase_bonus)
            if common or seq_name >= 0.45 or phrase_bonus:
                ranked.append((alg, score))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:max(1, limit)]

    def _find_generic_algorithm(self, text: str):
        ranked = self._rank_generic_algorithms(text, limit=2)
        if not ranked:
            return None, 0.0
        return ranked[0]

    def _generic_step(self, text: str) -> Tuple[Optional[AgentStep], List[str]]:
        ranked = self._rank_generic_algorithms(text, limit=5)
        if not ranked:
            return None, ["I could not identify the GIS operation in: %s" % text]

        alg, score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        operation_text, operation_words = self._generic_operation_text(text)
        hay = self._algorithm_search_text(alg)
        strong_word = any(len(w) >= 4 and w in hay for w in operation_words)
        clear_gap = (score - second) >= 0.08

        # v1.0 used 0.34 unconditionally.  That rejected many normal sentences.
        # Accept a lower score only when there is actual operation-word evidence
        # and the winning tool is meaningfully better than alternatives.
        auto_ok = score >= 0.34 or (score >= 0.22 and strong_word and clear_gap)
        if not auto_ok:
            choices = "; ".join("%s [%s, %.2f]" % (a.displayName(), a.id(), s)
                                for a, s in ranked[:3])
            return None, [
                "The request is understandable but the operation match is ambiguous: %s" % text,
                "Closest installed tools: %s" % choices,
                "Tip: include one operation word such as buffer, clip, dissolve, merge, slope, contour, join, reproject, extract or the Processing algorithm ID."
            ]

        step = AgentStep(
            alg.id(), alg.displayName(), {}, "Agent_Output",
            ["Dynamic Processing-registry match confidence: %.2f" % score,
             "Interpreted operation text: %s" % (operation_text or text)], text
        )
        return step, []

    def plan(self, prompt: str) -> PlanResult:
        messages = []
        steps = []
        chunks = self.parser.split_steps(prompt)
        if not chunks:
            return PlanResult([], ["Please enter a GIS task."])

        for chunk in chunks:
            step = self.parser.parse_known(chunk)
            if step is None:
                step, msgs = self._generic_step(chunk)
                messages.extend(msgs)
            if step is None:
                messages.append("Could not plan: %s" % chunk)
                continue
            try:
                params, assumptions = self._choose_layers(chunk, step)
                step.params.update(params)
                step.notes.extend(assumptions)
                if step.algorithm_id == "agent:ndvi":
                    step.algorithm_id = "native:rastercalc"
            except Exception as exc:
                step.notes.append("Input resolution warning: %s" % exc)
            if not step.source_text:
                step.source_text = chunk
            steps.append(step)
        return PlanResult(steps, messages)

    @staticmethod
    def _algorithm_parameter_names(alg):
        return {d.name() for d in alg.parameterDefinitions()}

    @staticmethod
    def _destination_names(alg):
        try:
            return {d.name() for d in alg.destinationParameterDefinitions()}
        except Exception:
            return {d.name() for d in alg.parameterDefinitions() if "destination" in (d.type() or "").lower()}

    def _sanitize_params(self, alg, params):
        valid = self._algorithm_parameter_names(alg)
        clean = {}
        removed = []
        for key, value in params.items():
            if key not in valid:
                removed.append(key)
                continue
            if value is None:
                continue
            clean[key] = value
        return clean, removed

    @staticmethod
    def _literal_value(raw):
        raw = (raw or "").strip().strip(",;")
        if not raw:
            return raw
        low = raw.lower()
        if low in {"true", "yes", "on"}:
            return True
        if low in {"false", "no", "off"}:
            return False
        if low in {"none", "null"}:
            return None
        try:
            return ast.literal_eval(raw)
        except Exception:
            pass
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            return float(raw)
        return raw.strip("'\"")

    def _extract_explicit_assignments(self, text, alg):
        """Extract PARAM=value hints for advanced prompts.

        Only names that actually exist in the selected Processing algorithm are
        accepted. Values use ``ast.literal_eval`` where possible; prompt text is
        never executed.
        """
        valid = self._algorithm_parameter_names(alg)
        out = {}
        # Value stops before another PARAM= token or semicolon/end. Quoted/list
        # values can contain spaces.
        pattern = re.compile(
            r"\b([A-Za-z][A-Za-z0-9_]*)\s*=\s*(\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}|\"[^\"]*\"|'[^']*'|.+?)"
            r"(?=\s+[A-Za-z][A-Za-z0-9_]*\s*=|;|$)",
            flags=re.I,
        )
        layer_by_name = {(lyr.name() or "").lower(): lyr for lyr in self._layers()}
        for m in pattern.finditer(text or ""):
            key = m.group(1).upper()
            # QGIS parameter names are normally upper-case, but resolve case-insensitively.
            actual = next((v for v in valid if v.upper() == key), None)
            if actual is None:
                continue
            val = self._literal_value(m.group(2))
            if isinstance(val, str) and val.lower() in layer_by_name:
                val = layer_by_name[val.lower()]
            out[actual] = val
        return out

    def _generic_fill_params(self, step: AgentStep, prompt: str, prior_output=None):
        alg = QgsApplication.processingRegistry().algorithmById(step.algorithm_id)
        if not alg:
            raise ValueError("Processing algorithm not found: %s" % step.algorithm_id)

        params, removed = self._sanitize_params(alg, dict(step.params))
        if removed:
            step.notes.append("Ignored unsupported parameter(s) in this QGIS version: %s" % ", ".join(sorted(removed)))

        local_prompt = step.source_text or prompt
        params.update(self._extract_explicit_assignments(local_prompt, alg))

        named = self._named_layers(local_prompt)
        layers = list(named)
        active = self._active()
        if active is not None and active not in layers:
            layers.append(active)
        for lyr in self._layers():
            if lyr not in layers:
                layers.append(lyr)

        scrubbed = re.sub(r"EPSG\s*[:=]?\s*\d{4,6}", "", local_prompt, flags=re.I)
        # Remove explicit PARAM=value blocks from heuristic numeric assignment.
        scrubbed = re.sub(r"\b[A-Za-z][A-Za-z0-9_]*\s*=\s*[^,;]+", "", scrubbed)
        number_values = [float(x) for x in re.findall(r"\b-?\d+(?:\.\d+)?\b", scrubbed)]
        epsg = self.parser.epsg(local_prompt)
        number_idx = 0
        layer_idx = 0
        prior_used = False
        named_in_clause = bool(named)

        dest_names = self._destination_names(alg)
        for d in alg.parameterDefinitions():
            name = d.name()
            if name in params:
                continue
            typ = (d.type() or "").lower()
            desc = (d.description() or "").lower()

            if name in dest_names or "destination" in typ or "sink" in typ:
                continue

            if "crs" in typ and epsg:
                params[name] = epsg
                continue

            is_multi = "multilayer" in typ or "multiple" in typ
            is_layer_param = any(k in typ for k in ["source", "vector", "raster", "layer"])
            if is_layer_param:
                compatible = layers
                if "raster" in typ:
                    compatible = [x for x in layers if self._is_raster(x)]
                elif any(k in typ for k in ["source", "vector"]):
                    compatible = [x for x in layers if self._is_vector(x)]

                if prior_output is not None and not named_in_clause and not prior_used and not is_multi:
                    params[name] = prior_output
                    prior_used = True
                    continue
                if compatible:
                    if is_multi:
                        params[name] = compatible
                    else:
                        params[name] = compatible[min(layer_idx, len(compatible) - 1)]
                        layer_idx += 1
                    continue

            if any(k in typ for k in ["number", "distance", "scale"]):
                if number_idx < len(number_values):
                    params[name] = number_values[number_idx]
                    number_idx += 1
                    continue

            if "field" in typ:
                found = None
                for lyr in layers:
                    if self._is_vector(lyr):
                        for fld in lyr.fields():
                            if fld.name().lower() in local_prompt.lower():
                                found = fld.name()
                                break
                    if found:
                        break
                if found:
                    params[name] = [found] if "multiple" in typ else found
                    continue

            if "expression" in typ:
                quoted = self.parser.quoted_expression(local_prompt)
                if quoted:
                    params[name] = quoted
                    continue

            if "boolean" in typ:
                token = name.replace("_", " ").lower()
                if token in local_prompt.lower() or (desc and desc in local_prompt.lower()):
                    params[name] = not any(x in local_prompt.lower() for x in ["no %s" % token, "without %s" % token])
                    continue

            try:
                default = d.defaultValue()
            except Exception:
                default = None
            if isinstance(default, (str, int, float, bool, list, tuple)) and default not in ("", None):
                params[name] = default

        return params

    @staticmethod
    def _output_extension(out_def):
        typ = (out_def.type() or "").lower()
        if "raster" in typ:
            return ".tif"
        if "folder" in typ or "directory" in typ:
            return ""
        if "html" in typ:
            return ".html"
        if "file" in typ and "vector" not in typ:
            return ".dat"
        return ".gpkg"

    def _prepare_destinations(self, alg, params, step, index, temporary):
        try:
            defs = list(alg.destinationParameterDefinitions())
        except Exception:
            defs = [d for d in alg.parameterDefinitions()
                    if "destination" in (d.type() or "").lower() or "sink" in (d.type() or "").lower()]
        if not defs:
            return params

        preferred = [d for d in defs if d.name().upper() in {"OUTPUT", "RESULT", "DESTINATION"}]
        targets = preferred or defs[:1]
        for out_def in targets:
            name = out_def.name()
            if name in params and params[name] not in (None, ""):
                continue
            if temporary:
                params[name] = "TEMPORARY_OUTPUT"
                continue

            base = QgsProject.instance().homePath() or os.path.expanduser("~")
            out_dir = os.path.join(base, "GIS_Work_Agent_Output")
            os.makedirs(out_dir, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", step.output_name or step.label).strip("_") or "output"
            ext = self._output_extension(out_def)
            if not ext:
                params[name] = os.path.join(out_dir, "%02d_%s" % (index, safe))
            else:
                params[name] = os.path.join(out_dir, "%02d_%s%s" % (index, safe, ext))
        return params

    def _execute_agent_action(self, step):
        if step.algorithm_id == "agent:listlayers":
            lines = []
            for lyr in self._layers():
                if self._is_vector(lyr):
                    try:
                        count = lyr.featureCount()
                    except Exception:
                        count = "?"
                    lines.append("Vector | %s | %s | features=%s" % (lyr.name(), lyr.crs().authid(), count))
                elif self._is_raster(lyr):
                    try:
                        detail = "%sx%s bands=%s" % (lyr.width(), lyr.height(), lyr.bandCount())
                    except Exception:
                        detail = "raster"
                    lines.append("Raster | %s | %s | %s" % (lyr.name(), lyr.crs().authid(), detail))
                else:
                    lines.append("Layer | %s" % lyr.name())
            return {"TEXT": "\n".join(lines) if lines else "No layers are loaded."}

        if step.algorithm_id == "agent:describelayer":
            lyr = step.params.get("LAYER") or self._active()
            if lyr is None:
                raise ValueError("No layer is available to describe.")
            lines = ["Name: %s" % lyr.name(), "CRS: %s" % lyr.crs().authid()]
            if self._is_vector(lyr):
                lines.append("Type: Vector")
                lines.append("Features: %s" % lyr.featureCount())
                lines.append("Fields: %s" % ", ".join(f.name() for f in lyr.fields()))
            elif self._is_raster(lyr):
                lines.append("Type: Raster")
                lines.append("Size: %s x %s" % (lyr.width(), lyr.height()))
                lines.append("Bands: %s" % lyr.bandCount())
            return {"TEXT": "\n".join(lines)}

        if step.algorithm_id == "agent:zoomtolayer":
            lyr = step.params.get("LAYER") or self._active()
            if lyr is None:
                raise ValueError("No layer is available to zoom to.")
            self.iface.setActiveLayer(lyr)
            self.iface.zoomToActiveLayer()
            return {"TEXT": "Zoomed to %s" % lyr.name()}

        if step.algorithm_id == "agent:clearselection":
            total = 0
            for lyr in self._available_by_type(vector=True):
                try:
                    total += lyr.selectedFeatureCount()
                    lyr.removeSelection()
                except Exception:
                    pass
            return {"TEXT": "Cleared %s selected feature(s)." % total}

        raise ValueError("Unknown agent action: %s" % step.algorithm_id)

    def execute(self, prompt: str, temporary=True, add_outputs=True):
        plan = self.plan(prompt)
        if not plan.steps:
            detail = "\n".join(plan.messages) if plan.messages else "No GIS operation was identified."
            raise ValueError("I could not safely choose a GIS operation for this prompt.\n%s" % detail)

        feedback = QgsProcessingFeedback()
        context = QgsProcessingContext()
        try:
            context.setProject(QgsProject.instance())
        except Exception:
            pass

        results = []
        previous = None

        for i, step in enumerate(plan.steps, 1):
            if step.algorithm_id in self.AGENT_ACTIONS:
                try:
                    result = self._execute_agent_action(step)
                except Exception as exc:
                    raise AgentExecutionError(
                        "Agent action failed at workflow step %d." % i,
                        step=step, params=step.params, original=exc,
                    ) from exc
                results.append((step, result))
                continue

            alg = QgsApplication.processingRegistry().algorithmById(step.algorithm_id)
            if not alg:
                raise AgentExecutionError("Algorithm is not available in this QGIS installation.", step=step)

            params = self._generic_fill_params(step, prompt, previous)
            params = self._prepare_destinations(alg, params, step, i, temporary)
            params, removed = self._sanitize_params(alg, params)
            if removed:
                step.notes.append("Ignored unsupported parameter(s): %s" % ", ".join(sorted(removed)))

            try:
                ok, message = alg.checkParameterValues(params, context)
            except Exception:
                ok, message = True, ""
            if not ok:
                raise AgentExecutionError(
                    "QGIS rejected the planned parameters before execution: %s" % (message or "invalid parameters"),
                    step=step, params=params
                )

            try:
                result = processing.run(
                    step.algorithm_id,
                    params,
                    context=context,
                    feedback=feedback,
                    is_child_algorithm=False,
                )
            except Exception as exc:
                raise AgentExecutionError(
                    "GIS operation failed at workflow step %d." % i,
                    step=step,
                    params=params,
                    original=exc,
                ) from exc

            output_value = self._pick_primary_output(result)
            if output_value is not None:
                previous = output_value
                if add_outputs:
                    self._add_result_to_project(output_value, step.output_name)
            results.append((step, result))

        return plan, results

    @staticmethod
    def _pick_primary_output(result):
        if not isinstance(result, dict):
            return None
        for key in ["OUTPUT", "RESULT", "DESTINATION"]:
            if key in result:
                return result[key]
        for value in result.values():
            if isinstance(value, (QgsVectorLayer, QgsRasterLayer, str)):
                return value
        return None

    def _add_result_to_project(self, output, name):
        project = QgsProject.instance()
        if isinstance(output, QgsMapLayer):
            if not project.mapLayer(output.id()):
                output.setName(name)
                project.addMapLayer(output)
            return
        if isinstance(output, str) and output and output != "TEMPORARY_OUTPUT":
            v = QgsVectorLayer(output, name, "ogr")
            if v.isValid():
                project.addMapLayer(v)
                return
            r = QgsRasterLayer(output, name)
            if r.isValid():
                project.addMapLayer(r)
