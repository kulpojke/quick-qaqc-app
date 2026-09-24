#!/usr/bin/env python
"""Local map-feature annotation app."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from src.fetch_overture_buildings import ensure_overture_buildings
from src.project_config import ConfigError, ReviewConfig, load_review_config


DEFAULT_BUILDINGS_PATH = Path("../damage-map-web-map/data/buildings_h3.geojson")
DEFAULT_ANNOTATIONS_PATH = Path("data/qaqc/annotations_local.csv")
DEFAULT_LABEL_FIELD = ""
DEFAULT_ANNOTATION_LABELS = "damaged,undamaged,unknown"
DEFAULT_COG_PATH = ""
DEFAULT_FEATURE_ID_FIELD = 'id'
DEFAULT_H3_PREFIX = 'h3_r'
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
      <h1 id="project-name">Feature Annotator</h1>
      <p class="muted">Select an H3 cell, work through its features, then save annotations.</p>
      <p class="muted" id="workflow-summary"></p>
    </section>

    <details id="settings-panel">
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

      <div class="selected" id="workflow-mode-section">
        <h2>Task</h2>
        <div class="label-buttons" id="workflow-mode-buttons"></div>
      </div>

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
        <h2 id="label-heading">Annotation Label</h2>
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
    const defaultConfidenceField = __DEFAULT_CONFIDENCE_FIELD__;
    const yamlConfigured = __YAML_CONFIGURED__;
    const configuredProjectName = __PROJECT_NAME__;
    const configuredFeatureIdField = __FEATURE_ID_FIELD__;
    const configuredH3Prefix = __H3_PREFIX__;
    const configuredUser = __WORKFLOW_USER__;
    const workflowModes = __WORKFLOW_MODES__;
    const todoH3Indexes = __TODO_H3_INDEXES__;
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
    const settingsPanel = document.getElementById('settings-panel');
    const projectName = document.getElementById('project-name');
    const workflowSummary = document.getElementById('workflow-summary');
    const workflowModeSection = document.getElementById('workflow-mode-section');
    const workflowModeButtons = document.getElementById('workflow-mode-buttons');
    const labelHeading = document.getElementById('label-heading');

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
    let buildingsPath = yamlConfigured
      ? defaultBuildingsPath
      : localStorage.getItem('qaqcBuildingsPath') || defaultBuildingsPath;
    let labelField = yamlConfigured
      ? defaultLabelField
      : localStorage.getItem('qaqcLabelField') || defaultLabelField;
    let annotationLabels = yamlConfigured
      ? defaultAnnotationLabels
      : localStorage.getItem('qaqcAnnotationLabels') || defaultAnnotationLabels;
    let cogPath = yamlConfigured
      ? defaultCogPath
      : localStorage.getItem('qaqcCogPath') || defaultCogPath;
    let confidenceField = yamlConfigured
      ? defaultConfidenceField
      : localStorage.getItem('qaqcConfidenceField') || '';
    let confidenceFilter = yamlConfigured
      ? 0
      : Number(localStorage.getItem('qaqcConfidenceFilter') || 0);
    let selectedCorrectLabel = '';
    const reviewModes = workflowModes.filter((mode) => mode === 'annotation' || mode === 'qaqc');
    let activeMode = reviewModes[0] || 'annotation';
    const featureIdField = configuredFeatureIdField || 'id';
    const h3Prefix = configuredH3Prefix || 'h3_r';
    const todoAssignments = todoH3Indexes.map((index) => ({
      index: String(index),
      resolution: h3.getResolution ? h3.getResolution(index) : h3.h3GetResolution(index),
    }));
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

    if (yamlConfigured) {
      settingsPanel.hidden = true;
      projectName.textContent = configuredProjectName || 'Feature Annotator';
      workflowSummary.textContent = `${workflowModes.join(' + ')} | ${todoAssignments.length || 'all'} H3 assignment${todoAssignments.length === 1 ? '' : 's'}`;
      reviewerInput.value = configuredUser;
      reviewerInput.readOnly = true;
    }
    else {
      reviewerInput.value = localStorage.getItem('qaqcReviewer') || '';
      reviewerInput.addEventListener('input', () => {
        localStorage.setItem('qaqcReviewer', reviewerInput.value);
        updateH3Grid();
      });
    }

    function renderWorkflowModes() {
      workflowModeButtons.replaceChildren();
      reviewModes.forEach((mode) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.value = mode;
        button.textContent = mode === 'qaqc' ? 'QA/QC' : 'Annotation';
        button.classList.toggle('active', mode === activeMode);
        button.addEventListener('click', async () => {
          if (activeMode === mode) {
            return;
          }
          activeMode = mode;
          renderWorkflowModes();
          await loadAnnotations();
          renderFeatureLayer(false);
        });
        workflowModeButtons.appendChild(button);
      });
      workflowModeSection.hidden = reviewModes.length < 2;
      labelHeading.textContent = activeMode === 'qaqc' ? 'QA/QC Label' : 'Annotation Label';
    }

    renderWorkflowModes();

    L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}",
      {
        attribution: "Tiles &copy; Esri",
        maxNativeZoom: 16,
        maxZoom: 23,
      }
    ).addTo(map);

    function annotationFor(feature) {
      return annotations[featureId(feature)] || {};
    }

    function featureId(feature) {
      const value = feature && feature.properties
        ? feature.properties[featureIdField]
        : null;
      return value == null ? '' : String(value);
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
      if (!yamlConfigured && buildingsData && labelField) {
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
      return buildingsData
        ? buildingsData.features.filter((feature) => (
            featureIsAssigned(feature) && featurePassesConfidenceFilter(feature)
          ))
        : [];
    }

    function featureIsAssigned(feature) {
      if (!todoAssignments.length) {
        return true;
      }
      return todoAssignments.some((assignment) => {
        const column = h3ColumnForResolution(assignment.resolution);
        return String(feature.properties[column] || '') === assignment.index;
      });
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
      return `${h3Prefix}${resolution}`;
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
        .map(featureId)
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

      if (selectedFeature && featureId(feature) === featureId(selectedFeature)) {
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
      const visibleIds = buildingLayer
        ? buildingLayer.getLayers().map((layer) => featureId(layer.feature)).filter(Boolean)
        : [];
      const total = visibleIds.length;
      const reviewed = visibleIds.filter(isReviewed).length;
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
      selectedFeatureLayer = layer || featureLayersById[featureId(feature)] || null;
      selectedOutlineVisible = false;
      const props = feature.properties;
      const annotation = annotationFor(feature);

      selectedId.textContent = featureId(feature) || 'none';
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
      const id = featureId(feature);
      if (id) {
        featureLayersById[id] = layer;
      }
      layer.on('click', () => {
        selectFeature(feature, layer);
        layer.bringToFront();
      });
      layer.bindTooltip(`ID: ${id}`, { sticky: false });
    }

    async function loadAnnotations() {
      const response = await fetch(`/api/annotations?mode=${encodeURIComponent(activeMode)}`);
      if (!response.ok) {
        throw new Error(await response.text());
      }
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
        id: featureId(selectedFeature),
        predicted_class: labelField ? labelFor(selectedFeature) : "",
        annotation_label: selectedCorrectLabel,
        qa_status: "annotated",
        qa_correct_class: selectedCorrectLabel,
        qa_notes: qaNotes.value,
        reviewer: reviewerInput.value,
      };

      const response = await fetch(`/api/annotations?mode=${encodeURIComponent(activeMode)}`, {
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


def is_parquet_source(source: Path | str) -> bool:
    '''*!*! Return whether a local path or URL names a Parquet feature source.'''

    source_path = (
        urlparse(str(source)).path
        if is_http_url(str(source))
        else str(source)
    )
    return Path(source_path).suffix.lower() in {'.parquet', '.geoparquet'}


def quote_sql_identifier(value: str) -> str:
    '''*!*! Quote a DuckDB identifier after escaping embedded quotes.'''

    quote = chr(34)
    return f'{quote}{value.replace(quote, quote * 2)}{quote}'


def quote_sql_string(value: str) -> str:
    '''*!*! Quote a DuckDB string literal after escaping apostrophes.'''

    quote = chr(39)
    return f'{quote}{value.replace(quote, quote * 2)}{quote}'


def load_duckdb_extension(connection, name: str) -> None:
    '''*!*! Load a DuckDB extension, installing it when not cached locally.'''

    import duckdb

    try:
        connection.execute(f'LOAD {name}')
    except duckdb.Error:
        connection.execute(f'INSTALL {name}')
        connection.execute(f'LOAD {name}')


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
    '''Reads project features and layer persisted review records.
    This will change with backend addition'''

    def __init__(
        self,
        buildings_path: Path | str,
        annotations_path: Path,
        annotations_input_path: Path | None = None,
    ):
        '''Configures feature, output, and optional merged-input paths.'''

        self.buildings_path = buildings_path
        self.annotations_path = annotations_path
        self.annotations_input_path = annotations_input_path
        self._write_lock = threading.Lock()

    def _read_parquet_buildings(
        self,
        source: Path | str,
        h3_assignments: dict[str, set[str]],
    ) -> dict:
        '''*!*! Query local or remote GeoParquet and return a FeatureCollection.'''

        try:
            import duckdb
        except ImportError as error:
            raise RuntimeError(
                'DuckDB is required to read GeoParquet feature sources'
            ) from error

        connection = duckdb.connect()
        try:
            load_duckdb_extension(connection, 'spatial')
            if is_http_url(str(source)):
                load_duckdb_extension(connection, 'httpfs')

            description = connection.execute(
                'DESCRIBE SELECT * FROM read_parquet(?)',
                [str(source)],
            ).fetchall()
            geometry_columns = [
                name
                for name, data_type, *_ in description
                if str(data_type).startswith('GEOMETRY')
            ]
            if not geometry_columns:
                raise ValueError(f'GeoParquet has no geometry column: {source}')
            if len(geometry_columns) > 1:
                raise ValueError(
                    f'GeoParquet has multiple geometry columns: {geometry_columns}'
                )

            geometry_column = geometry_columns[0]
            property_columns = [
                name for name, *_ in description if name != geometry_column
            ]
            available_columns = {name for name, *_ in description}
            missing_columns = sorted(set(h3_assignments) - available_columns)
            if missing_columns:
                missing_text = ', '.join(missing_columns)
                raise ValueError(
                    f'GeoParquet is missing assigned H3 column(s): {missing_text}'
                )

            property_items = []
            for column in property_columns:
                property_items.extend(
                    [quote_sql_string(column), quote_sql_identifier(column)]
                )
            property_arguments = ', '.join(property_items)
            properties_sql = (
                f'json_object({property_arguments})'
                if property_items
                else 'json_object()'
            )

            parameters: list[object] = [str(source)]
            assignment_clauses = []
            for column, indexes in sorted(h3_assignments.items()):
                ordered_indexes = sorted(indexes)
                placeholders = ', '.join('?' for _ in ordered_indexes)
                assignment_clauses.append(
                    f'{quote_sql_identifier(column)} IN ({placeholders})'
                )
                parameters.extend(ordered_indexes)
            assignment_filter = ' OR '.join(assignment_clauses)
            where_sql = (
                f'WHERE {assignment_filter}'
                if assignment_clauses
                else ''
            )
            geometry_identifier = quote_sql_identifier(geometry_column)
            rows = connection.execute(
                f'''
SELECT json_object(
    'type', 'Feature',
    'geometry', ST_AsGeoJSON({geometry_identifier})::JSON,
    'properties', {properties_sql}
)
FROM read_parquet(?)
{where_sql}
''',
                parameters,
            ).fetchall()
            return {
                'type': 'FeatureCollection',
                'features': [json.loads(feature_json) for feature_json, in rows],
            }
        except duckdb.Error as error:
            raise RuntimeError(
                f'Could not query GeoParquet feature source {source}: {error}'
            ) from error
        finally:
            connection.close()

    def read_buildings(
        self,
        buildings_path: Path | str | None = None,
        *,
        h3_assignments: dict[str, set[str]] | None = None,
    ) -> dict:
        '''
        Loads GeoJSON or GeoParquet from buildings_path, falling back to the
        path stored in the object.
        TODO: currently falls back to harcoded dafault, in future use 
        config.yml to set  default
        '''
        source = self.buildings_path if buildings_path is None else buildings_path
        assignments = h3_assignments or {}
        if is_parquet_source(source):
            return self._read_parquet_buildings(source, assignments)
        if isinstance(source, str) and is_http_url(source):
            request = Request(
                source,
                headers={
                    'Accept': 'application/geo+json, application/json',
                    'User-Agent': 'damagemap-qaqc/1.0',
                },
            )
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        path = Path(source)
        with path.open(encoding='utf-8') as file:
            return json.load(file)

    @staticmethod
    def _read_annotation_file(path: Path | None) -> dict[str, dict[str, str]]:
        '''*!*! Read one annotation CSV and normalize merged wide rows.'''

        if path is None or not path.exists():
            return {}

        with path.open(newline='', encoding='utf-8') as file:
            annotations = {}
            for row in csv.DictReader(file):
                if not row.get('id'):
                    continue
                # Wide files from merge_qaqc_annotations.py have one label
                # column per reviewer. Treat them as complete without exposing
                # a previous reviewer's label in the blinded UI.
                reviewer_labels = [
                    value
                    for key, value in row.items()
                    if key.endswith('_annotation_label') and value
                ]
                if not row.get('annotation_label') and reviewer_labels:
                    row['qa_status'] = row.get('qa_status') or 'annotated'
                annotations[row['id']] = row
            return annotations

    def read_annotations(self) -> dict[str, dict[str, str]]:
        '''*!*! Overlay this user's output records on merged input records.'''
        # TODO: what does this do? Do we need it?
        annotations = self._read_annotation_file(self.annotations_input_path)
        annotations.update(self._read_annotation_file(self.annotations_path))
        return annotations

    def write_annotation(self, annotation: dict[str, str]) -> dict[str, str]:
        '''
        Atomically add or replace one record in the user output
        TODO: currently csv, this is where it should write to DB.
        '''

        with self._write_lock:
            # Only rewrite this user's output. Loaded merged input remains read-only.
            annotations = self._read_annotation_file(self.annotations_path)
            annotation = {field: annotation.get(field, '') for field in ANNOTATION_FIELDS}
            annotation['reviewed_at'] = datetime.now(timezone.utc).isoformat()
            annotations[annotation['id']] = annotation

            self.annotations_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    'w',
                    dir=self.annotations_path.parent,
                    newline='',
                    encoding='utf-8',
                    delete=False,
                ) as file:
                    temp_path = Path(file.name)
                    writer = csv.DictWriter(file, fieldnames=ANNOTATION_FIELDS)
                    writer.writeheader()
                    writer.writerows(annotations.values())
                os.replace(temp_path, self.annotations_path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

        return annotation


def assignment_columns(
    review_config: ReviewConfig | None,
) -> dict[str, set[str]]:
    '''*!*! Group configured H3 assignments by their feature column.'''

    if review_config is None:
        return {}

    import h3

    assignments: dict[str, set[str]] = {}
    for index in review_config.todo_h3_indexes:
        column = f'{review_config.h3_prefix}{h3.get_resolution(index)}'
        assignments.setdefault(column, set()).add(index)
    return assignments


def filter_buildings_for_assignments(
    buildings: dict,
    review_config: ReviewConfig | None,
) -> dict:
    '''*!*! Return only features belonging to assigned H3 cells.'''

    if review_config is None or not review_config.todo_h3_indexes:
        return buildings

    assignments = assignment_columns(review_config)

    filtered = dict(buildings)
    filtered['features'] = [
        feature
        for feature in buildings.get('features', [])
        if any(
            str(feature.get('properties', {}).get(column, '')) in indexes
            for column, indexes in assignments.items()
        )
    ]
    return filtered


def prepare_configured_features(review_config: ReviewConfig) -> None:
    '''*!*! Fetch or reuse YAML-configured Overture buildings before serving.'''

    if review_config.overture is None:
        return
    result = ensure_overture_buildings(
        cog_source=review_config.imagery_cog,
        fire_date=review_config.overture.fire_date,
        output_path=review_config.features_path,
        h3_prefix=review_config.h3_prefix,
        release_selection=review_config.overture.release_selection,
        refresh=review_config.overture.refresh,
    )
    action = 'Reused cached' if result.from_cache else 'Fetched'
    selection_text = (
        f'before {review_config.overture.fire_date}'
        if review_config.overture.release_selection == 'before_fire'
        else 'oldest available fallback'
    )
    print(
        f'{action} {result.feature_count:,} Overture buildings '
        f'from {result.release} ({selection_text})'
    )


def make_handler(
    store: QaqcStore,
    review_config: ReviewConfig | None = None,
    mode_stores: dict[str, QaqcStore] | None = None,
):
    '''*!*! Build an HTTP handler bound to project config and mode stores.'''

    stores = mode_stores or {'annotation': store}
    configured_buildings = None
    assigned_feature_ids = None
    assignments = assignment_columns(review_config)
    if review_config is not None:
        configured_buildings = filter_buildings_for_assignments(
            store.read_buildings(h3_assignments=assignments),
            review_config,
        )
        assigned_feature_ids = {
            str(feature.get('properties', {}).get(review_config.feature_id_field))
            for feature in configured_buildings.get('features', [])
            if feature.get('properties', {}).get(review_config.feature_id_field) is not None
        }

    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                return

        def app_html(self) -> str:
            '''*!*! Return frontend HTML with project defaults inserted.'''

            labels = DEFAULT_ANNOTATION_LABELS
            cog_path = DEFAULT_COG_PATH
            label_field = DEFAULT_LABEL_FIELD
            project_name = 'Feature Annotator'
            feature_id_field = DEFAULT_FEATURE_ID_FIELD
            h3_prefix = DEFAULT_H3_PREFIX
            workflow_user = ''
            workflow_modes = ['annotation']
            todo_h3_indexes = []
            if review_config is not None:
                labels = ','.join(review_config.annotation_labels)
                cog_path = review_config.imagery_cog
                label_field = review_config.predicted_class_field
                project_name = review_config.project_name
                feature_id_field = review_config.feature_id_field
                h3_prefix = review_config.h3_prefix
                workflow_user = review_config.user
                workflow_modes = list(review_config.modes)
                todo_h3_indexes = list(review_config.todo_h3_indexes)
            return (
                HTML
                .replace('__DEFAULT_BUILDINGS_PATH__', json.dumps(str(store.buildings_path)))
                .replace('__DEFAULT_LABEL_FIELD__', json.dumps(label_field))
                .replace('__DEFAULT_ANNOTATION_LABELS__', json.dumps(labels))
                .replace('__DEFAULT_COG_PATH__', json.dumps(cog_path))
                .replace(
                    '__DEFAULT_CONFIDENCE_FIELD__',
                    json.dumps(review_config.confidence_field if review_config else ''),
                )
                .replace('__YAML_CONFIGURED__', json.dumps(review_config is not None))
                .replace('__PROJECT_NAME__', json.dumps(project_name))
                .replace('__FEATURE_ID_FIELD__', json.dumps(feature_id_field))
                .replace('__H3_PREFIX__', json.dumps(h3_prefix))
                .replace('__WORKFLOW_USER__', json.dumps(workflow_user))
                .replace('__WORKFLOW_MODES__', json.dumps(workflow_modes))
                .replace('__TODO_H3_INDEXES__', json.dumps(todo_h3_indexes))
            )

        def requested_buildings_source(self) -> Path | str:
            '''*!*! Returns the configured local or remote feature source.'''

            if review_config is not None:
                return store.buildings_path
            query = parse_qs(urlparse(self.path).query)
            value = query.get("path", [""])[0].strip()
            if not value:
                return store.buildings_path
            return value if is_http_url(value) else Path(value)

        def requested_cog_source(self) -> CogSource | None:
            if review_config is not None:
                return parse_cog_source(review_config.imagery_cog)
            query = parse_qs(urlparse(self.path).query)
            value = query.get("path", [""])[0].strip()
            return parse_cog_source(value)

        def requested_annotation_store(self) -> QaqcStore | None:
            '''*!*! Return the configured persistence store for a requested mode.'''

            query = parse_qs(urlparse(self.path).query)
            mode = query.get('mode', ['annotation'])[0].strip().lower()
            return stores.get(mode)

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
                if configured_buildings is not None:
                    self.send_json(configured_buildings)
                    return
                buildings_source = self.requested_buildings_source()
                if (
                    isinstance(buildings_source, Path)
                    and not buildings_source.exists()
                ):
                    self.send_text(
                        f"Feature file not found: {buildings_source}",
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                buildings = store.read_buildings(
                    buildings_source,
                    h3_assignments=assignments,
                )
                self.send_json(filter_buildings_for_assignments(buildings, review_config))
                return

            if path == "/api/annotations":
                annotation_store = self.requested_annotation_store()
                if annotation_store is None:
                    self.send_text('Workflow mode is not enabled', HTTPStatus.BAD_REQUEST)
                    return
                self.send_json(annotation_store.read_annotations())
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

            annotation_store = self.requested_annotation_store()
            if annotation_store is None:
                self.send_text('Workflow mode is not enabled', HTTPStatus.BAD_REQUEST)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length) or b"{}")

            if not payload.get("id"):
                self.send_text("Missing feature id", HTTPStatus.BAD_REQUEST)
                return

            if assigned_feature_ids is not None and str(payload['id']) not in assigned_feature_ids:
                self.send_text('Feature is not assigned to this user', HTTPStatus.FORBIDDEN)
                return

            if review_config is not None:
                if payload.get('annotation_label') not in review_config.annotation_labels:
                    self.send_text('Invalid annotation label', HTTPStatus.BAD_REQUEST)
                    return
                payload['reviewer'] = review_config.user

            self.send_json(annotation_store.write_annotation(payload))

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def build_parser() -> argparse.ArgumentParser:
    '''*!*! Build command-line arguments for fallback and YAML modes.'''

    parser = argparse.ArgumentParser(description="Run the local map-feature annotation app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        '--yaml',
        type=Path,
        help=(
            'Project YAML. Relative paths inside it are resolved from the YAML '
            'directory; when omitted, the in-app Settings panel is available.'
        ),
    )
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
    '''*!*! Load configuration and serve the annotation application.'''

    args = build_parser().parse_args()
    try:
        review_config = load_review_config(args.yaml) if args.yaml else None
    except ConfigError as error:
        raise SystemExit(f'Invalid project configuration: {error}') from error

    if review_config is not None:
        try:
            prepare_configured_features(review_config)
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f'Could not prepare Overture buildings: {error}') from error

    buildings_path = review_config.features_path if review_config else args.buildings
    #TODO: Is this where postGIS sconnection goes?
    annotations_input_path = None
    annotations_path = args.annotations
    # this reads modes from config
    mode_stores = None
    if review_config:
        mode_stores = {}
        if 'annotation' in review_config.modes:
            mode_stores['annotation'] = QaqcStore(
                buildings_path,
                review_config.annotations_output,
                review_config.annotations_input,
            )
        if 'qaqc' in review_config.modes:
            mode_stores['qaqc'] = QaqcStore(
                buildings_path,
                review_config.qaqc_output,
                review_config.qaqc_input,
            )
        if not mode_stores:
            raise SystemExit(
                'Editing-only projects are not supported yet; include annotation or qaqc mode'
            )
        first_mode = next(mode for mode in review_config.modes if mode in mode_stores)
        first_store = mode_stores[first_mode]
        annotations_input_path = first_store.annotations_input_path
        annotations_path = first_store.annotations_path

    if annotations_path is None:
        raise SystemExit('The active workflow has no annotation output path')

    store = QaqcStore(buildings_path, annotations_path, annotations_input_path)
    try:
        handler = make_handler(store, review_config, mode_stores)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f'Could not load project features: {error}') from error
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f'Annotation app: http://{args.host}:{args.port}')
    if review_config:
        modes_text = ', '.join(review_config.modes)
        print(f'Configuration: {review_config.source_path}')
        print(f'Project: {review_config.project_name}')
        print(f'User: {review_config.user}')
        print(f'Modes: {modes_text}')
        print(f'Assigned H3 cells: {len(review_config.todo_h3_indexes):,}')
    print(f'Features: {store.buildings_path}')
    if mode_stores:
        for mode, mode_store in mode_stores.items():
            if mode_store.annotations_input_path:
                print(f'{mode.title()} input: {mode_store.annotations_input_path}')
            print(f'{mode.title()} output: {mode_store.annotations_path}')
    else:
        if store.annotations_input_path:
            print(f'Existing annotations: {store.annotations_input_path}')
        print(f'Annotations: {store.annotations_path}')
    server.serve_forever()


if __name__ == "__main__":
    main()
