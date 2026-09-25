// *!*! Load runtime values from Python so this file remains static and independently maintainable.
const configResponse = await fetch('/api/config');
if (!configResponse.ok) {
  throw new Error(await configResponse.text());
}
const {
  defaultAnnotationLabels,
  defaultCogPath,
  configuredProjectName,
  configuredUser,
  workflowModes,
  todoH3Indexes,
  layers,
} = await configResponse.json();

// *!*! Leaflet, Leaflet-Geoman, and H3 are loaded by index.html before this module executes.
const map = L.map('map', { preferCanvas: true, maxZoom: 23 });
const POINT_MIN_H3_RESOLUTION = 8;
const h3ZoomLayers = [
  { minZoom: 0, maxZoom: 8, resolution: 5 },
  { minZoom: 9, maxZoom: 10, resolution: 6 },
  { minZoom: 11, maxZoom: 12, resolution: 7 },
  { minZoom: 13, maxZoom: 14, resolution: 8 },
  { minZoom: 15, maxZoom: 16, resolution: 9 },
  { minZoom: 17, maxZoom: 99, resolution: 10 },
];

// *!*! Cache stable DOM references once; feature and H3 layers are replaced frequently.
const reviewerInput = document.getElementById('reviewer');
const saveButton = document.getElementById('save');
const selectedCell = document.getElementById('selected-cell');
const selectedCellResolution = document.getElementById('selected-cell-resolution');
const selectedCellProgress = document.getElementById('selected-cell-progress');
const selectedId = document.getElementById('selected-id');
const toggleFeatureOutline = document.getElementById('toggle-feature-outline');
const correctLabelButtons = document.getElementById('correct-label-buttons');
const qaNotes = document.getElementById('qa-notes');
const totalCount = document.getElementById('total-count');
const reviewedCount = document.getElementById('reviewed-count');
const openCount = document.getElementById('open-count');
const previousFeatureButton = document.getElementById('previous-building');
const nextOpenFeatureButton = document.getElementById('next-open-building');
const nextFeatureButton = document.getElementById('next-building');
const projectName = document.getElementById('project-name');
const workflowSummary = document.getElementById('workflow-summary');
const layerList = document.getElementById('layer-list');
const annotationLabelSection = document.getElementById('annotation-label-section');
const annotationNotesSection = document.getElementById('annotation-notes-section');

// *!*! Mutable state tracks active work, selected features, and rendered map layers.
let selectedFeature = null;
let selectedCellId = null;
let selectedCellRes = null;
let selectedCellFeatureIds = [];
let selectedCellPosition = 0;
let buildingsData = null;
let h3GridLayer = null;
let cogLayer = null;
let selectedFeatureMarker = null;
let selectedFeatureLayer = null;
let selectedOutlineVisible = false;
let featureLayersById = {};
let featureLayerGroups = new Map();
let featuresByLayer = new Map();
let featuresByH3 = new Map();
let annotations = {};
let selectedCorrectLabel = '';
let awaitingPointPlacement = false;
let draftGeometry = null;
let geometryDirty = false;

const annotationLabels = defaultAnnotationLabels;
const cogPath = defaultCogPath;
const confidenceFilter = 0;
const todoAssignments = todoH3Indexes.map((index) => ({
  index: String(index),
  resolution: h3.getResolution ? h3.getResolution(index) : h3.h3GetResolution(index),
}));
const confidenceFilters = [
  { value: 'all', maxRank: Infinity },
  { value: 'very_low', maxRank: 0 },
  { value: 'low', maxRank: 1 },
  { value: 'medium', maxRank: 2 },
  { value: 'high', maxRank: 3 },
];
const layerStates = new Map(layers.map((layer) => [layer.id, { visible: true }]));

function layerSupportsAnnotation(layer) {
  return workflowModes.includes('annotation') && layer.modes.includes('annotation');
}

function layerSupportsPointEditing(layer) {
  return Boolean(
    workflowModes.includes('editing') &&
    layer.modes.includes('editing') &&
    layer.geometryTypes.some((type) => ['Point', 'MultiPoint'].includes(type)) &&
    layer.editing &&
    layer.editing.move
  );
}

function layerSupportsPolygonEditing(layer) {
  return Boolean(
    workflowModes.includes('editing') &&
    layer.modes.includes('editing') &&
    layer.geometryTypes.some((type) => ['Polygon', 'MultiPolygon'].includes(type)) &&
    layer.editing &&
    (layer.editing.move || layer.editing.reshape)
  );
}

