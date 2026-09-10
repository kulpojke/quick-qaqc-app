#!/usr/bin/env python
"""Local map-feature annotation app."""

from __future__ import annotations

import argparse
import csv
import io
import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


DEFAULT_BUILDINGS_PATH = Path("../damage-map-web-map/data/buildings_h3.geojson")
DEFAULT_ANNOTATIONS_PATH = Path("data/qaqc/annotations_local.csv")
DEFAULT_LABEL_FIELD = ""
DEFAULT_ANNOTATION_LABELS = "damaged,undamaged,unknown"
DEFAULT_COG_PATH = ""
TILE_SIZE = 256

ANNOTATION_FIELDS = [
    "id",
    "predicted_class",
    "annotation_label",
    "qa_status",
    "qa_correct_class",
    "qa_notes",
    "reviewer",
    "reviewed_at",
]

HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Map Feature Annotator</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
  <script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/h3-js@4.2.1/dist/h3-js.umd.js"></script>
  <style>
    :root {
      --bg: #f8fafc;
      --panel: rgba(255, 255, 255, 0.96);
      --border: rgba(15, 23, 42, 0.16);
      --text: #17212b;
      --muted: #64748b;
      --unreviewed: #64748b;
      --annotated: #15803d;
      --cell-empty: #e5e7eb;
      --cell-partial: #fee08b;
      --cell-done: #1a9850;
      --shadow: 0 14px 34px rgba(15, 23, 42, 0.18);
    }

    html,
    body {
      height: 100%;
      margin: 0;
      overflow: hidden;
      width: 100%;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    #map {
      inset: 0 360px 0 0;
      position: fixed;
    }

    .panel {
      background: var(--panel);
      border-left: 1px solid var(--border);
      bottom: 0;
      box-shadow: var(--shadow);
      box-sizing: border-box;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 14px;
      overflow: auto;
      padding: 18px;
      position: fixed;
      right: 0;
      top: 0;
      width: 360px;
      z-index: 600;
    }

    h1,
    h2 {
      letter-spacing: 0;
      margin: 0;
    }

    h1 {
      font-size: 1rem;
      font-weight: 700;
    }

    h2 {
      font-size: 0.86rem;
      font-weight: 700;
    }

    .muted {
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.35;
    }

    .stats {
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(3, 1fr);
    }

    .stat {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px;
    }

    .stat strong {
      display: block;
      font-size: 1rem;
    }

    .form {
      display: grid;
      gap: 12px;
    }

    label {
      display: grid;
      font-size: 0.78rem;
      font-weight: 650;
      gap: 5px;
    }

    input,
    select,
    textarea,
    button {
      box-sizing: border-box;
      font: inherit;
      width: 100%;
    }

    input,
    select,
    textarea {
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      padding: 8px 9px;
    }

    textarea {
      min-height: 92px;
      resize: vertical;
    }

    button {
      background: #17212b;
      border: 0;
      border-radius: 6px;
      color: #fff;
      cursor: pointer;
      font-weight: 700;
      padding: 10px 12px;
    }

    button:disabled {
      cursor: not-allowed;
      opacity: 0.48;
    }

    .button-row {
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(3, 1fr);
    }

    .button-row button {
      background: #334155;
      font-size: 0.78rem;
      padding: 8px;
    }

    .label-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .label-buttons button {
      background: #f8fafc;
      border: 1px solid var(--border);
      color: var(--text);
      font-size: 0.78rem;
      padding: 8px 10px;
      width: auto;
    }

    .label-buttons button.active {
      background: #17212b;
      border-color: #17212b;
      color: #fff;
    }

    .selected {
      border: 1px solid var(--border);
      border-radius: 6px;
      display: grid;
      gap: 6px;
      padding: 10px;
    }

    .selected code {
      overflow-wrap: anywhere;
    }

    details {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px;
    }

    summary {
      cursor: pointer;
      font-size: 0.82rem;
      font-weight: 700;
    }

    .settings-body {
      display: grid;
      gap: 10px;
      padding-top: 10px;
    }

    .settings-status {
      color: var(--muted);
      font-size: 0.76rem;
      line-height: 1.3;
      min-height: 1rem;
    }

    .legend {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      bottom: 18px;
      box-shadow: var(--shadow);
      display: grid;
      font-size: 0.78rem;
      gap: 7px;
      left: 16px;
      padding: 10px;
      position: fixed;
      z-index: 500;
    }

    .legend div {
      align-items: center;
      display: flex;
      gap: 7px;
    }

    .legend-heading {
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 700;
      margin-top: 2px;
      text-transform: uppercase;
    }

    .legend-heading:first-child {
      margin-top: 0;
    }

    .dot {
      border: 1px solid var(--border);
      border-radius: 999px;
      display: inline-block;
      height: 10px;
      width: 10px;
    }

    .dot.unreviewed { background: var(--unreviewed); }
    .dot.annotated { background: var(--annotated); }
    .dot.cell-empty { background: var(--cell-empty); }
    .dot.cell-partial { background: var(--cell-partial); }
    .dot.cell-done { background: var(--cell-done); }

    @media (max-width: 860px) {
      #map {
        inset: 0 0 44vh 0;
      }

      .panel {
        border-left: 0;
        border-top: 1px solid var(--border);
        height: 44vh;
        left: 0;
        top: auto;
        width: 100%;
      }
    }
  </style>
