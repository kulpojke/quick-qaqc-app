'''*!*! YAML configuration support for feature annotation and editing.'''

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlparse

import yaml


SUPPORTED_VERSION = 3
ALLOWED_MODES = {'annotation', 'editing'}
ALLOWED_GEOMETRY_TYPES = {'Point', 'MultiPoint', 'Polygon', 'MultiPolygon'}
DEFAULT_H3_RESOLUTIONS = (5, 6, 7, 8, 9, 10)
LAYER_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
REVIEWER_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+@-]{0,254}$')


class ConfigError(ValueError):
    '''*!*! Raised when a project configuration is incomplete or invalid.'''


@dataclass(frozen=True)
class EditingConfig:
    '''*!*! Describe geometry operations enabled for one feature layer.'''

    move: bool = False
    reshape: bool = False
    create: bool = False
    delete: bool = False


@dataclass(frozen=True)
class LayerConfig:
    '''*!*! Describe one independently imported and exported feature layer.'''

    id: str
    name: str
    source: Path | str
    source_crs: str
    geometry_types: tuple[str, ...]
    feature_id_field: str
    predicted_class_field: str
    confidence_field: str
    display_fields: tuple[str, ...]
    h3_prefix: str
    h3_resolutions: tuple[int, ...]
    modes: tuple[str, ...]
    editing: EditingConfig


@dataclass(frozen=True)
class ReviewerAssignmentConfig:
    '''*!*! Assign one reviewer to layer modes and optional H3 cells.'''

    layer_id: str
    modes: tuple[str, ...]
    h3_indexes: tuple[str, ...]


@dataclass(frozen=True)
class ReviewerConfig:
    '''*!*! Describe one authenticated reviewer and their project assignments.'''

    id: str
    assignments: tuple[ReviewerAssignmentConfig, ...]

    @property
    def modes(self) -> tuple[str, ...]:
        '''*!*! Return the reviewer's enabled modes in configuration order.'''

        return tuple(dict.fromkeys(
            mode
            for assignment in self.assignments
            for mode in assignment.modes
        ))

    def assignment_for(
        self,
        layer_id: str,
        mode: str,
    ) -> ReviewerAssignmentConfig | None:
        '''*!*! Return this reviewer's assignment for one layer and mode.'''

        return next(
            (
                assignment
                for assignment in self.assignments
                if assignment.layer_id == layer_id and mode in assignment.modes
            ),
            None,
        )


@dataclass(frozen=True)
class ExportConfig:
    '''*!*! Describe a project's R2 destination without storing credentials.'''

    bucket: str
    endpoint_url: str
    prefix: str = 'exports'


@dataclass(frozen=True)
class ReviewConfig:
    '''*!*! Validated settings loaded from one project YAML file.'''

    source_path: Path
    project_id: str
    project_name: str
    imagery_cog: str
    layers: tuple[LayerConfig, ...]
    annotation_labels: tuple[str, ...]
    modes: tuple[str, ...]
    reviewers: tuple[ReviewerConfig, ...]
    exports: ExportConfig | None = None

    def reviewer(self, reviewer_id: str) -> ReviewerConfig | None:
        '''*!*! Return one configured reviewer by authenticated identity.'''

        return next(
            (reviewer for reviewer in self.reviewers if reviewer.id == reviewer_id),
            None,
        )


def _mapping(parent: dict, key: str) -> dict:
    '''*!*! Return a named config section after validating it is a mapping.'''

    value = parent.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f'{key} must be a mapping')
    return value


def _required_string(parent: dict, key: str, section: str) -> str:
    '''*!*! Read a required, non-empty string from a config section.'''

    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{section}.{key} must be a non-empty string')
    return value.strip()


def _optional_string(parent: dict, key: str, default: str = '') -> str:
    '''*!*! Read an optional string, returning its configured default.'''

    value = parent.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f'{key} must be a string')
    return value.strip()


def _optional_bool(parent: dict, key: str, default: bool = False) -> bool:
    '''*!*! Read an optional boolean without coercing strings or numbers.'''

    value = parent.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f'{key} must be true or false')
    return value


def _is_http_url(value: str) -> bool:
    '''*!*! Return whether a value is an absolute HTTP or HTTPS URL.'''

    parsed = urlparse(value)
    return parsed.scheme.lower() in {'http', 'https'} and bool(parsed.netloc)