function availableTools(layer) {
  const tools = [];
  if (layerSupportsAnnotation(layer)) {
    tools.push({ mode: 'annotation', label: 'Annotate' });
  }
  if (layerSupportsPolygonEditing(layer)) {
    tools.push({ mode: 'polygon-editing', label: 'Edit polygons' });
  }
  if (layerSupportsPointEditing(layer)) {
    tools.push({ mode: 'point-editing', label: 'Edit points' });
  }
  return tools;
}

function firstAvailableTool() {
  for (const layer of layers) {
    const tool = availableTools(layer)[0];
    if (tool) {
      return { layerId: layer.id, mode: tool.mode };
    }
  }
  throw new Error('No configured layer mode is available');
}

let activeTool = firstAvailableTool();

projectName.textContent = configuredProjectName || 'Feature Annotator';
workflowSummary.textContent = `${layers.length} layer${layers.length === 1 ? '' : 's'} | ${todoAssignments.length || 'all'} H3 assignment${todoAssignments.length === 1 ? '' : 's'}`;
reviewerInput.value = configuredUser;
reviewerInput.readOnly = true;

L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}',
  {
    attribution: 'Tiles &copy; Esri',
    maxNativeZoom: 16,
    maxZoom: 23,
  }
).addTo(map);

function isAnnotationMode() {
  return activeTool.mode === 'annotation';
}

function isPointEditingMode() {
  return activeTool.mode === 'point-editing';
}

function isPolygonEditingMode() {
  return activeTool.mode === 'polygon-editing';
}

function activeLayerDefinition() {
  return layers.find((layer) => layer.id === activeTool.layerId) || null;
}

function featureId(feature) {
  const value = feature ? feature.id : null;
  return value == null ? '' : String(value);
}

// *!*! Layer identity prevents equal IDs in separate Parquets from colliding.
function featureKey(feature) {
  return JSON.stringify([String(feature.layer_id || ''), featureId(feature)]);
}

function h3IndexKey(layerId, resolution, cell) {
  return JSON.stringify([String(layerId), Number(resolution), String(cell)]);
}

function layerDefinition(feature) {
  return layers.find((entry) => entry.id === feature.layer_id) || null;
}

function layerField(feature, role) {
  const layer = layerDefinition(feature);
  return layer && layer.fields ? (layer.fields[role] || '') : '';
}

function featureIsPoint(feature) {
  return Boolean(feature && feature.geometry && ['Point', 'MultiPoint'].includes(feature.geometry.type));
}

function featureIsPolygon(feature) {
  return Boolean(feature && feature.geometry && ['Polygon', 'MultiPolygon'].includes(feature.geometry.type));
}

function annotationFor(feature) {
  return annotations[featureKey(feature)] || {};
}

function annotationLabelFor(row) {
  return row ? (row.annotation_label || row.qa_correct_class || '') : '';
}

function annotationIsComplete(row) {
  return Boolean(annotationLabelFor(row) || (row && row.qa_status));
}

function isReviewed(featureKeyValue) {
  return annotationIsComplete(annotations[featureKeyValue]);
}

function labelFor(feature) {
  const labelField = layerField(feature, 'predictedClass');
  if (!labelField) {
    return 'none';
  }
  const value = feature.properties[labelField];
  return value == null || value === '' ? 'none' : String(value);
}

function configuredAnnotationLabels() {
  return annotationLabels
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);
}

function updateSaveButton() {
  saveButton.textContent = isAnnotationMode() ? 'Save Annotation' : 'Save Geometry';
  saveButton.disabled = isAnnotationMode()
    ? !selectedFeature || !selectedCorrectLabel
    : !selectedFeature || !geometryDirty;
}

function setCorrectLabel(value) {
  selectedCorrectLabel = value || '';
  Array.from(correctLabelButtons.children).forEach((button) => {
    button.classList.toggle('active', button.dataset.value === selectedCorrectLabel);
  });
  updateSaveButton();
}

function updateCorrectLabelOptions(selectedValue = '') {
  const values = Array.from(new Set(configuredAnnotationLabels()));
  if (selectedValue && !values.includes(selectedValue)) {
    values.push(selectedValue);
  }
  if (!values.includes('unknown')) {
    values.push('unknown');
  }
  correctLabelButtons.replaceChildren();
  values.forEach((value) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.value = value;
    button.textContent = value;
    button.addEventListener('click', () => setCorrectLabel(value));
    correctLabelButtons.appendChild(button);
  });
  setCorrectLabel(values.includes(selectedValue) ? selectedValue : '');
}

