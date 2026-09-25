// *!*! Load runtime values from Python so this file remains static and independently maintainable.
const configResponse = await fetch('/api/config');
if (!configResponse.ok) {
  throw new Error(await configResponse.text());
}
const {
  defaultLabelField,
  defaultAnnotationLabels,
  defaultCogPath,
  defaultConfidenceField,
  configuredProjectName,
  configuredFeatureIdField,
  configuredH3Prefix,
  configuredUser,
  workflowModes,
  todoH3Indexes,
} = await configResponse.json();

// *!*! Leaflet and H3 are loaded by index.html before this module executes.
const map = L.map("map", { preferCanvas: true, maxZoom: 23 });
const h3ZoomLayers = [
  { minZoom: 0, maxZoom: 8, resolution: 5 },
  { minZoom: 9, maxZoom: 10, resolution: 6 },
  { minZoom: 11, maxZoom: 12, resolution: 7 },
  { minZoom: 13, maxZoom: 14, resolution: 8 },
  { minZoom: 15, maxZoom: 16, resolution: 9 },
  { minZoom: 17, maxZoom: 99, resolution: 10 },
];
// *!*! Cache stable DOM references once; feature and H3 layers are replaced frequently.
const reviewerInput = document.getElementById("reviewer");
const saveButton = document.getElementById("save");
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
const projectName = document.getElementById('project-name');
const workflowSummary = document.getElementById('workflow-summary');
const workflowModeSection = document.getElementById('workflow-mode-section');
const workflowModeButtons = document.getElementById('workflow-mode-buttons');
const labelHeading = document.getElementById('label-heading');

// *!*! Mutable state tracks the current feature, selected H3 cell, and rendered map layers.
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
// *!*! Server-provided YAML values are the only runtime configuration source.
const labelField = defaultLabelField;
const annotationLabels = defaultAnnotationLabels;
const cogPath = defaultCogPath;
const confidenceField = defaultConfidenceField;
const confidenceFilter = 0;
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

projectName.textContent = configuredProjectName || 'Feature Annotator';
workflowSummary.textContent = `${workflowModes.join(' + ')} | ${todoAssignments.length || 'all'} H3 assignment${todoAssignments.length === 1 ? '' : 's'}`;
reviewerInput.value = configuredUser;
reviewerInput.readOnly = true;

// *!*! Annotation and QA/QC share map navigation but load independent review records.
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

// *!*! Confidence normalization accepts either fractions or percentages from model outputs.
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

// *!*! COG metadata establishes map bounds before Leaflet requests server-rendered tiles.
async function updateCogLayer() {
  if (cogLayer) {
    map.removeLayer(cogLayer);
    cogLayer = null;
  }
  if (!cogPath) {
    return;
  }

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
  map.fitBounds(bounds, { padding: [24, 24] });
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

// *!*! Build only the H3 cells visible at the current logical review resolution.
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

// *!*! Feature styling distinguishes saved reviews while preserving a clear active selection.
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

// *!*! Cell-local navigation wraps so reviewers can move repeatedly through an assignment.
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

// *!*! The active mode selects its own server-side annotation store.
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
  const response = await fetch('/api/buildings');
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

map.on("zoomend", () => {
  updateH3Grid();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    clearSelectedCell();
  }
});

// *!*! Save the current review, redraw completion state, then advance within the H3 cell.
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
    feature_version_seen: selectedFeature.version,
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

// *!*! Initial data loads precede imagery so vector review remains usable if a COG fails.
async function init() {
  await loadAnnotations();
  await loadBuildings();
  try {
    await updateCogLayer();
  }
  catch (error) {
    alert(error.message);
  }
}

init().catch((error) => {
  document.body.textContent = error.message;
});