def _resolve_source(
    value: object,
    *,
    base_dir: Path,
    field: str,
) -> Path | str:
    '''*!*! Resolve one local path while preserving an HTTP(S) URL.'''

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{field} must be a local path or HTTP(S) URL')
    rendered = value.strip()
    if _is_http_url(rendered):
        return rendered
    path = Path(rendered).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _parse_modes(workflow: dict) -> tuple[str, ...]:
    '''*!*! Validate and deduplicate configured workflow modes.'''

    modes = workflow.get('modes')
    if not isinstance(modes, list) or not modes:
        raise ConfigError('workflow.modes must be a non-empty list')
    normalized = tuple(dict.fromkeys(str(mode).strip().lower() for mode in modes))
    invalid = sorted(set(normalized) - ALLOWED_MODES)
    if invalid:
        raise ConfigError(f'Unsupported workflow mode(s): {", ".join(invalid)}')
    return normalized


def _parse_h3_indexes(
    values: object,
    *,
    field: str,
    allowed_resolutions: tuple[int, ...],
) -> tuple[str, ...]:
    '''*!*! Validate H3 cells assigned to one reviewer layer.'''

    import h3

    if not isinstance(values, list):
        raise ConfigError(f'{field} must be a list')

    indexes = []
    for position, item in enumerate(values):
        index = item.get('h3_index') if isinstance(item, dict) else item
        if not isinstance(index, str) or not index.strip():
            raise ConfigError(f'{field}[{position}] must be a non-empty H3 index')
        index = index.strip()
        if not h3.is_valid_cell(index):
            raise ConfigError(f'{field}[{position}] is not a valid H3 index: {index}')
        resolution = h3.get_resolution(index)
        if resolution not in allowed_resolutions:
            raise ConfigError(
                f'{field}[{position}] uses resolution {resolution}, but its layer '
                f'generates only {list(allowed_resolutions)}'
            )
        indexes.append(index)
    return tuple(dict.fromkeys(indexes))


def _parse_labels(root: dict, modes: tuple[str, ...]) -> tuple[str, ...]:
    '''*!*! Validate and deduplicate configured annotation labels.'''

    annotation = _mapping(root, 'annotation')
    labels = annotation.get('labels', [])
    if not isinstance(labels, list):
        raise ConfigError('annotation.labels must be a list')
    normalized = tuple(
        dict.fromkeys(str(label).strip() for label in labels if str(label).strip())
    )
    if 'annotation' in modes and not normalized:
        raise ConfigError(
            'annotation.labels must contain at least one label for annotation mode'
        )
    return normalized


def _parse_geometry_types(layer: dict, position: int) -> tuple[str, ...]:
    '''*!*! Validate GeoJSON geometry types accepted by one layer.'''

    values = layer.get('geometry_types')
    if not isinstance(values, list) or not values:
        raise ConfigError(f'layers[{position}].geometry_types must be a non-empty list')
    normalized = tuple(dict.fromkeys(str(value).strip() for value in values))
    invalid = sorted(set(normalized) - ALLOWED_GEOMETRY_TYPES)
    if invalid:
        raise ConfigError(
            f'layers[{position}] has unsupported geometry type(s): {", ".join(invalid)}'
        )
    return normalized


def _parse_h3_resolutions(layer: dict, position: int) -> tuple[int, ...]:
    '''*!*! Validate H3 resolutions generated for one imported layer.'''

    values = layer.get('h3_resolutions', list(DEFAULT_H3_RESOLUTIONS))
    if not isinstance(values, list) or not values:
        raise ConfigError(f'layers[{position}].h3_resolutions must be a non-empty list')
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ConfigError(f'layers[{position}].h3_resolutions must contain integers')
    normalized = tuple(dict.fromkeys(values))
    if any(value < 0 or value > 15 for value in normalized):
        raise ConfigError(f'layers[{position}].h3_resolutions must be between 0 and 15')
    return normalized


def _parse_layer_modes(
    layer: dict,
    position: int,
    workflow_modes: tuple[str, ...],
) -> tuple[str, ...]:
    '''*!*! Restrict one layer to a non-empty subset of workflow modes.'''

    values = layer.get('modes', list(workflow_modes))
    if not isinstance(values, list) or not values:
        raise ConfigError(f'layers[{position}].modes must be a non-empty list')
    modes = tuple(dict.fromkeys(str(value).strip().lower() for value in values))
    invalid = sorted(set(modes) - set(workflow_modes))
    if invalid:
        raise ConfigError(
            f'layers[{position}].modes are not enabled by workflow.modes: '
            f'{", ".join(invalid)}'
        )
    return modes