// *!*! Confidence normalization accepts either fractions or percentages from model outputs.
function classConfidence(feature) {
  const confidenceField = layerField(feature, 'confidence');
  if (!confidenceField) {
    return null;
  }
  const value = feature.properties[confidenceField];
  if (value == null || value === '') {
    return null;
  }
  let probability = Number(value);
  if (!Number.isFinite(probability)) {
    return null;
  }
  if (probability > 1) {
    probability = probability / 100;
  }
  if (confidenceField.toLowerCase().includes('damaged') && labelFor(feature).toLowerCase() === 'undamaged') {
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
    return { rank: 0 };
  }
  if (confidence < 0.75) {
    return { rank: 1 };
  }
  if (confidence < 0.9) {
    return { rank: 2 };
  }
  return { rank: 3 };
}

function featurePassesConfidenceFilter(feature) {
  const filter = confidenceFilters[confidenceFilter] || confidenceFilters[0];
  const confidenceField = layerField(feature, 'confidence');
  if (filter.value === 'all' || !confidenceField) {
    return true;
  }
  const category = confidenceCategory(feature);
  return category ? category.rank <= filter.maxRank : false;
}

function featureIsAssigned(feature) {
  if (!todoAssignments.length) {
    return true;
  }
  return todoAssignments.some((assignment) => (
    String((feature.h3 || {})[String(assignment.resolution)] || '') === assignment.index
  ));
}

// *!*! Build reusable indexes once instead of rescanning every feature for every H3 cell.
function indexFeatures() {
  featuresByLayer = new Map(layers.map((layer) => [layer.id, []]));
  featuresByH3 = new Map();
  buildingsData.features.forEach((feature) => {
    if (!featuresByLayer.has(feature.layer_id)) {
      featuresByLayer.set(feature.layer_id, []);
    }
    featuresByLayer.get(feature.layer_id).push(feature);
    Object.entries(feature.h3 || {}).forEach(([resolution, cell]) => {
      const key = h3IndexKey(feature.layer_id, resolution, cell);
      if (!featuresByH3.has(key)) {
        featuresByH3.set(key, []);
      }
      featuresByH3.get(key).push(feature);
    });
  });
}

function activeFeatures() {
  return (featuresByLayer.get(activeTool.layerId) || []).filter((feature) => (
    featureIsAssigned(feature) && featurePassesConfidenceFilter(feature)
  ));
}

function featuresInActiveCell(cell, resolution) {
  const indexed = featuresByH3.get(h3IndexKey(activeTool.layerId, resolution, cell)) || [];
  return indexed.filter((feature) => (
    featureIsAssigned(feature) && featurePassesConfidenceFilter(feature)
  ));
}

function visibleFeaturesForLayer(layer) {
  const source = featuresByLayer.get(layer.id) || [];
  return source.filter((feature) => {
    if (!featureIsAssigned(feature)) {
      return false;
    }
    if (!featureIsPoint(feature)) {
      return true;
    }
    return Boolean(
      selectedCellId &&
      selectedCellRes != null &&
      String((feature.h3 || {})[String(selectedCellRes)] || '') === selectedCellId
    );
  });
}

function setPointPlacement(active) {
  awaitingPointPlacement = Boolean(active);
  document.body.classList.toggle('point-placement', awaitingPointPlacement);
}

function configureModeControls() {
  const annotation = isAnnotationMode();
  annotationLabelSection.hidden = !annotation;
  annotationNotesSection.hidden = !annotation;
  toggleFeatureOutline.hidden = !annotation;
  nextOpenFeatureButton.hidden = !annotation;
  updateSaveButton();
}

// *!*! Each mode button selects one layer and makes it the sole interactive top layer.
function selectLayerTool(layerId, mode) {
  const layer = layers.find((entry) => entry.id === layerId);
  if (!layer || !availableTools(layer).some((tool) => tool.mode === mode)) {
    return;
  }
  layerStates.get(layerId).visible = true;
  activeTool = { layerId: layerId, mode: mode };
  selectedCellId = null;
  selectedCellRes = null;
  selectedCellFeatureIds = [];
  selectedCellPosition = 0;
  clearSelectedFeature();
  configureModeControls();
  renderLayerPanel();
  renderFeatureLayers(false, true);
}

