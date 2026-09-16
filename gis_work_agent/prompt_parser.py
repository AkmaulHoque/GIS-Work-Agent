# -*- coding: utf-8 -*-
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AgentStep:
    algorithm_id: str
    label: str
    params: Dict = field(default_factory=dict)
    output_name: str = "Agent_Output"
    notes: List[str] = field(default_factory=list)
    source_text: str = ""


class PromptParser:
    """Natural-language parser for common, high-confidence GIS requests.

    The parser intentionally handles only operations that can be mapped safely
    and predictably. Unknown clauses are delegated to the dynamic Processing
    registry matcher in ``agent_engine.py``.

    Prompt text is never evaluated as Python or shell code.
    """

    OP_START = (
        r"buffer|clip|crop|mask|cut|intersect(?:ion)?|union|difference|erase|"
        r"dissolve|fix\s+geometr(?:y|ies)|repair\s+geometr(?:y|ies)|make\s+valid|"
        r"centroid(?:s)?|convex\s+hull|multipart|simplif\w*|reproject|transform\s+crs|"
        r"merge|hillshade|slope|contour(?:s)?|polygonize|raster\s+to\s+polygon|"
        r"zonal\s+\w+|calculate\s+zonal|extract\s+by\s+expression|"
        r"select\s+by\s+expression|keep\s+features|filter\s+features|"
        r"calculate\s+(?:the\s+)?area|add\s+(?:the\s+)?area|find\s+(?:the\s+)?area|"
        r"tell\s+(?:me\s+)?(?:the\s+)?area|how\s+big|"
        r"calculate\s+(?:the\s+)?length|add\s+(?:the\s+)?length|find\s+(?:the\s+)?length|"
        r"tell\s+(?:me\s+)?(?:the\s+)?length|how\s+long|"
        r"spatial\s+join|join\s+attributes\s+by\s+location|join\s+by\s+location|"
        r"delete\s+(?:field|fields|column|columns)|drop\s+(?:field|fields|column|columns)|"
        r"retain\s+(?:field|fields|column|columns)|keep\s+only\s+(?:field|fields|column|columns)|"
        r"list\s+layers|describe\s+layer|inspect\s+layer|zoom\s+to|clear\s+selection|"
        r"raster\s+calculator|calculate\s+ndvi|ndvi"
    )

    def split_steps(self, prompt: str) -> List[str]:
        text = re.sub(r"\s+", " ", (prompt or "").strip())
        if not text:
            return []

        parts = re.split(
            r"\s*(?:;|\n)+\s*|\b(?:then|after\s+that|afterwards|next|followed\s+by)\b",
            text,
            flags=re.I,
        )
        parts = [p.strip(" ,.") for p in parts if p.strip(" ,.")]

        if len(parts) == 1:
            pattern = r"\s*(?:,|\band\b)\s+(?=(?:" + self.OP_START + r")\b)"
            parts = [p.strip(" ,.") for p in re.split(pattern, parts[0], flags=re.I) if p.strip(" ,.")]

        return parts

    @staticmethod
    def number(text: str, default: Optional[float] = None):
        scrubbed = re.sub(r"EPSG\s*[:=]?\s*\d{4,6}", "", text, flags=re.I)
        m = re.search(r"(-?\d+(?:\.\d+)?)", scrubbed)
        return float(m.group(1)) if m else default

    @staticmethod
    def epsg(text: str):
        m = re.search(r"EPSG\s*[:=]?\s*(\d{4,6})", text, flags=re.I)
        return "EPSG:%s" % m.group(1) if m else None

    @staticmethod
    def field_after(text: str, keyword: str):
        pattern = (
            rf"\b{re.escape(keyword)}\b\s+(?:field\s+)?[\"']?"
            r"([A-Za-z_][\w ]*?)[\"']?"
            r"(?=\s+(?:then|and|using|from|to|by|into|as|where|with)\b|$)"
        )
        m = re.search(pattern, text, flags=re.I)
        return m.group(1).strip() if m else None

    @staticmethod
    def quoted_expression(text: str):
        m = re.search(r"(?:expression|where)\s+[\"'](.+?)[\"']", text, flags=re.I)
        return m.group(1) if m else None

    @staticmethod
    def _simple_where_expression(text: str):
        """Turn simple English WHERE syntax into a QGIS expression.

        Examples:
          STATE_UT = ASSAM          -> "STATE_UT" = 'ASSAM'
          Area_Ha > 100             -> "Area_Ha" > 100
          Class_Name != Forest      -> "Class_Name" != 'Forest'
        """
        quoted = PromptParser.quoted_expression(text)
        if quoted:
            return quoted

        m = re.search(
            r"\bwhere\s+[\"']?([A-Za-z_][\w]*)[\"']?\s*"
            r"(>=|<=|!=|<>|=|>|<|LIKE|ILIKE)\s*"
            r"(.+?)(?=\s+(?:then|and\s+(?:buffer|clip|dissolve|calculate|reproject|merge)\b)|$)",
            text,
            flags=re.I,
        )
        if not m:
            return None

        field, op, raw = m.group(1), m.group(2).upper(), m.group(3).strip().strip(" ,.;")
        if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
            raw = raw[1:-1]

        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            value = raw
        elif raw.lower() in {"null", "none"}:
            if op in {"=", "=="}:
                return '"%s" IS NULL' % field
            return '"%s" IS NOT NULL' % field
        else:
            value = "'%s'" % raw.replace("'", "''")
        if op == "<>":
            op = "!="
        return '"%s" %s %s' % (field, op, value)

    @staticmethod
    def _field_list(text: str):
        m = re.search(
            r"(?:fields?|columns?)\s*[:=]?\s*(.+?)(?=\s+(?:then|from|of|in|on|using)\b|$)",
            text,
            flags=re.I,
        )
        if not m:
            return []
        raw = m.group(1).strip()
        raw = re.sub(r"\band\b", ",", raw, flags=re.I)
        vals = []
        for item in raw.split(","):
            item = item.strip().strip("'\"")
            if re.fullmatch(r"[A-Za-z_][\w ]*", item):
                vals.append(item.strip())
        return vals


    @staticmethod
    def _normalize_beginner(text: str) -> str:
        """Translate very simple/child-like wording into canonical GIS intent.

        This is deliberately rule based. It does not execute or evaluate prompt
        text; it only rewrites common phrases before the normal parser runs.
        """
        low = re.sub(r"\s+", " ", (text or "").strip().lower())

        # Remove harmless conversational filler while keeping layer/field names
        # in the original ``text`` for parameter extraction.
        low = re.sub(
            r"^(?:please\s+)?(?:can you|could you|would you|will you)\s+", "", low
        )
        low = re.sub(
            r"^(?:please\s+)?(?:i want(?: you)? to|i need(?: you)? to|i would like(?: you)? to|just)\s+",
            "", low,
        )

        # Area / length -- phrases a non-GIS user is likely to type.
        if (
            re.search(r"\b(?:calculate|find|get|give|tell|show|measure|work out)\s+(?:me\s+)?(?:the\s+)?area\b", low)
            or re.search(r"\barea\s+(?:in|as)\s+(?:ha|hectares?|square metres?|square meters?|m2|km2|sq km)\b", low)
            or re.search(r"\bhow\s+big\b", low)
            or re.search(r"\b(?:size|area)\s+of\s+(?:this|the|my)\s+(?:map|shape|polygon|layer|land|field)\b", low)
        ):
            low = "calculate area " + low

        if (
            re.search(r"\b(?:calculate|find|get|give|tell|show|measure|work out)\s+(?:me\s+)?(?:the\s+)?length\b", low)
            or re.search(r"\bhow\s+long\b", low)
            or re.search(r"\b(?:road|line|river|route)\s+length\b", low)
        ):
            low = "calculate length " + low

        # Common visual / everyday descriptions of GIS operations.
        if re.search(r"\b(?:ring|zone|space|distance)\s+around\b", low) or re.search(r"\baround\b.*\b\d", low):
            low = "buffer " + low
        if re.search(r"\bkeep\s+(?:only\s+)?(?:the\s+)?(?:part|things?|features?)\s+inside\b", low):
            low = "clip " + low
        if re.search(r"\b(?:put|combine|join)\s+.*\b(?:maps?|layers?|files?|rasters?)\s+together\b", low):
            low = "merge " + low
        if re.search(r"\b(?:middle|centre|center)\s+(?:point|points)\b", low):
            low = "centroid " + low
        if re.search(r"\b(?:fix|repair)\s+(?:my|this|the)?\s*(?:map|layer|shape|shapes|polygon|polygons)\b", low):
            low = "fix geometries " + low
        if re.search(r"\b(?:steepness|steep places?|how steep)\b", low):
            low = "slope " + low
        if re.search(r"\b(?:shadow map|terrain shadow|shaded relief)\b", low):
            low = "hillshade " + low
        if re.search(r"\b(?:elevation|height)\s+lines?\b", low):
            low = "contour " + low
        if re.search(r"\b(?:change|convert)\s+(?:the\s+)?(?:projection|coordinate system|crs)\b", low):
            low = "reproject " + low
        if re.search(r"\b(?:turn|convert)\s+(?:the\s+)?raster\s+(?:in)?to\s+(?:a\s+)?polygon", low):
            low = "polygonize " + low
        if re.search(r"\baverage\s+.*\b(?:by|for each)\s+(?:district|zone|polygon|area)\b", low):
            low = "zonal mean " + low

        # Normalize optional articles that previously caused exact-phrase misses.
        low = re.sub(r"\bcalculate\s+(?:the\s+)?area\b", "calculate area", low)
        low = re.sub(r"\badd\s+(?:the\s+)?area\b", "add area", low)
        low = re.sub(r"\bcalculate\s+(?:the\s+)?length\b", "calculate length", low)
        low = re.sub(r"\badd\s+(?:the\s+)?length\b", "add length", low)
        return re.sub(r"\s+", " ", low).strip()

    def parse_known(self, text: str) -> Optional[AgentStep]:
        low = self._normalize_beginner(text)
        step = None

        # Project / navigation commands
        if re.search(r"\b(?:list|show)\s+(?:all\s+)?layers\b", low):
            step = AgentStep("agent:listlayers", "List project layers", {}, "Layer_List")

        elif re.search(r"\b(?:describe|inspect|summarize)\s+(?:the\s+)?(?:layer\s+)?", low) and "layer" in low:
            step = AgentStep("agent:describelayer", "Describe layer", {}, "Layer_Description")

        elif re.search(r"\bzoom\s+to\b", low):
            step = AgentStep("agent:zoomtolayer", "Zoom to layer", {}, "Zoom")

        elif "clear selection" in low or "remove selection" in low or "deselect all" in low:
            step = AgentStep("agent:clearselection", "Clear selection", {}, "Selection_Cleared")

        # Vector operations
        elif "buffer" in low:
            distance = self.number(text, 10.0)
            step = AgentStep(
                "native:buffer", "Buffer",
                {"DISTANCE": distance, "SEGMENTS": 5, "END_CAP_STYLE": 0,
                 "JOIN_STYLE": 0, "MITER_LIMIT": 2, "DISSOLVE": "dissolve" in low,
                 "SEPARATE_DISJOINT": False},
                "Buffer", ["Buffer distance interpreted as %s layer units." % distance]
            )

        elif re.search(r"\b(?:clip|crop|mask|cut)\b", low):
            if any(w in low for w in ["raster", "dem", "tif", "image", "satellite"]):
                step = AgentStep(
                    "gdal:cliprasterbymasklayer", "Clip raster by mask",
                    {"CROP_TO_CUTLINE": True, "KEEP_RESOLUTION": True}, "Clipped_Raster"
                )
            else:
                step = AgentStep("native:clip", "Clip", {}, "Clipped")

        elif "intersection" in low or "intersect" in low:
            step = AgentStep("native:intersection", "Intersection", {"GRID_SIZE": None}, "Intersection")

        elif re.search(r"\bunion\b", low):
            step = AgentStep("native:union", "Union", {"GRID_SIZE": None}, "Union")

        elif "difference" in low or "erase" in low:
            step = AgentStep("native:difference", "Difference / Erase", {"GRID_SIZE": None}, "Difference")

        elif "dissolve" in low:
            field = self.field_after(text, "by")
            step = AgentStep(
                "native:dissolve", "Dissolve",
                {"FIELD": [field] if field else [], "SEPARATE_DISJOINT": False},
                "Dissolved",
                ["Dissolve field: %s" % field if field else "No dissolve field found; dissolving all features."]
            )

        elif "fix geometr" in low or "repair geometr" in low or "make valid" in low:
            step = AgentStep("native:fixgeometries", "Fix geometries", {"METHOD": 1}, "Fixed_Geometries")

        elif "centroid" in low:
            step = AgentStep("native:centroids", "Centroids", {"ALL_PARTS": False}, "Centroids")

        elif "convex hull" in low:
            step = AgentStep("native:convexhull", "Convex hull", {}, "Convex_Hull")

        elif "multipart" in low and ("single" in low or "explode" in low):
            step = AgentStep("native:multiparttosingleparts", "Multipart to singleparts", {}, "Singleparts")

        elif "simplif" in low:
            tol = self.number(text, 1.0)
            step = AgentStep("native:simplifygeometries", "Simplify geometries",
                             {"METHOD": 0, "TOLERANCE": tol}, "Simplified")

        elif ("reproject" in low or "transform crs" in low or "change crs" in low) and any(
                w in low for w in ["raster", "dem", "tif", "image"]):
            crs = self.epsg(text) or "EPSG:4326"
            step = AgentStep("gdal:warpreproject", "Reproject raster",
                             {"TARGET_CRS": crs}, "Reprojected_Raster", ["Target CRS: %s" % crs])

        elif "reproject" in low or "transform crs" in low or "change crs" in low:
            crs = self.epsg(text) or "EPSG:4326"
            step = AgentStep("native:reprojectlayer", "Reproject layer",
                             {"TARGET_CRS": crs}, "Reprojected", ["Target CRS: %s" % crs])

        elif "spatial join" in low or "join attributes by location" in low or "join by location" in low:
            step = AgentStep(
                "native:joinattributesbylocation", "Join attributes by location",
                {"PREDICATE": [0], "JOIN_FIELDS": [], "METHOD": 1,
                 "DISCARD_NONMATCHING": False, "PREFIX": ""},
                "Spatial_Join"
            )

        elif ("extract by expression" in low or "select by expression" in low or
              "keep features" in low or "filter features" in low or
              re.search(r"\bwhere\b", low)):
            expr = self._simple_where_expression(text)
            if expr:
                if re.search(r"\b(?:remove|exclude|delete)\s+features\b", low):
                    expr = "NOT (%s)" % expr
                step = AgentStep("native:extractbyexpression", "Filter / extract features",
                                 {"EXPRESSION": expr}, "Filtered",
                                 ["Expression: %s" % expr])

        elif re.search(r"\b(?:calculate|add|find|measure|get|show|tell|give)\s+(?:me\s+)?(?:the\s+)?area\b", low) or "how big" in low:
            if "hect" in low or re.search(r"\bha\b", low):
                formula, field = "$area / 10000", "area_ha"
            elif "km2" in low or "sq km" in low or "square kilomet" in low:
                formula, field = "$area / 1000000", "area_km2"
            else:
                formula, field = "$area", "area_m2"
            step = AgentStep(
                "native:fieldcalculator", "Calculate area",
                {"FIELD_NAME": field, "FIELD_TYPE": 0, "FIELD_LENGTH": 20,
                 "FIELD_PRECISION": 4, "NEW_FIELD": True, "FORMULA": formula},
                "Area_Calculated", ["New field: %s" % field]
            )

        elif re.search(r"\b(?:calculate|add|find|measure|get|show|tell|give)\s+(?:me\s+)?(?:the\s+)?length\b", low) or "how long" in low:
            if "km" in low and "km2" not in low:
                formula, field = "$length / 1000", "length_km"
            else:
                formula, field = "$length", "length_m"
            step = AgentStep(
                "native:fieldcalculator", "Calculate length",
                {"FIELD_NAME": field, "FIELD_TYPE": 0, "FIELD_LENGTH": 20,
                 "FIELD_PRECISION": 4, "NEW_FIELD": True, "FORMULA": formula},
                "Length_Calculated", ["New field: %s" % field]
            )

        elif re.search(r"\b(?:delete|drop|remove)\s+(?:field|fields|column|columns)\b", low):
            fields = self._field_list(text)
            if fields:
                step = AgentStep("native:deletecolumn", "Delete fields", {"COLUMN": fields},
                                 "Fields_Removed", ["Fields: %s" % ", ".join(fields)])

        elif (re.search(r"\bretain\s+(?:field|fields|column|columns)\b", low) or
              re.search(r"\bkeep\s+only\s+(?:field|fields|column|columns)\b", low)):
            fields = self._field_list(text)
            if fields:
                step = AgentStep("native:retainfields", "Retain fields", {"FIELDS": fields},
                                 "Fields_Retained", ["Fields: %s" % ", ".join(fields)])

        # Raster operations
        elif "merge" in low and any(w in low for w in ["raster", "dem", "tif", "image"]):
            step = AgentStep("gdal:merge", "Merge rasters", {"PCT": False, "SEPARATE": False}, "Merged_Raster")

        elif "merge" in low:
            step = AgentStep("native:mergevectorlayers", "Merge vector layers", {"CRS": None}, "Merged_Vector")

        elif "hillshade" in low:
            step = AgentStep("gdal:hillshade", "Hillshade",
                             {"BAND": 1, "Z_FACTOR": 1.0, "SCALE": 1.0,
                              "AZIMUTH": 315.0, "ALTITUDE": 45.0}, "Hillshade")

        elif re.search(r"\bslope\b", low):
            step = AgentStep("gdal:slope", "Slope",
                             {"BAND": 1, "SCALE": 1.0,
                              "AS_PERCENT": "percent" in low or "%" in low}, "Slope")

        elif "contour" in low:
            interval = self.number(text, 10.0)
            step = AgentStep("gdal:contour", "Contour",
                             {"BAND": 1, "INTERVAL": interval, "FIELD_NAME": "ELEV"}, "Contours")

        elif "polygonize" in low or "raster to polygon" in low:
            step = AgentStep("gdal:polygonize", "Raster to polygon", {"BAND": 1, "FIELD": "DN"}, "Polygonized")

        elif ("zonal" in low and "stat" in low) or "zonal mean" in low or "zonal sum" in low:
            stats = [2]
            if "sum" in low:
                stats = [1]
            elif "median" in low:
                stats = [3]
            elif "minimum" in low or " min " in " %s " % low:
                stats = [5]
            elif "maximum" in low or " max " in " %s " % low:
                stats = [6]
            step = AgentStep("native:zonalstatisticsfb", "Zonal statistics",
                             {"RASTER_BAND": 1, "COLUMN_PREFIX": "z_", "STATISTICS": stats},
                             "Zonal_Statistics")

        elif "ndvi" in low:
            step = AgentStep("agent:ndvi", "Calculate NDVI", {}, "NDVI",
                             ["Requires two raster layers identified as NIR and Red in the prompt or project."])

        elif "raster calculator" in low:
            expr = self.quoted_expression(text)
            if expr:
                step = AgentStep("native:rastercalc", "Raster calculator", {"EXPRESSION": expr},
                                 "Raster_Calculation", ["Expression: %s" % expr])

        if step is not None:
            step.source_text = text
        return step
