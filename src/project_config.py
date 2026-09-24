'''*!*! YAML configuration support for the annotation and QA/QC app.'''

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml


SUPPORTED_VERSION = 1
ALLOWED_MODES = {'annotation', 'qaqc', 'editing'}


class ConfigError(ValueError):
    '''*!*! Raised when a project configuration is incomplete or invalid.'''


@dataclass(frozen=True)
class OvertureConfig:
    '''*!*! Configure automatic Overture building retrieval from Fused.'''

    fire_date: date
    release_selection: str
    refresh: bool


@dataclass(frozen=True)
class ReviewConfig:
    '''*!*! Validated settings loaded from one project YAML file.'''

    source_path: Path
    project_id: str
    project_name: str
    features_path: Path | str
    imagery_cog: str
    overture: OvertureConfig | None
    annotations_input: Path | None
    qaqc_input: Path | None
    annotations_output: Path | None
    qaqc_output: Path | None
    edited_features_output: Path | None
    feature_id_field: str
    predicted_class_field: str
    annotation_label_field: str
    confidence_field: str
    h3_prefix: str
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


def _fire_date(value: object) -> date:
    '''*!*! Normalize a YAML date or ISO-8601 timestamp to a calendar date.'''

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ConfigError('project.fire_date must be an ISO-8601 date or timestamp')
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.fromisoformat(normalized.replace('Z', '+00:00')).date()
        except ValueError as error:
            raise ConfigError(
                'project.fire_date must be an ISO-8601 date or timestamp'
            ) from error


def _auto_feature_selection(value: object) -> str | None:
    '''*!*! Return the Fused release strategy encoded by paths.features.'''

    if value is None:
        return 'before_fire'
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == 'none':
            return 'before_fire'
        if normalized == 'oldest':
            return 'oldest'
    return None


def _parse_overture(
    root: dict,
    project: dict,
    imagery_cog: str,
    release_selection: str | None,
) -> OvertureConfig | None:
    '''*!*! Parse Fused retrieval settings implied by paths.features.'''

    overture = _mapping(root, 'overture')
    if release_selection is None:
        if overture:
            raise ConfigError(
                'overture settings require paths.features to be None or oldest'
            )
        return None
    if not imagery_cog:
        raise ConfigError(
            'paths.imagery_cog is required when paths.features is None or oldest'
        )
    return OvertureConfig(
        fire_date=_fire_date(project.get('fire_date')),
        release_selection=release_selection,
        refresh=_optional_bool(overture, 'refresh'),
    )


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


def _resolve_path(
    value: object,
    *,
    base_dir: Path,
    user: str,
    field: str,
) -> Path | None:
    '''*!*! Resolve an optional configured path relative to the YAML file.'''

    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ConfigError(f'{field} must be a path string')
    rendered = _expand_user_template(value.strip(), user, field)
    path = Path(rendered).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _resolve_cog(value: object, *, base_dir: Path, user: str) -> str:
    '''*!*! Resolve a local COG path while preserving remote COG URLs.'''

    if value is None or value == '':
        return ''
    if not isinstance(value, str):
        raise ConfigError('paths.imagery_cog must be a path or HTTP(S) URL')
    rendered = _expand_user_template(value.strip(), user, 'paths.imagery_cog')
    if _is_http_url(rendered):
        return rendered
    path = Path(rendered).expanduser()
    return str(path if path.is_absolute() else (base_dir / path).resolve())


def _resolve_feature_source(
    value: object,
    *,
    base_dir: Path,
    user: str,
) -> Path | str | None:
    '''*!*! Resolves a local feature path while preserving HTTP(S) URLs.'''

    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ConfigError('paths.features must be a path or HTTP(S) URL')
    rendered = _expand_user_template(value.strip(), user, 'paths.features')
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
        invalid_text = ', '.join(invalid)
        allowed_text = ', '.join(sorted(ALLOWED_MODES))
        raise ConfigError(
            f'Unsupported workflow mode(s): {invalid_text}; choose from {allowed_text}'
        )
    return normalized