function renderLayerPanel() {
  layerList.replaceChildren();
  layers.forEach((layer) => {
    const state = layerStates.get(layer.id);
    const row = document.createElement('div');
    row.className = 'layer-row';
    const heading = document.createElement('div');
    heading.className = 'layer-heading-row';
    const visibility = document.createElement('input');
    visibility.type = 'checkbox';
    visibility.checked = state.visible;
    visibility.setAttribute('aria-label', `Show ${layer.name}`);
    visibility.addEventListener('change', () => {
      state.visible = visibility.checked;
      if (!state.visible && activeTool.layerId === layer.id) {
        const fallback = layers.find((entry) => (
          layerStates.get(entry.id).visible && availableTools(entry).length
        ));
        if (fallback) {
          activeTool = { layerId: fallback.id, mode: availableTools(fallback)[0].mode };
          selectedCellId = null;
          selectedCellRes = null;
        }
        else {
          state.visible = true;
        }
      }
      clearSelectedFeature();
      configureModeControls();
      renderLayerPanel();
      renderFeatureLayers(false, true);
    });
    const name = document.createElement('span');
    name.className = 'layer-name';
    name.textContent = layer.name;
    name.title = layer.name;
    heading.append(visibility, name);

    const actions = document.createElement('div');
    actions.className = 'layer-actions';
    availableTools(layer).forEach((tool) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = tool.label;
      button.classList.toggle(
        'active',
        activeTool.layerId === layer.id && activeTool.mode === tool.mode
      );
      button.addEventListener('click', () => selectLayerTool(layer.id, tool.mode));
      actions.appendChild(button);
    });
    row.append(heading, actions);
    layerList.appendChild(row);
  });
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
      attribution: 'COG imagery',
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
  const layer = h3ZoomLayers.find((entry) => (
    numericZoom >= entry.minZoom && numericZoom <= entry.maxZoom
  ));
  let resolution = layer ? layer.resolution : h3ZoomLayers[h3ZoomLayers.length - 1].resolution;
  const activeLayer = activeLayerDefinition();
  if (activeLayer && activeLayer.geometryTypes.some((type) => ['Point', 'MultiPoint'].includes(type))) {
    resolution = Math.max(resolution, POINT_MIN_H3_RESOLUTION);
  }
  return resolution;
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
  const ids = featuresInActiveCell(cell, resolution).map(featureKey).filter(Boolean);
  const reviewed = isAnnotationMode() ? ids.filter(isReviewed).length : 0;
  return {
    total: ids.length,
    reviewed: reviewed,
    completion: isAnnotationMode() && ids.length ? reviewed / ids.length : 0,
    ids: ids,
  };
}

function h3CompletionColor(completion) {
  if (completion >= 1) {
    return '#1a9850';
  }
  if (completion > 0) {
    return '#fee08b';
  }
  return '#e5e7eb';
}

// *!*! Build H3 cells only for the selected layer-mode using the precomputed index.
function h3GridGeoJson(resolution) {
  const cells = new Set(
    activeFeatures()
      .map((feature) => (feature.h3 || {})[String(resolution)])
      .filter(Boolean)
  );
  const visibleCells = selectedCellId && selectedCellRes === resolution
    ? [selectedCellId]
    : Array.from(cells);
  return {
    type: 'FeatureCollection',
    features: visibleCells.map((cell) => {
      const stats = h3CellStats(cell, resolution);
      return {
        type: 'Feature',
        properties: {
          h3_cell: cell,
          resolution: resolution,
          total: stats.total,
          reviewed: stats.reviewed,
          completion: stats.completion,
          completion_pct: `${Math.round(100 * stats.completion)}%`,
        },
        geometry: {
          type: 'Polygon',
          coordinates: [h3BoundaryLatLngs(cell).map((coord) => [coord[1], coord[0]])],
        },
      };
    }),
  };
}

