#!/usr/bin/env bash
# *!*! Update one Compose stack while retaining its database and export storage.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    printf 'Usage: %s <project.yaml> [--auto-assign-hex FRAC [N]] [--conf COLUMN LIMIT] [--hex-resolution R] [--manual-assign-hex]\n' "$0" >&2
    exit 2
fi

# *!*! Resolve the argument before changing directories; callers may be elsewhere.
project_yaml=$(realpath -- "$1")
shift
assignment_args=("$@")
# *!*! Reject unknown flags before invoking Docker; Python validates their values.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto-assign-hex)
            [[ $# -ge 2 ]] || exit 2
            shift 2
            if [[ $# -gt 0 && "$1" != --* ]]; then shift; fi
            ;;
        --conf) [[ $# -ge 3 ]] || exit 2; shift 3 ;;
        --hex-resolution) [[ $# -ge 2 ]] || exit 2; shift 2 ;;
        --manual-assign-hex) shift ;;
        *) printf 'Unknown assignment argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done
if [[ ! -f "$project_yaml" || ! -r "$project_yaml" ]]; then
    printf 'Project YAML is not a readable file: %s\n' "$project_yaml" >&2
    exit 2
fi
repo_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$repo_directory"
if [[ ! -f .env ]]; then
    printf 'Create %s/.env from .env.example and set the database credentials first.\n' "$repo_directory" >&2
    exit 2
fi

# *!*! Preserve access to sibling data paths for YAML files inside the repository.
case "$project_yaml" in
    "$repo_directory"/*)
        export PROJECT_CONFIG_DIR="$repo_directory"
        export PROJECT_CONFIG="${project_yaml#"$repo_directory"/}"
        ;;
    *)
        export PROJECT_CONFIG_DIR
        PROJECT_CONFIG_DIR=$(dirname -- "$project_yaml")
        export PROJECT_CONFIG
        PROJECT_CONFIG=$(basename -- "$project_yaml")
        ;;
esac

# *!*! Always use this checkout's Compose file and .env, without sourcing secrets.
compose=(docker compose --project-directory "$repo_directory" --env-file "$repo_directory/.env" -f "$repo_directory/compose.yaml")
"${compose[@]}" config --quiet
# *!*! Catch incomplete Git deployments before starting a costly image build.
if [[ ! -f src/api/r2_export.py ]]; then
    printf 'Missing src/api/r2_export.py. Include the new publisher file in your Git commit and pull again.\n' >&2
    exit 2
fi
printf 'Updating project from %s\n' "$project_yaml"

# *!*! Build and validate before changing running services. Docker reuses its cache.
"${compose[@]}" build app
"${compose[@]}" run --rm --no-deps --entrypoint python export -c \
    'from src.project_config import load_review_config; from src.api.bootstrap import build_parser; from src.api.auto_assign import assignment_options, validate_options; from src.api.r2_export import check_destination; args = build_parser().parse_args(); config = load_review_config(args.yaml); options = assignment_options(args); validate_options(config, options) if options is not None else None; check_destination(config)' \
    --yaml "/project/$PROJECT_CONFIG" "${assignment_args[@]}"

# *!*! Reuse an existing database container and volume; never run down or prune.
"${compose[@]}" up -d --no-recreate --wait --wait-timeout 180 db
"${compose[@]}" run --rm --no-deps migrate
if [[ ${#assignment_args[@]} -gt 0 ]]; then
    "${compose[@]}" run --rm --no-deps bootstrap python -m src.api.bootstrap \
        --yaml "/project/$PROJECT_CONFIG" "${assignment_args[@]}"
else
    "${compose[@]}" run --rm --no-deps bootstrap
fi

# *!*! Recreate application services even for YAML-only changes, then await health.
"${compose[@]}" up -d --no-deps --no-build --force-recreate --wait --wait-timeout 180 app api export
"${compose[@]}" run --rm --no-deps startup
"${compose[@]}" ps
printf 'Update complete. Database volume and local exports retained.\n'