def _parse_editing(layer: dict, position: int) -> EditingConfig:
    '''*!*! Parse explicit edit capabilities for one layer.'''

    value = layer.get('editing', {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError(f'layers[{position}].editing must be a mapping')
    return EditingConfig(
        move=_optional_bool(value, 'move'),
        reshape=_optional_bool(value, 'reshape'),
        create=_optional_bool(value, 'create'),
        delete=_optional_bool(value, 'delete'),
    )


def _parse_display_fields(fields: dict, position: int) -> tuple[str, ...]:
    '''*!*! Validate optional source columns displayed for one layer.'''

    values = fields.get('display', [])
    if not isinstance(values, list):
        raise ConfigError(f'layers[{position}].fields.display must be a list')
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def _parse_layers(
    root: dict,
    *,
    base_dir: Path,
    workflow_modes: tuple[str, ...],
) -> tuple[LayerConfig, ...]:
    '''*!*! Parse independent point and polygon GeoParquet layers.'''

    values = root.get('layers')
    if not isinstance(values, list) or not values:
        raise ConfigError('layers must be a non-empty list')

    layers = []
    seen_ids = set()
    for position, value in enumerate(values):
        if not isinstance(value, dict):
            raise ConfigError(f'layers[{position}] must be a mapping')
        layer_id = _required_string(value, 'id', f'layers[{position}]')
        if layer_id == 'annotations':
            raise ConfigError('Layer id annotations is reserved for annotation exports')
        if not LAYER_ID_PATTERN.fullmatch(layer_id):
            raise ConfigError(
                f'layers[{position}].id must contain only letters, numbers, _ or -'
            )
        if layer_id in seen_ids:
            raise ConfigError(f'Duplicate layer id: {layer_id}')
        seen_ids.add(layer_id)
        fields = _mapping(value, 'fields')
        parsed = LayerConfig(
            id=layer_id,
            name=_optional_string(value, 'name', layer_id),
            source=_resolve_source(
                value.get('source'),
                base_dir=base_dir,
                field=f'layers[{position}].source',
            ),
            source_crs=_optional_string(value, 'crs', 'EPSG:4326'),
            geometry_types=_parse_geometry_types(value, position),
            feature_id_field=_optional_string(fields, 'feature_id', 'id'),
            predicted_class_field=_optional_string(fields, 'predicted_class', ''),
            confidence_field=_optional_string(fields, 'confidence', ''),
            display_fields=_parse_display_fields(fields, position),
            h3_prefix=_optional_string(value, 'h3_prefix', 'h3_r'),
            h3_resolutions=_parse_h3_resolutions(value, position),
            modes=_parse_layer_modes(value, position, workflow_modes),
            editing=_parse_editing(value, position),
        )
        if 'editing' in parsed.modes and not any(vars(parsed.editing).values()):
            raise ConfigError(
                f'layers[{position}] enables editing but has no editing capabilities'
            )
        layers.append(parsed)
    return tuple(layers)


def _parse_reviewers(
    workflow: dict,
    layers: tuple[LayerConfig, ...],
) -> tuple[ReviewerConfig, ...]:
    '''*!*! Validate reviewer identities and layer-specific assignments.'''

    values = workflow.get('reviewers')
    if not isinstance(values, list) or not values:
        raise ConfigError('workflow.reviewers must be a non-empty list')

    layer_by_id = {layer.id: layer for layer in layers}
    reviewers = []
    seen_reviewer_ids = set()
    for reviewer_position, value in enumerate(values):
        field = f'workflow.reviewers[{reviewer_position}]'
        if not isinstance(value, dict):
            raise ConfigError(f'{field} must be a mapping')
        reviewer_id = _required_string(value, 'id', field)
        if not REVIEWER_ID_PATTERN.fullmatch(reviewer_id):
            raise ConfigError(f'{field}.id contains unsupported characters')
        if reviewer_id in seen_reviewer_ids:
            raise ConfigError(f'Duplicate reviewer id: {reviewer_id}')
        seen_reviewer_ids.add(reviewer_id)

        assignment_values = value.get('assignments')
        if not isinstance(assignment_values, list) or not assignment_values:
            raise ConfigError(f'{field}.assignments must be a non-empty list')
        assignments = []
        seen_layer_modes = set()
        for assignment_position, assignment_value in enumerate(assignment_values):
            assignment_field = f'{field}.assignments[{assignment_position}]'
            if not isinstance(assignment_value, dict):
                raise ConfigError(f'{assignment_field} must be a mapping')
            layer_id = _required_string(assignment_value, 'layer', assignment_field)
            layer = layer_by_id.get(layer_id)
            if layer is None:
                raise ConfigError(f'{assignment_field}.layer is unknown: {layer_id}')

            mode_values = assignment_value.get('modes', list(layer.modes))
            if not isinstance(mode_values, list) or not mode_values:
                raise ConfigError(f'{assignment_field}.modes must be a non-empty list')
            modes = tuple(dict.fromkeys(
                str(mode).strip().lower() for mode in mode_values
            ))
            invalid_modes = sorted(set(modes) - set(layer.modes))
            if invalid_modes:
                invalid_modes_text = ', '.join(invalid_modes)
                raise ConfigError(
                    f'{assignment_field}.modes are not enabled for layer {layer_id}: '
                    f'{invalid_modes_text}'
                )
            duplicate_modes = sorted(
                mode for mode in modes if (layer_id, mode) in seen_layer_modes
            )
            if duplicate_modes:
                duplicate_modes_text = ', '.join(duplicate_modes)
                raise ConfigError(
                    f'{assignment_field} duplicates {layer_id} mode(s): '
                    f'{duplicate_modes_text}'
                )
            seen_layer_modes.update((layer_id, mode) for mode in modes)
            assignments.append(ReviewerAssignmentConfig(
                layer_id=layer_id,
                modes=modes,
                h3_indexes=_parse_h3_indexes(
                    assignment_value.get('h3_indexes', []),
                    field=f'{assignment_field}.h3_indexes',
                    allowed_resolutions=layer.h3_resolutions,
                ),
            ))
        reviewers.append(ReviewerConfig(
            id=reviewer_id,
            assignments=tuple(assignments),
        ))
    return tuple(reviewers)


def _parse_exports(root: dict) -> ExportConfig | None:
    '''*!*! Validate an optional R2 destination; reject secrets and mistyped fields.'''

    if 'exports' not in root:
        return None
    exports = _mapping(root, 'exports')
    unknown = set(exports) - {'bucket', 'endpoint_url', 'prefix'}
    if unknown:
        raise ConfigError(f'Unknown exports field(s): {", ".join(sorted(unknown))}')
    bucket = _required_string(exports, 'bucket', 'exports')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise ConfigError('exports.bucket must be an R2 bucket name, not a URL')
    endpoint = _required_string(exports, 'endpoint_url', 'exports').rstrip('/')
    if not re.fullmatch(
        r'https://[a-f0-9]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com',
        endpoint,
    ):
        raise ConfigError('exports.endpoint_url must be an HTTPS R2 S3 API endpoint')
    prefix = _optional_string(exports, 'prefix', 'exports').strip('/')
    if not prefix or any(
        not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*', part)
        for part in prefix.split('/')
    ):
        raise ConfigError('exports.prefix must be a non-empty object prefix without dot segments')
    return ExportConfig(bucket=bucket, endpoint_url=endpoint, prefix=prefix)


def load_review_config(path: Path) -> ReviewConfig:
    '''*!*! Load and validate a layer-aware project configuration.'''

    source_path = path.expanduser().resolve()
    if not source_path.is_file():
        raise ConfigError(f'Configuration file not found: {source_path}')

    with source_path.open(encoding='utf-8') as file:
        root = yaml.safe_load(file) or {}
    if not isinstance(root, dict):
        raise ConfigError('The YAML document must be a mapping')
    version = root.get('version')
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f'Unsupported config version {version!r}; expected {SUPPORTED_VERSION}'
        )

    project = _mapping(root, 'project')
    paths = _mapping(root, 'paths')
    workflow = _mapping(root, 'workflow')
    modes = _parse_modes(workflow)
    base_dir = source_path.parent
    project_name = _optional_string(project, 'name', source_path.stem)
    project_id = _optional_string(project, 'id', project_name)
    if not project_id:
        raise ConfigError('project.id must be a non-empty string')

    imagery_cog = str(_resolve_source(
        paths.get('imagery_cog'),
        base_dir=base_dir,
        field='paths.imagery_cog',
    ))
    layers = _parse_layers(
        root,
        base_dir=base_dir,
        workflow_modes=modes,
    )
    enabled_layer_modes = {mode for layer in layers for mode in layer.modes}
    unused_modes = sorted(set(modes) - enabled_layer_modes)
    if unused_modes:
        raise ConfigError(
            f'workflow mode(s) are not enabled for any layer: {", ".join(unused_modes)}'
        )

    return ReviewConfig(
        source_path=source_path,
        project_id=project_id,
        project_name=project_name,
        imagery_cog=imagery_cog,
        layers=layers,
        annotation_labels=_parse_labels(root, modes),
        modes=modes,
        reviewers=_parse_reviewers(workflow, layers),
        exports=_parse_exports(root),
    )