function styleH3Cell(feature) {
  const isSelected = (
    feature.properties.h3_cell === selectedCellId &&
    feature.properties.resolution === selectedCellRes
  );
  if (isSelected) {
    return {
      color: '#ff1493',
      fill: false,
      fillColor: '#ff1493',
      fillOpacity: 0,
      opacity: 1,
      weight: 3.2,
    };
  }
  return {
    color: '#475569',
    fillColor: h3CompletionColor(feature.properties.completion),
    fillOpacity: 0.38,
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
        layer.on('click', () => selectCellFeature(feature));
      }
      const detail = isAnnotationMode()
        ? `${feature.properties.reviewed}/${feature.properties.total} annotated (${feature.properties.completion_pct})`
        : `${feature.properties.total} features`;
      layer.bindTooltip(`H3 r${feature.properties.resolution}<br>${detail}`, { sticky: false });
    },
  }).addTo(map);
  h3GridLayer.bringToBack();
  bringActiveLayerToFront();
}

// *!*! High-contrast point fills remain legible over both dark and light aerial imagery.
function styleFeature(feature, activeLayer) {
  const complete = annotationIsComplete(annotationFor(feature));
  let style;
  if (featureIsPoint(feature)) {
    style = complete
      ? { color: '#052e16', fillColor: '#22c55e', fillOpacity: 0.92, opacity: 1, weight: 2 }
      : { color: '#111827', fillColor: '#fbbf24', fillOpacity: 0.96, opacity: 1, weight: 2 };
  }
  else if (complete) {
    style = { color: '#14532d', fillColor: '#15803d', fillOpacity: 0.42, opacity: 1, weight: 1.4 };
  }
  else {
    style = { color: '#475569', fillColor: '#64748b', fillOpacity: 0.22, opacity: 1, weight: 0.8 };
  }
  if (!activeLayer) {
    style.opacity = 0.52;
    style.fillOpacity *= 0.55;
  }
  const selected = selectedFeature && featureKey(feature) === featureKey(selectedFeature);
  if (selected && isAnnotationMode()) {
    style.color = '#39ff14';
    style.fillOpacity = 0;
    style.opacity = selectedOutlineVisible ? 1 : 0;
    style.weight = selectedOutlineVisible ? 2.4 : 0;
  }
  return style;
}

function disableSelectedGeometryEditing() {
  if (!selectedFeatureLayer || !selectedFeatureLayer.pm) {
    return;
  }
  selectedFeatureLayer.pm.disable();
  if (selectedFeatureLayer.pm.layerDragEnabled()) {
    selectedFeatureLayer.pm.disableLayerDrag();
  }
}

function capturePolygonDraft() {
  if (!selectedFeatureLayer || !featureIsPolygon(selectedFeature)) {
    return;
  }
  draftGeometry = selectedFeatureLayer.toGeoJSON().geometry;
  geometryDirty = true;
  updateSelectedFeatureMarker();
  updateSaveButton();
}

function enablePolygonEditing() {
  if (!isPolygonEditingMode() || !selectedFeatureLayer || !selectedFeatureLayer.pm) {
    return;
  }
  const layer = activeLayerDefinition();
  if (layer.editing.reshape) {
    selectedFeatureLayer.pm.enable({ allowSelfIntersection: false });
  }
  if (layer.editing.move) {
    selectedFeatureLayer.pm.enableLayerDrag();
  }
  selectedFeatureLayer.on('pm:edit', capturePolygonDraft);
  selectedFeatureLayer.on('pm:dragend', capturePolygonDraft);
}

function updateSelectedOutlineButton() {
  toggleFeatureOutline.disabled = !selectedFeature;
  toggleFeatureOutline.textContent = selectedOutlineVisible ? 'Hide Outline' : 'Show Outline';
}

function restyleFeatureLayers() {
  featureLayerGroups.forEach((group, layerId) => {
    group.setStyle((feature) => styleFeature(feature, layerId === activeTool.layerId));
  });
  if (selectedFeatureMarker) {
    selectedFeatureMarker.bringToFront();
  }
}

function toggleSelectedOutline() {
  if (!selectedFeature || !isAnnotationMode()) {
    return;
  }
  selectedOutlineVisible = !selectedOutlineVisible;
  updateSelectedOutlineButton();
  restyleFeatureLayers();
}

toggleFeatureOutline.addEventListener('click', toggleSelectedOutline);

function geometryCoordinates(geometry) {
  if (!geometry) {
    return [];
  }
  if (geometry.type === 'Polygon') {
    return geometry.coordinates.flat(1);
  }
  if (geometry.type === 'MultiPolygon') {
    return geometry.coordinates.flat(2);
  }
  if (geometry.type === 'Point') {
    return [geometry.coordinates];
  }
  if (geometry.type === 'MultiPoint') {
    return geometry.coordinates;
  }
  return [];
}

