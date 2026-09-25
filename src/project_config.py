'''*!*! YAML configuration support for feature annotation and editing.'''

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlparse

import yaml


SUPPORTED_VERSION = 2
ALLOWED_MODES = {'annotation', 'editing'}
ALLOWED_GEOMETRY_TYPES = {'Point', 'MultiPoint', 'Polygon', 'MultiPolygon'}
DEFAULT_H3_RESOLUTIONS = (5, 6, 7, 8, 9, 10)
LAYER_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


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
class ReviewConfig:
    '''*!*! Validated settings loaded from one project YAML file.'''

    source_path: Path
    project_id: str
    project_name: str
    imagery_cog: str
    layers: tuple[LayerConfig, ...]
    annotation_labels: tuple[str, ...]
    user: str
    modes: tuple[str, ...]
    todo_h3_indexes: tuple[str, ...]


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


def _expand_user_template(value: str, user: str, field: str) -> str:
    '''*!*! Expand the supported user placeholder in a configured value.'''

    try:
        return value.format(user=user)
    except (KeyError, ValueError) as error:
        raise ConfigError(
            f'{field} contains an unsupported template field; only {{user}} is allowed'
        ) from error


def _resolve_source(
    value: object,
    *,
    base_dir: Path,
    user: str,
    field: str,
) -> Path | str:
    '''*!*! Resolve one local path while preserving an HTTP(S) URL.'''

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{field} must be a local path or HTTP(S) URL')
    rendered = _expand_user_template(value.strip(), user, field)
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


def _parse_todo(workflow: dict) -> tuple[str, ...]:
    '''*!*! Validate and deduplicate configured H3 TODO assignments.'''

    import h3

    todo = workflow.get('todo', [])
    if not isinstance(todo, list):
        raise ConfigError('workflow.todo must be a list')

    indexes = []
    for position, item in enumerate(todo):
        index = item.get('h3_index') if isinstance(item, dict) else item
        if not isinstance(index, str) or not index.strip():
            raise ConfigError(
                f'workflow.todo[{position}].h3_index must be a non-empty string'
            )
        index = index.strip()
        if not h3.is_valid_cell(index):
            raise ConfigError(f'workflow.todo[{position}] is not a valid H3 index: {index}')
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
    user: str,
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
                user=user,
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
    user = _required_string(workflow, 'user', 'workflow')
    modes = _parse_modes(workflow)
    base_dir = source_path.parent
    project_name = _optional_string(project, 'name', source_path.stem)
    project_id = _optional_string(project, 'id', project_name)
    if not project_id:
        raise ConfigError('project.id must be a non-empty string')

    imagery_cog = str(_resolve_source(
        paths.get('imagery_cog'),
        base_dir=base_dir,
        user=user,
        field='paths.imagery_cog',
    ))
    layers = _parse_layers(
        root,
        base_dir=base_dir,
        user=user,
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
        user=user,
        modes=modes,
        todo_h3_indexes=_parse_todo(workflow),
    )