</head>
<body>
  <div id="map"></div>

  <aside class="panel">
    <section>
      <h1>Feature Annotator</h1>
      <p class="muted">Select an H3 cell, work through its features, then save annotations.</p>
    </section>

    <details>
      <summary>Settings</summary>
      <div class="settings-body">
        <label>
          Feature GeoJSON
          <input id="buildings-path">
        </label>
        <label>
          Label field
          <input id="label-field" placeholder="optional source label field">
        </label>
        <label>
          Annotation labels
          <input id="annotation-labels" placeholder="comma-separated labels">
        </label>
        <label>
          COG imagery
          <input id="cog-path" placeholder="optional local path or URL">
        </label>
        <label>
          Class probability field
          <input id="confidence-field" placeholder="optional">
        </label>
        <label>
          Max confidence
          <input id="confidence-filter" type="range" min="0" max="4" step="1">
          <span class="muted" id="confidence-filter-label">All</span>
        </label>
        <button id="apply-settings" type="button">Load</button>
        <div class="settings-status" id="settings-status"></div>
      </div>
    </details>

    <section class="stats">
      <div class="stat"><span class="muted">Total</span><strong id="total-count">0</strong></div>
      <div class="stat"><span class="muted">Annotated</span><strong id="reviewed-count">0</strong></div>
      <div class="stat"><span class="muted">Open</span><strong id="open-count">0</strong></div>
    </section>

    <section class="form">
      <label>
        Annotator
        <input id="reviewer" autocomplete="name" placeholder="annotator name">
      </label>

      <div class="selected">
        <h2>Selected H3 Cell</h2>
        <div class="muted">Cell: <code id="selected-cell">none</code></div>
        <div class="muted">Resolution: <strong id="selected-cell-resolution">none</strong></div>
        <div class="muted">Progress: <strong id="selected-cell-progress">none</strong></div>
        <div class="button-row">
          <button id="previous-building" disabled>Previous</button>
          <button id="next-open-building" disabled>Next Open</button>
          <button id="next-building" disabled>Next</button>
        </div>
      </div>

      <div class="selected">
        <h2>Selected Feature</h2>
        <div class="muted">ID: <code id="selected-id">none</code></div>
        <button id="toggle-feature-outline" type="button" disabled>Show Outline</button>
      </div>

      <div class="selected">
        <h2>Annotation Label</h2>
        <div class="label-buttons" id="correct-label-buttons"></div>
      </div>

      <label>
        Notes
        <textarea id="qa-notes" placeholder="optional"></textarea>
      </label>
    </section>

    <button id="save" disabled>Save Annotation</button>
  </aside>

  <div class="legend">
    <div class="legend-heading">H3 Grid Cells</div>
    <div><span class="dot cell-empty"></span>Not started</div>
    <div><span class="dot cell-partial"></span>Partially annotated</div>
    <div><span class="dot cell-done"></span>Fully annotated</div>
    <div class="legend-heading">Features</div>
    <div><span class="dot unreviewed"></span>Unannotated</div>
    <div><span class="dot annotated"></span>Annotated</div>
  </div>

  <script>
    const defaultBuildingsPath = __DEFAULT_BUILDINGS_PATH__;
    const defaultLabelField = __DEFAULT_LABEL_FIELD__;
    const defaultAnnotationLabels = __DEFAULT_ANNOTATION_LABELS__;
    const defaultCogPath = __DEFAULT_COG_PATH__;
    const map = L.map("map", { preferCanvas: true, maxZoom: 23 });
    const h3ZoomLayers = [
      { minZoom: 0, maxZoom: 8, resolution: 5 },
      { minZoom: 9, maxZoom: 10, resolution: 6 },
      { minZoom: 11, maxZoom: 12, resolution: 7 },
      { minZoom: 13, maxZoom: 14, resolution: 8 },
      { minZoom: 15, maxZoom: 16, resolution: 9 },
      { minZoom: 17, maxZoom: 99, resolution: 10 },
    ];
    const reviewerInput = document.getElementById("reviewer");
    const saveButton = document.getElementById("save");
    const buildingsPathInput = document.getElementById("buildings-path");
    const labelFieldInput = document.getElementById("label-field");
    const annotationLabelsInput = document.getElementById("annotation-labels");
    const cogPathInput = document.getElementById("cog-path");
    const confidenceFieldInput = document.getElementById("confidence-field");
    const confidenceFilterInput = document.getElementById("confidence-filter");
    const confidenceFilterLabel = document.getElementById("confidence-filter-label");
    const applySettings = document.getElementById("apply-settings");
    const settingsStatus = document.getElementById("settings-status");
    const selectedCell = document.getElementById("selected-cell");
    const selectedCellResolution = document.getElementById("selected-cell-resolution");
    const selectedCellProgress = document.getElementById("selected-cell-progress");
    const selectedId = document.getElementById("selected-id");
    const toggleFeatureOutline = document.getElementById("toggle-feature-outline");
    const correctLabelButtons = document.getElementById("correct-label-buttons");
    const qaNotes = document.getElementById("qa-notes");
    const totalCount = document.getElementById("total-count");
    const reviewedCount = document.getElementById("reviewed-count");
    const openCount = document.getElementById("open-count");
    const previousBuilding = document.getElementById("previous-building");
    const nextOpenBuilding = document.getElementById("next-open-building");
    const nextBuilding = document.getElementById("next-building");

    let selectedFeature = null;
    let selectedCellId = null;
    let selectedCellRes = null;
    let selectedCellBuildingIds = [];
    let selectedCellPosition = 0;
    let buildingsData = null;
    let buildingLayer = null;
    let h3GridLayer = null;
    let cogLayer = null;
    let selectedBuildingMarker = null;
    let selectedFeatureLayer = null;
    let selectedOutlineVisible = false;
    let featureLayersById = {};
    let annotations = {};
    let buildingsPath = localStorage.getItem("qaqcBuildingsPath") || defaultBuildingsPath;
    let labelField = localStorage.getItem("qaqcLabelField") || defaultLabelField;
    let annotationLabels = localStorage.getItem("qaqcAnnotationLabels") || defaultAnnotationLabels;
    let cogPath = localStorage.getItem("qaqcCogPath") || defaultCogPath;
    let confidenceField = localStorage.getItem("qaqcConfidenceField") || "";
    let confidenceFilter = Number(localStorage.getItem("qaqcConfidenceFilter") || 0);
    let selectedCorrectLabel = "";
    const confidenceFilters = [
      { label: "All", value: "all", maxRank: Infinity },
      { label: "Very low", value: "very_low", maxRank: 0 },
      { label: "Low or below", value: "low", maxRank: 1 },
      { label: "Medium or below", value: "medium", maxRank: 2 },
      { label: "High or below", value: "high", maxRank: 3 },
    ];

    buildingsPathInput.value = buildingsPath;
    labelFieldInput.value = labelField;
    annotationLabelsInput.value = annotationLabels;
    cogPathInput.value = cogPath;
    confidenceFieldInput.value = confidenceField;
    confidenceFilterInput.value = confidenceFilter;

    function updateConfidenceFilterLabel() {
      const filter = confidenceFilters[Number(confidenceFilterInput.value)] || confidenceFilters[0];
      confidenceFilterLabel.textContent = filter.label;
    }

    updateConfidenceFilterLabel();
    confidenceFilterInput.addEventListener("input", updateConfidenceFilterLabel);

    reviewerInput.value = localStorage.getItem("qaqcReviewer") || "";
    reviewerInput.addEventListener("input", () => {
      localStorage.setItem("qaqcReviewer", reviewerInput.value);
      updateH3Grid();
    });

    L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}",
      {
        attribution: "Tiles &copy; Esri",
        maxNativeZoom: 16,
        maxZoom: 23,
      }
    ).addTo(map);

    function annotationFor(feature) {
      return annotations[feature.properties.id] || {};
    }

    function labelFor(feature) {
      if (!labelField) {
        return "none";
      }
      const value = feature.properties[labelField];
      return value == null || value === "" ? "none" : String(value);
    }

    function configuredAnnotationLabels() {
      return annotationLabels
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
    }

    function labelValues() {
      if (buildingsData && labelField) {
        const values = Array.from(new Set(
          buildingsData.features
            .map((feature) => feature.properties[labelField])
            .filter((value) => value != null && value !== "")
            .map((value) => String(value))
        )).sort((left, right) => left.localeCompare(right));
        if (values.length) {
          return values;
        }
      }
      return Array.from(new Set(configuredAnnotationLabels()));
    }

    function setCorrectLabel(value) {
      selectedCorrectLabel = value || "";
      Array.from(correctLabelButtons.children).forEach((button) => {
        button.classList.toggle("active", button.dataset.value === selectedCorrectLabel);
      });
      saveButton.disabled = !selectedFeature || !selectedCorrectLabel;
    }

    function updateCorrectLabelOptions(selectedValue = "") {
      const values = labelValues();
      if (selectedValue && !values.includes(selectedValue)) {
        values.push(selectedValue);
      }
      if (!values.includes("unknown")) {
        values.push("unknown");
      }

      correctLabelButtons.replaceChildren();
      values.forEach((value) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.value = value;
        button.textContent = value;
        button.addEventListener("click", () => setCorrectLabel(value));
        correctLabelButtons.appendChild(button);
      });
      setCorrectLabel(values.includes(selectedValue) ? selectedValue : "");
    }

    function annotationLabelFor(row) {
      return row ? (row.annotation_label || row.qa_correct_class || "") : "";
    }

    function annotationIsComplete(row) {
      return Boolean(annotationLabelFor(row) || (row && row.qa_status));
    }

    function classConfidence(feature) {
      if (!confidenceField) {
        return null;
      }

      const value = feature.properties[confidenceField];
      if (value == null || value === "") {
        return null;
      }

      let probability = Number(value);
      if (!Number.isFinite(probability)) {
        return null;
      }
      if (probability > 1) {
        probability = probability / 100;
      }

      const label = labelFor(feature).toLowerCase();
      if (confidenceField.toLowerCase().includes("damaged") && label === "undamaged") {
        probability = 1 - probability;
      }

      return Math.max(0, Math.min(1, probability));
    }

    function confidenceCategory(feature) {
      const confidence = classConfidence(feature);
      if (confidence == null) {
        return null;
      }
      if (confidence < 0.6) {
        return { value: "very_low", rank: 0 };
      }
      if (confidence < 0.75) {
        return { value: "low", rank: 1 };
      }
      if (confidence < 0.9) {
        return { value: "medium", rank: 2 };
      }
      return { value: "high", rank: 3 };
    }

    function featurePassesConfidenceFilter(feature) {
      const filter = confidenceFilters[confidenceFilter] || confidenceFilters[0];
      if (filter.value === "all" || !confidenceField) {
        return true;
      }
      const category = confidenceCategory(feature);
      return category ? category.rank <= filter.maxRank : false;
    }

    function visibleFeatures() {
      return buildingsData ? buildingsData.features.filter(featurePassesConfidenceFilter) : [];
    }

    function isReviewed(buildingId) {
      return annotationIsComplete(annotations[buildingId]);
    }

    function setSettingsStatus(message) {
      settingsStatus.textContent = message || "";
    }

    async function updateCogLayer() {
      if (cogLayer) {
        map.removeLayer(cogLayer);
        cogLayer = null;
      }
      if (!cogPath) {
        setSettingsStatus("No COG loaded.");
        return;
      }

      setSettingsStatus("Checking COG...");
      const encodedPath = encodeURIComponent(cogPath);
      const infoResponse = await fetch(`/api/cog/info?path=${encodedPath}`);
      if (!infoResponse.ok) {
        throw new Error(await infoResponse.text());
      }
      const info = await infoResponse.json();
      const cacheKey = encodeURIComponent(`${cogPath}-${Date.now()}`);
      const bounds = L.latLngBounds(info.bounds);
      cogLayer = L.tileLayer(
        `/api/cog/tile/{z}/{x}/{y}.png?path=${encodedPath}&v=${cacheKey}`,
        {
          attribution: "COG imagery",
          bounds: bounds,
          maxNativeZoom: 23,
          maxZoom: 23,
          opacity: 1,
          tms: false,
        }
      ).addTo(map);
      let reportedTileError = false;
      cogLayer.on("tileerror", (event) => {
        if (!reportedTileError) {
          reportedTileError = true;
          setSettingsStatus(`COG tile failed: ${event.tile && event.tile.src ? event.tile.src : cogPath}`);
        }
      });
      cogLayer.on("load", () => {
        if (!reportedTileError) {
          setSettingsStatus(`COG loaded: ${info.width} x ${info.height}, ${info.crs}`);
        }
      });
      map.fitBounds(bounds, { padding: [24, 24] });
      setSettingsStatus(`COG layer added: ${info.width} x ${info.height}, ${info.crs}`);
    }

    function h3ResolutionForZoom(zoom) {
      const numericZoom = Number.isFinite(zoom) ? zoom : 0;
      const layer = h3ZoomLayers.find((entry) => {
        return numericZoom >= entry.minZoom && numericZoom <= entry.maxZoom;
      });
      return layer ? layer.resolution : h3ZoomLayers[h3ZoomLayers.length - 1].resolution;
    }

    function h3ColumnForResolution(resolution) {
      return `h3_r${resolution}`;
    }

    function h3BoundaryLatLngs(cell) {
      const boundary = h3.cellToBoundary
        ? h3.cellToBoundary(cell, true)
        : h3.h3ToGeoBoundary(cell, true);
      const latLngs = boundary.map((coord) => [coord[1], coord[0]]);
      latLngs.push(latLngs[0]);
      return latLngs;
    }

    function h3CellStats(cell, resolution) {
      const column = h3ColumnForResolution(resolution);
      const ids = visibleFeatures()
        .filter((feature) => feature.properties[column] === cell)
        .map((feature) => feature.properties.id)
        .filter(Boolean);
      const reviewed = ids.filter(isReviewed).length;
      return {
        total: ids.length,
        reviewed: reviewed,
        completion: ids.length ? reviewed / ids.length : 0,
        ids: ids,
      };
    }

    function h3CompletionColor(completion) {
      if (completion >= 1) {
        return "#1a9850";
      }
      if (completion > 0) {
        return "#fee08b";
      }
      return "#e5e7eb";
    }

    function h3GridGeoJson(resolution) {
      const column = h3ColumnForResolution(resolution);
      const cells = new Set(
        visibleFeatures()
          .map((feature) => feature.properties[column])
          .filter(Boolean)
      );
      const visibleCells = selectedCellId && selectedCellRes === resolution
        ? [selectedCellId]
        : Array.from(cells);

      return {
        type: "FeatureCollection",
        features: visibleCells.map((cell) => {
          const stats = h3CellStats(cell, resolution);
          return {
            type: "Feature",
            properties: {
              h3_cell: cell,
              resolution: resolution,
              total: stats.total,
              reviewed: stats.reviewed,
              completion: stats.completion,
              completion_pct: `${Math.round(100 * stats.completion)}%`,
            },
            geometry: {
              type: "Polygon",
              coordinates: [h3BoundaryLatLngs(cell).map((coord) => [coord[1], coord[0]])],
            },
          };
        }),
      };
    }

    function styleH3Cell(feature) {
      const isSelected =
        feature.properties.h3_cell === selectedCellId &&
        feature.properties.resolution === selectedCellRes;
      if (isSelected) {
        return {
          color: "#ff1493",
          fill: false,
          fillColor: "#ff1493",
          fillOpacity: 0,
          opacity: 1,
          weight: 3.2,
        };
      }
      return {
        color: "#475569",
        fillColor: h3CompletionColor(feature.properties.completion),
        fillOpacity: 0.46,
        opacity: 0.72,
        weight: 0.9,
      };
    }

    function updateH3Grid() {
      if (!buildingsData) {
        return;
      }

      const resolution = selectedCellRes || h3ResolutionForZoom(map.getZoom());

      if (h3GridLayer) {
        map.removeLayer(h3GridLayer);
      }

      h3GridLayer = L.geoJSON(h3GridGeoJson(resolution), {
        interactive: !selectedCellId,
        style: styleH3Cell,
        onEachFeature: (feature, layer) => {
          if (!selectedCellId) {
            layer.on("click", () => selectCellFeature(feature));
          }
          layer.bindTooltip(
            `H3 r${feature.properties.resolution}<br>` +
              `${feature.properties.reviewed}/${feature.properties.total} annotated ` +
              `(${feature.properties.completion_pct})`,
            { sticky: false }
          );
        },
      }).addTo(map);

      if (selectedCellId) {
        h3GridLayer.bringToFront();
      }
      else {
        h3GridLayer.bringToBack();
      }

      if (buildingLayer && !selectedCellId) {
        buildingLayer.bringToFront();
      }
    }

    function styleFeature(feature) {
      const annotation = annotationFor(feature);
      let style = null;
      if (annotationIsComplete(annotation)) {
        style = { color: "#14532d", fillColor: "#15803d", fillOpacity: 0.42, weight: 1.4 };
      }
      else {
        style = {
          color: "#475569",
          fillColor: "#64748b",
          fillOpacity: 0.22,
          weight: 0.8,
        };
      }

      if (selectedFeature && feature.properties.id === selectedFeature.properties.id) {
        style.color = "#39ff14";
        style.fillOpacity = 0;
        style.opacity = selectedOutlineVisible ? 1 : 0;
        style.weight = selectedOutlineVisible ? 2.4 : 0;
      }

      return style;
    }

    function updateSelectedOutlineButton() {
      toggleFeatureOutline.disabled = !selectedFeature;
      toggleFeatureOutline.textContent = selectedOutlineVisible ? "Hide Outline" : "Show Outline";
    }

    function restyleSelectedFeature() {
      if (buildingLayer) {
        buildingLayer.setStyle(styleFeature);
      }
      if (selectedBuildingMarker) {
        selectedBuildingMarker.bringToFront();
      }
    }

    function toggleSelectedOutline() {
      if (!selectedFeature) {
        return;
      }
      selectedOutlineVisible = !selectedOutlineVisible;
      updateSelectedOutlineButton();
      restyleSelectedFeature();
    }

    toggleFeatureOutline.addEventListener("click", toggleSelectedOutline);

    function geometryCoordinates(geometry) {
      if (!geometry) {
        return [];
      }
      if (geometry.type === "Polygon") {
        return geometry.coordinates.flat(1);
      }
      if (geometry.type === "MultiPolygon") {
        return geometry.coordinates.flat(2);
      }
      return [];
    }

    function updateSelectedBuildingMarker() {
      if (selectedBuildingMarker) {
        map.removeLayer(selectedBuildingMarker);
        selectedBuildingMarker = null;
      }
      if (!selectedFeature || !selectedFeatureLayer) {
        return;
      }

      const center = selectedFeatureLayer.getBounds().getCenter();

      const maxDistance = geometryCoordinates(selectedFeature.geometry)
        .map((coordinate) => map.distance(center, L.latLng(coordinate[1], coordinate[0])))
        .reduce((largest, distance) => Math.max(largest, distance), 0);

      selectedBuildingMarker = L.circle(center, {
        radius: Math.max(0.5, 1.1 * maxDistance),
        color: "#39ff14",
        opacity: 1,
        fill: false,
        weight: 3,
        interactive: false,
      }).addTo(map);
      selectedBuildingMarker.bringToFront();
    }

    function updateCounts() {
      const total = buildingLayer ? buildingLayer.getLayers().length : 0;
      const reviewed = Object.values(annotations).filter(annotationIsComplete).length;
      totalCount.textContent = total;
      reviewedCount.textContent = reviewed;
      openCount.textContent = Math.max(total - reviewed, 0);
    }

    function clearSelectedBuilding() {
      selectedFeature = null;
      selectedFeatureLayer = null;
      selectedId.textContent = "none";
      setCorrectLabel("");
      qaNotes.value = "";
      saveButton.disabled = true;
      selectedOutlineVisible = false;
      updateSelectedOutlineButton();
      updateSelectedBuildingMarker();
    }

    function selectFeature(feature, layer) {
      selectedFeature = feature;
      selectedFeatureLayer = layer || featureLayersById[feature.properties.id] || null;
      selectedOutlineVisible = false;
      const props = feature.properties;
      const annotation = annotationFor(feature);

      selectedId.textContent = props.id || "none";
      updateSelectedOutlineButton();
      updateCorrectLabelOptions(annotationLabelFor(annotation));
      qaNotes.value = annotation.qa_notes || "";
      saveButton.disabled = !selectedCorrectLabel;
      if (buildingLayer) {
        buildingLayer.setStyle(styleFeature);
      }
      updateSelectedBuildingMarker();
      updateCellProgress();
    }

    function clearSelectedCell() {
      selectedCellId = null;
      selectedCellRes = null;
      selectedCellBuildingIds = [];
      selectedCellPosition = 0;
      updateCellProgress();
      updateH3Grid();
    }

    function selectCellFeature(feature) {
      selectedCellId = feature.properties.h3_cell;
      selectedCellRes = feature.properties.resolution;
      selectedCellBuildingIds = h3CellStats(selectedCellId, selectedCellRes).ids;
      const nextOpenIndex = selectedCellBuildingIds.findIndex((id) => !isReviewed(id));
      selectedCellPosition = nextOpenIndex >= 0 ? nextOpenIndex : 0;
      updateH3Grid();
      selectBuildingAtCellPosition(selectedCellPosition, false);
    }

    function selectBuildingAtCellPosition(position, panToBuilding) {
      if (!selectedCellBuildingIds.length) {
        return;
      }

      selectedCellPosition = (position + selectedCellBuildingIds.length) % selectedCellBuildingIds.length;
      const buildingId = selectedCellBuildingIds[selectedCellPosition];
      const layer = featureLayersById[buildingId];
      if (!layer) {
        return;
      }

      selectFeature(layer.feature, layer);
      if (panToBuilding) {
        map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 21 });
      }
    }

    function nextOpenBuildingIndex() {
      if (!selectedCellBuildingIds.length) {
        return -1;
      }

      for (let offset = 1; offset <= selectedCellBuildingIds.length; offset++) {
        const index = (selectedCellPosition + offset) % selectedCellBuildingIds.length;
        if (!isReviewed(selectedCellBuildingIds[index])) {
          return index;
        }
      }
      return -1;
    }

    function selectNextOpenBuilding() {
      const nextIndex = nextOpenBuildingIndex();
      if (nextIndex >= 0) {
        selectBuildingAtCellPosition(nextIndex, true);
      }
    }

    function finishSelectedCellReview() {
      clearSelectedBuilding();
      clearSelectedCell();
      const minZoom = Number.isFinite(map.getMinZoom()) ? map.getMinZoom() : 0;
      map.setZoom(Math.max(map.getZoom() - 2, minZoom));
    }

    function updateCellProgress() {
      if (!selectedCellId || !selectedCellBuildingIds.length) {
        selectedCell.textContent = "none";
        selectedCellResolution.textContent = "none";
        selectedCellProgress.textContent = "none";
        previousBuilding.disabled = true;
        nextOpenBuilding.disabled = true;
        nextBuilding.disabled = true;
        return;
      }

      const reviewed = selectedCellBuildingIds.filter(isReviewed).length;
      selectedCell.textContent = selectedCellId;
      selectedCellResolution.textContent = `r${selectedCellRes}`;
      selectedCellProgress.textContent =
        `${reviewed}/${selectedCellBuildingIds.length} annotated, ` +
        `feature ${selectedCellPosition + 1}/${selectedCellBuildingIds.length}`;
      previousBuilding.disabled = selectedCellBuildingIds.length < 2;
      nextOpenBuilding.disabled = reviewed >= selectedCellBuildingIds.length;
      nextBuilding.disabled = selectedCellBuildingIds.length < 2;
    }

    function onEachFeature(feature, layer) {
      if (feature.properties.id) {
        featureLayersById[feature.properties.id] = layer;
      }
      layer.on("click", () => {
        selectFeature(feature, layer);
        layer.bringToFront();
      });
      layer.bindTooltip(`ID: ${feature.properties.id}`, { sticky: false });
    }

    async function loadAnnotations() {
      const response = await fetch("/api/annotations");
      annotations = await response.json();
    }

    function renderFeatureLayer(fitToFeatures) {
      if (buildingLayer) {
        map.removeLayer(buildingLayer);
      }
      if (h3GridLayer) {
        map.removeLayer(h3GridLayer);
        h3GridLayer = null;
      }
      if (selectedBuildingMarker) {
        map.removeLayer(selectedBuildingMarker);
        selectedBuildingMarker = null;
      }
      clearSelectedBuilding();
      selectedCellId = null;
      selectedCellRes = null;
      selectedCellBuildingIds = [];
      selectedCellPosition = 0;
      featureLayersById = {};
      updateCorrectLabelOptions();
      const data = {
        type: buildingsData.type,
        features: visibleFeatures(),
      };
      buildingLayer = L.geoJSON(data, {
        style: styleFeature,
        onEachFeature: onEachFeature,
      }).addTo(map);
      if (fitToFeatures && !cogPath && buildingLayer.getLayers().length) {
        map.fitBounds(buildingLayer.getBounds(), { padding: [24, 24] });
      }
      updateH3Grid();
      updateCounts();
    }

    async function loadBuildings() {
      const response = await fetch(`/api/buildings?path=${encodeURIComponent(buildingsPath)}`);
      if (!response.ok) {
        throw new Error(await response.text());
      }
      buildingsData = await response.json();
      renderFeatureLayer(true);
    }

    previousBuilding.addEventListener("click", () => {
      selectBuildingAtCellPosition(selectedCellPosition - 1, true);
    });

    nextOpenBuilding.addEventListener("click", () => {
      selectNextOpenBuilding();
    });

    nextBuilding.addEventListener("click", () => {
      selectBuildingAtCellPosition(selectedCellPosition + 1, true);
    });

    applySettings.addEventListener("click", async () => {
      const nextBuildingsPath = buildingsPathInput.value.trim() || defaultBuildingsPath;
      const nextLabelField = labelFieldInput.value.trim();
      const nextAnnotationLabels = annotationLabelsInput.value.trim();
      const nextCogPath = cogPathInput.value.trim() || defaultCogPath;
      const nextConfidenceField = confidenceFieldInput.value.trim();
      const nextConfidenceFilter = Number(confidenceFilterInput.value);
      const reloadFeatures = nextBuildingsPath !== buildingsPath;
      const rerenderFeatures =
        reloadFeatures ||
        nextLabelField !== labelField ||
        nextAnnotationLabels !== annotationLabels ||
        nextConfidenceField !== confidenceField ||
        nextConfidenceFilter !== confidenceFilter;

      applySettings.disabled = true;
      setSettingsStatus("Loading settings...");
      buildingsPath = nextBuildingsPath;
      labelField = nextLabelField;
      annotationLabels = nextAnnotationLabels;
      cogPath = nextCogPath;
      confidenceField = nextConfidenceField;
      confidenceFilter = Number.isFinite(nextConfidenceFilter) ? nextConfidenceFilter : 0;
      localStorage.setItem("qaqcBuildingsPath", buildingsPath);
      localStorage.setItem("qaqcLabelField", labelField);
      localStorage.setItem("qaqcAnnotationLabels", annotationLabels);
      localStorage.setItem("qaqcCogPath", cogPath);
      localStorage.setItem("qaqcConfidenceField", confidenceField);
      localStorage.setItem("qaqcConfidenceFilter", String(confidenceFilter));
      saveButton.disabled = true;
      try {
        if (reloadFeatures) {
          await loadBuildings();
        }
        else if (rerenderFeatures) {
          renderFeatureLayer(false);
        }
      }
      catch (error) {
        setSettingsStatus(error.message);
        alert(error.message);
      }
      try {
        await updateCogLayer();
      }
      catch (error) {
        setSettingsStatus(error.message);
        alert(error.message);
      }
      applySettings.disabled = false;
    });

    map.on("zoomend", () => {
      updateH3Grid();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        clearSelectedCell();
      }
    });

    saveButton.addEventListener("click", async () => {
      if (!selectedFeature) {
        return;
      }

      const props = selectedFeature.properties;
      const payload = {
        id: props.id,
        predicted_class: labelField ? labelFor(selectedFeature) : "",
        annotation_label: selectedCorrectLabel,
        qa_status: "annotated",
        qa_correct_class: selectedCorrectLabel,
        qa_notes: qaNotes.value,
        reviewer: reviewerInput.value,
      };

      const response = await fetch("/api/annotations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        alert(await response.text());
        return;
      }

      const saved = await response.json();
      annotations[saved.id] = saved;
      buildingLayer.setStyle(styleFeature);
      updateH3Grid();
      updateCounts();
      updateCellProgress();
      if (selectedCellId && selectedCellBuildingIds.length) {
        const nextIndex = nextOpenBuildingIndex();
        if (nextIndex >= 0) {
          selectBuildingAtCellPosition(nextIndex, true);
        }
        else {
          finishSelectedCellReview();
        }
      }
    });

    async function init() {
      await loadAnnotations();
      await loadBuildings();
      try {
        await updateCogLayer();
      }
      catch (error) {
        setSettingsStatus(error.message);
        alert(error.message);
      }
    }

    init().catch((error) => {
      document.body.innerHTML = `<pre>${error.message}</pre>`;
    });
  </script>