function updateSelectedFeatureMarker() {
  if (selectedFeatureMarker) {
    map.removeLayer(selectedFeatureMarker);
    selectedFeatureMarker = null;
  }
  if (!selectedFeature || !selectedFeatureLayer) {
    return;
  }
  const center = selectedFeatureLayer.getBounds().getCenter();
  if (featureIsPoint(selectedFeature)) {
    selectedFeatureMarker = L.circleMarker(center, {
      radius: 11,
      color: '#39ff14',
      opacity: 1,
      fill: false,
      weight: 3,
      interactive: false,
    }).addTo(map);
    selectedFeatureMarker.bringToFront();
    return;
  }
  const geometry = draftGeometry || selectedFeature.geometry;
  const maxDistance = geometryCoordinates(geometry)
    .map((coordinate) => map.distance(center, L.latLng(coordinate[1], coordinate[0])))
    .reduce((largest, distance) => Math.max(largest, distance), 0);
  selectedFeatureMarker = L.circle(center, {
    radius: Math.max(0.5, 1.1 * maxDistance),
    color: '#39ff14',
    opacity: 1,
    fill: false,
    weight: 3,
    interactive: false,
  }).addTo(map);
  selectedFeatureMarker.bringToFront();
}

function updateCounts() {
  const ids = activeFeatures().map(featureKey);
  const reviewed = ids.filter(isReviewed).length;
  totalCount.textContent = ids.length;
  reviewedCount.textContent = reviewed;
  openCount.textContent = Math.max(ids.length - reviewed, 0);
}

function clearSelectedFeature() {
  disableSelectedGeometryEditing();
  setPointPlacement(false);
  selectedFeature = null;
  selectedFeatureLayer = null;
  draftGeometry = null;
  geometryDirty = false;
  selectedId.textContent = 'none';
  setCorrectLabel('');
  qaNotes.value = '';
  selectedOutlineVisible = false;
  updateSelectedOutlineButton();
  updateSelectedFeatureMarker();
  updateSaveButton();
}

function selectFeature(feature, layer, activateEditing = false) {
  disableSelectedGeometryEditing();
  selectedFeature = feature;
  selectedFeatureLayer = layer || featureLayersById[featureKey(feature)] || null;
  draftGeometry = null;
  geometryDirty = false;
  selectedOutlineVisible = false;
  const annotation = annotationFor(feature);
  selectedId.textContent = `${feature.layer_name || feature.layer_id}: ${featureId(feature)}`;
  updateSelectedOutlineButton();
  updateCorrectLabelOptions(annotationLabelFor(annotation));
  qaNotes.value = annotation.qa_notes || '';
  restyleFeatureLayers();
  updateSelectedFeatureMarker();
  updateCellProgress();
  setPointPlacement(
    activateEditing && isPointEditingMode() && featureIsPoint(feature)
  );
  if (activateEditing && isPolygonEditingMode() && featureIsPolygon(feature)) {
    enablePolygonEditing();
  }
  updateSaveButton();
}

function clearSelectedCell() {
  selectedCellId = null;
  selectedCellRes = null;
  selectedCellFeatureIds = [];
  selectedCellPosition = 0;
  renderFeatureLayers(false, true);
}

function selectCellFeature(feature) {
  selectedCellId = feature.properties.h3_cell;
  selectedCellRes = feature.properties.resolution;
  selectedCellFeatureIds = h3CellStats(selectedCellId, selectedCellRes).ids;
  const nextOpenIndex = isAnnotationMode()
    ? selectedCellFeatureIds.findIndex((id) => !isReviewed(id))
    : 0;
  selectedCellPosition = nextOpenIndex >= 0 ? nextOpenIndex : 0;
  renderFeatureLayers(false, true);
  selectFeatureAtCellPosition(selectedCellPosition, false);
}

// *!*! Cell-local navigation stays scoped to the active layer and selected mode.
function selectFeatureAtCellPosition(position, panToFeature) {
  if (!selectedCellFeatureIds.length) {
    return;
  }
  selectedCellPosition = (
    position + selectedCellFeatureIds.length
  ) % selectedCellFeatureIds.length;
  const key = selectedCellFeatureIds[selectedCellPosition];
  const layer = featureLayersById[key];
  if (!layer) {
    return;
  }
  selectFeature(layer.feature, layer, !isAnnotationMode());
  if (panToFeature) {
    map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 21 });
  }
}