def _parse_todo(workflow: dict) -> tuple[str, ...]:
    '''*!*! Validate and deduplicate H3 cells assigned to the technician.'''

    import h3

    todo = workflow.get('todo', [])
    if todo is None:
        return ()
    if not isinstance(todo, list):
        raise ConfigError('workflow.todo must be a list')

    indexes = []
    for position, item in enumerate(todo, start=1):
        if isinstance(item, str):
            index = item.strip()
        elif isinstance(item, dict):
            index = str(item.get('h3_index', '')).strip()
        else:
            index = ''
        if not index:
            raise ConfigError(
                f'workflow.todo item {position} must contain a non-empty h3_index'
            )
        if not h3.is_valid_cell(index):
            raise ConfigError(
                f'workflow.todo item {position} is not a valid H3 index: {index}'
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
    if {'annotation', 'qaqc'}.intersection(modes) and not normalized:
        raise ConfigError(
            'annotation.labels must contain at least one label for annotation or qaqc mode'
        )
    return normalized


def load_review_config(path: Path) -> ReviewConfig:
    '''*!*! Load and validate a config, resolving its relative paths.'''

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
    fields = _mapping(root, 'fields')
    workflow = _mapping(root, 'workflow')
    user = _required_string(workflow, 'user', 'workflow')
    modes = _parse_modes(workflow)
    base_dir = source_path.parent
    project_name = _optional_string(project, 'name', source_path.stem)
    project_id = _optional_string(project, 'id', project_name)
    if not project_id:
        raise ConfigError('project.id must be a non-empty string')

    features_value = paths.get('features')
    release_selection = _auto_feature_selection(features_value)
    if release_selection is None:
        features_path = _resolve_feature_source(
            features_value,
            base_dir=base_dir,
            user=user,
        )
        if features_path is None:
            raise ConfigError('paths.features must be a path, None, or oldest')
    else:
        features_path = (
            base_dir / 'data' / f'{source_path.stem}_overture_buildings.geojson'
        ).resolve()
    imagery_cog = _resolve_cog(paths.get('imagery_cog'), base_dir=base_dir, user=user)
    overture = _parse_overture(root, project, imagery_cog, release_selection)

    annotations_output = _resolve_path(
        paths.get('annotations_output'),
        base_dir=base_dir,
        user=user,
        field='paths.annotations_output',
    )
    qaqc_output = _resolve_path(
        paths.get('qaqc_output'),
        base_dir=base_dir,
        user=user,
        field='paths.qaqc_output',
    )
    edited_features_output = _resolve_path(
        paths.get('edited_features_output'),
        base_dir=base_dir,
        user=user,
        field='paths.edited_features_output',
    )
    required_outputs = {
        'annotation': (annotations_output, 'paths.annotations_output'),
        'qaqc': (qaqc_output, 'paths.qaqc_output'),
        'editing': (edited_features_output, 'paths.edited_features_output'),
    }
    for mode in modes:
        value, field = required_outputs[mode]
        if value is None:
            raise ConfigError(f'{field} is required when {mode} mode is enabled')

    return ReviewConfig(
        source_path=source_path,
        project_id=project_id,
        project_name=project_name,
        features_path=features_path,
        imagery_cog=imagery_cog,
        overture=overture,
        annotations_input=_resolve_path(
            paths.get('annotations_input'),
            base_dir=base_dir,
            user=user,
            field='paths.annotations_input',
        ),
        qaqc_input=_resolve_path(
            paths.get('qaqc_input'),
            base_dir=base_dir,
            user=user,
            field='paths.qaqc_input',
        ),
        annotations_output=annotations_output,
        qaqc_output=qaqc_output,
        edited_features_output=edited_features_output,
        feature_id_field=_optional_string(fields, 'feature_id', 'id'),
        predicted_class_field=_optional_string(fields, 'predicted_class', 'predicted_class'),
        annotation_label_field=_optional_string(fields, 'annotation_label', 'annotation_label'),
        confidence_field=_optional_string(fields, 'confidence', ''),
        h3_prefix=_optional_string(fields, 'h3_prefix', 'h3_r'),
        annotation_labels=_parse_labels(root, modes),
        user=user,
        modes=modes,
        todo_h3_indexes=_parse_todo(workflow),
    )