</body>
</html>
"""


def empty_png_tile() -> bytes:
    ''' Creates empty tile to be used as a successful blank map tile response'''
    from PIL import Image

    image = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


CogSource = Path | str


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def parse_cog_source(value: str) -> CogSource | None:
    '''Parses COG source string into http(s) url, Path, or None'''
    value = value.strip()
    if not value:
        return None
    if is_http_url(value):
        return value
    return Path(value)


def cog_source_exists(source: CogSource) -> bool:
    '''Returns True if source is a string or Path to an existing file'''
    return isinstance(source, str) or source.exists()


def render_cog_tile(cog_source: CogSource, z: int, x: int, y: int) -> bytes:
    '''Returns rendered tile'''
    from rio_tiler.errors import TileOutsideBounds
    from rio_tiler.io import Reader

    try:
        # fetch the tile
        with Reader(str(cog_source)) as cog:
            # TODO: allow for different band orders?
            indexes = (1, 2, 3) if cog.dataset.count >= 3 else (1,)
            tile = cog.tile(x, y, z, indexes=indexes)
            return tile.render(img_format="PNG")
    except TileOutsideBounds:
        # return and empty tile
        return empty_png_tile()


def cog_info(cog_source: CogSource) -> dict[str, object]:
    '''Returns dict containing info about COG'''
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(str(cog_source)) as dataset:
        if dataset.crs is None:
            raise ValueError(f"COG has no CRS: {cog_source}")

        left, bottom, right, top = transform_bounds(
            dataset.crs,
            "EPSG:4326",
            *dataset.bounds,
            densify_pts=21,
        )
        return {
            "path": str(cog_source),
            "crs": dataset.crs.to_string(),
            "width": dataset.width,
            "height": dataset.height,
            "count": dataset.count,
            "bounds": [[bottom, left], [top, right]],
        }


class QaqcStore:
    def __init__(self, buildings_path: Path, annotations_path: Path):
        self.buildings_path = buildings_path
        self.annotations_path = annotations_path

    def read_buildings(self, buildings_path: Path | None = None) -> dict:
        '''
        Load json from buildings_path, fall back to path stored in object.
        TODO: currently falls back to harcoded dafault, in future use 
        config.yml to set  default
        '''
        path = buildings_path or self.buildings_path
        with path.open() as file:
            return json.load(file)

    def read_annotations(self) -> dict[str, dict[str, str]]:
        if not self.annotations_path.exists():
            return {}

        with self.annotations_path.open(newline="") as file:
            return {
                row["id"]: row
                for row in csv.DictReader(file)
                if row.get("id")
            }

    def write_annotation(self, annotation: dict[str, str]) -> dict[str, str]:
        annotations = self.read_annotations()
        annotation = {field: annotation.get(field, "") for field in ANNOTATION_FIELDS}
        annotation["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        annotations[annotation["id"]] = annotation

        self.annotations_path.parent.mkdir(parents=True, exist_ok=True)
        with self.annotations_path.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=ANNOTATION_FIELDS)
            writer.writeheader()
            writer.writerows(annotations.values())

        return annotation


def make_handler(store: QaqcStore):
    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                return

        def app_html(self) -> str:
            '''Returns frontend HTML string with default paths inserted'''
            return (
                HTML
                .replace("__DEFAULT_BUILDINGS_PATH__", json.dumps(str(store.buildings_path)))
                .replace("__DEFAULT_LABEL_FIELD__", json.dumps(DEFAULT_LABEL_FIELD))
                .replace("__DEFAULT_ANNOTATION_LABELS__", json.dumps(DEFAULT_ANNOTATION_LABELS))
                .replace("__DEFAULT_COG_PATH__", json.dumps(DEFAULT_COG_PATH))
            )

        def requested_buildings_path(self) -> Path:
            query = parse_qs(urlparse(self.path).query)
            value = query.get("path", [""])[0].strip()
            return Path(value) if value else store.buildings_path

        def requested_cog_source(self) -> CogSource | None:
            query = parse_qs(urlparse(self.path).query)
            value = query.get("path", [""])[0].strip()
            return parse_cog_source(value)

        def tile_coordinates(self) -> tuple[int, int, int]:
            path = urlparse(self.path).path
            tile_path = path.removeprefix("/api/cog/tile/").removesuffix(".png")
            z_text, x_text, y_text = tile_path.split("/")
            return int(z_text), int(x_text), int(y_text)

        def send_json(self, body: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                return

        def send_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                return

        def send_png(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                return

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self.send_text(self.app_html())
                return

            if path == "/api/buildings":
                buildings_path = self.requested_buildings_path()
                if not buildings_path.exists():
                    self.send_text(
                        f"Feature file not found: {buildings_path}",
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self.send_json(store.read_buildings(buildings_path))
                return

            if path == "/api/annotations":
                self.send_json(store.read_annotations())
                return

            if path == "/api/cog/info":
                cog_source = self.requested_cog_source()
                if cog_source is None:
                    self.send_text("COG path or URL is required", HTTPStatus.BAD_REQUEST)
                    return
                if not cog_source_exists(cog_source):
                    self.send_text(f"COG file not found: {cog_source}", HTTPStatus.NOT_FOUND)
                    return
                try:
                    self.send_json(cog_info(cog_source))
                except Exception as error:
                    self.send_text(str(error), HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if path.startswith("/api/cog/tile/") and path.endswith(".png"):
                cog_source = self.requested_cog_source()
                if cog_source is None:
                    self.send_text("COG path or URL is required", HTTPStatus.BAD_REQUEST)
                    return
                if not cog_source_exists(cog_source):
                    self.send_text(f"COG file not found: {cog_source}", HTTPStatus.NOT_FOUND)
                    return
                try:
                    z, x, y = self.tile_coordinates()
                    self.send_png(render_cog_tile(cog_source, z, x, y))
                except Exception as error:
                    self.send_text(str(error), HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            self.send_text("Not found", HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path != "/api/annotations":
                self.send_text("Not found", HTTPStatus.NOT_FOUND)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length) or b"{}")

            if not payload.get("id"):
                self.send_text("Missing feature id", HTTPStatus.BAD_REQUEST)
                return

            self.send_json(store.write_annotation(payload))

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local map-feature annotation app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        "--buildings",
        type=Path,
        default=DEFAULT_BUILDINGS_PATH,
        help=f"Feature GeoJSON path. Default: {DEFAULT_BUILDINGS_PATH}",
    )
    parser.add_argument(
        "--annotations",
        type=Path,
        default=DEFAULT_ANNOTATIONS_PATH,
        help=f"Annotation CSV path. Default: {DEFAULT_ANNOTATIONS_PATH}",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = QaqcStore(args.buildings, args.annotations)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(f"Annotation app: http://{args.host}:{args.port}")
    print(f"Features: {store.buildings_path}")
    print(f"Annotations: {store.annotations_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