function nextOpenFeatureIndex() {
  if (!selectedCellFeatureIds.length) {
    return -1;
  }
  for (let offset = 1; offset <= selectedCellFeatureIds.length; offset++) {
    const index = (selectedCellPosition + offset) % selectedCellFeatureIds.length;
    if (!isReviewed(selectedCellFeatureIds[index])) {
      return index;
    }
  }
  return -1;
}

function finishSelectedCellReview() {
  clearSelectedFeature();
  clearSelectedCell();
  const minZoom = Number.isFinite(map.getMinZoom()) ? map.getMinZoom() : 0;
  map.setZoom(Math.max(map.getZoom() - 2, minZoom));
}

function updateCellProgress() {
  if (!selectedCellId || !selectedCellFeatureIds.length) {
    selectedCell.textContent = 'none';
    selectedCellResolution.textContent = 'none';
    selectedCellProgress.textContent = 'none';
    previousFeatureButton.disabled = true;
    nextOpenFeatureButton.disabled = true;
    nextFeatureButton.disabled = true;
    return;
  }
  const reviewed = selectedCellFeatureIds.filter(isReviewed).length;
  selectedCell.textContent = selectedCellId;
  selectedCellResolution.textContent = `r${selectedCellRes}`;
  selectedCellProgress.textContent = isAnnotationMode()
    ? `${reviewed}/${selectedCellFeatureIds.length} annotated, feature ${selectedCellPosition + 1}/${selectedCellFeatureIds.length}`
    : `feature ${selectedCellPosition + 1}/${selectedCellFeatureIds.length}`;
  previousFeatureButton.disabled = selectedCellFeatureIds.length < 2;
  nextOpenFeatureButton.disabled = reviewed >= selectedCellFeatureIds.length;
  nextFeatureButton.disabled = selectedCellFeatureIds.length < 2;
}

function onEachFeature(feature, layer, interactive) {
  const id = featureId(feature);
  const key = featureKey(feature);
  if (interactive && id && feature.layer_id) {
    featureLayersById[key] = layer;
  }
  if (interactive) {
    layer.on('click', (event) => {
      if (event.originalEvent) {
        L.DomEvent.stopPropagation(event.originalEvent);
      }
      selectFeature(feature, layer, !isAnnotationMode());
      layer.bringToFront();
    });
  }
  layer.bindTooltip(`${feature.layer_name || feature.layer_id}<br>ID: ${id}`, { sticky: false });
}

function bringActiveLayerToFront() {
  const activeGroup = featureLayerGroups.get(activeTool.layerId);
  if (activeGroup) {
    activeGroup.bringToFront();
  }
  if (selectedFeatureMarker) {
    selectedFeatureMarker.bringToFront();
  }
}

function addFeatureLayer(layer) {
  const active = layer.id === activeTool.layerId;
  const data = {
    type: 'FeatureCollection',
    features: visibleFeaturesForLayer(layer),
  };
  const group = L.geoJSON(data, {
    interactive: active,
    style: (feature) => styleFeature(feature, active),
    pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
      ...styleFeature(feature, active),
      interactive: active,
      radius: active ? 6 : 4,
    }),
    onEachFeature: (feature, featureLayer) => onEachFeature(feature, featureLayer, active),
  }).addTo(map);
  featureLayerGroups.set(layer.id, group);
}

function renderFeatureLayers(fitToFeatures, preserveCell = false) {
  featureLayerGroups.forEach((group) => map.removeLayer(group));
  featureLayerGroups = new Map();
  if (h3GridLayer) {
    map.removeLayer(h3GridLayer);
    h3GridLayer = null;
  }
  clearSelectedFeature();
  if (!preserveCell) {
    selectedCellId = null;
    selectedCellRes = null;
    selectedCellFeatureIds = [];
    selectedCellPosition = 0;
  }
  else if (selectedCellId && selectedCellRes != null) {
    selectedCellFeatureIds = h3CellStats(selectedCellId, selectedCellRes).ids;
    selectedCellPosition = Math.min(
      selectedCellPosition,
      Math.max(selectedCellFeatureIds.length - 1, 0)
    );
  }
  featureLayersById = {};
  updateCorrectLabelOptions();
  layers
    .filter((layer) => layerStates.get(layer.id).visible && layer.id !== activeTool.layerId)
    .forEach(addFeatureLayer);
  const activeLayer = activeLayerDefinition();
  if (activeLayer && layerStates.get(activeLayer.id).visible) {
    addFeatureLayer(activeLayer);
  }
  if (fitToFeatures && !cogPath) {
    const activeGroup = featureLayerGroups.get(activeTool.layerId);
    if (activeGroup && activeGroup.getLayers().length) {
      map.fitBounds(activeGroup.getBounds(), { padding: [24, 24] });
    }
  }
  updateH3Grid();
  updateCounts();
  updateCellProgress();
}

async function loadAnnotations() {
  if (!workflowModes.includes('annotation')) {
    annotations = {};
    return;
  }
  const response = await fetch('/api/annotations?mode=annotation');
  if (!response.ok) {
    throw new Error(await response.text());
  }
  annotations = await response.json();
}

async function loadFeatures() {
  const response = await fetch('/api/buildings');
  if (!response.ok) {
    throw new Error(await response.text());
  }
  buildingsData = await response.json();
  indexFeatures();
  renderFeatureLayers(true);
}

function stagePointPlacement(latlng) {
  if (!selectedFeature || !featureIsPoint(selectedFeature) || !selectedFeatureLayer) {
    return;
  }
  selectedFeatureLayer.setLatLng(latlng);
  draftGeometry = {
    type: 'Point',
    coordinates: [latlng.lng, latlng.lat],
  };
  geometryDirty = true;
  setPointPlacement(false);
  updateSelectedFeatureMarker();
  updateSaveButton();
}

// *!*! Save geometry with optimistic version checking, then advance the green marker.
async function saveGeometry() {
  if (!selectedFeature || !draftGeometry || !geometryDirty) {
    return;
  }
  const feature = selectedFeature;
  const response = await fetch('/api/features', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: featureId(feature),
      layer_id: feature.layer_id,
      expected_version: feature.version,
      geometry: draftGeometry,
    }),
  });
  if (!response.ok) {
    alert(await response.text());
    return;
  }
  Object.assign(feature, await response.json());
  indexFeatures();
  const nextPosition = selectedCellFeatureIds.length > 1
    ? selectedCellPosition + 1
    : selectedCellPosition;
  renderFeatureLayers(false, true);
  selectFeatureAtCellPosition(nextPosition, selectedCellFeatureIds.length > 1);
}

async function saveAnnotation() {
  if (!selectedFeature) {
    return;
  }
  const payload = {
    id: featureId(selectedFeature),
    layer_id: selectedFeature.layer_id,
    predicted_class: labelFor(selectedFeature),
    annotation_label: selectedCorrectLabel,
    qa_status: 'annotated',
    qa_correct_class: selectedCorrectLabel,
    qa_notes: qaNotes.value,
    reviewer: reviewerInput.value,
    feature_version_seen: selectedFeature.version,
  };
  const response = await fetch('/api/annotations?mode=annotation', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    alert(await response.text());
    return;
  }
  const saved = await response.json();
  annotations[featureKey(saved)] = saved;
  restyleFeatureLayers();
  updateH3Grid();
  updateCounts();
  updateCellProgress();
  if (selectedCellId && selectedCellFeatureIds.length) {
    const nextIndex = nextOpenFeatureIndex();
    if (nextIndex >= 0) {
      selectFeatureAtCellPosition(nextIndex, true);
    }
    else {
      finishSelectedCellReview();
    }
  }
}

previousFeatureButton.addEventListener('click', () => {
  selectFeatureAtCellPosition(selectedCellPosition - 1, true);
});

nextOpenFeatureButton.addEventListener('click', () => {
  const nextIndex = nextOpenFeatureIndex();
  if (nextIndex >= 0) {
    selectFeatureAtCellPosition(nextIndex, true);
  }
});

nextFeatureButton.addEventListener('click', () => {
  selectFeatureAtCellPosition(selectedCellPosition + 1, true);
});

map.on('zoomend', () => updateH3Grid());

map.on('click', (event) => {
  if (awaitingPointPlacement) {
    stagePointPlacement(event.latlng);
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    clearSelectedCell();
  }
});

saveButton.addEventListener('click', () => {
  const operation = isAnnotationMode() ? saveAnnotation() : saveGeometry();
  operation.catch((error) => alert(error.message));
});

// *!*! Initial data loads precede imagery so vector review remains usable if a COG fails.
async function init() {
  configureModeControls();
  renderLayerPanel();
  await loadAnnotations();
  await loadFeatures();
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
