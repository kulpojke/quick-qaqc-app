#!/usr/bin/env bash
# *!*! Update one Compose stack while retaining its database and export storage.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf 'Usage: %s <project.yaml>\n' "$0" >&2
    exit 2
fi

# *!*! Resolve the argument before changing directories; callers may be elsewhere.
project_yaml=$(realpath -- "$1")
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
printf 'Updating project from %s\n' "$project_yaml"

# *!*! Build and validate before changing running services. Docker reuses its cache.
"${compose[@]}" build app
"${compose[@]}" run --rm --no-deps --entrypoint python app -c \
    'import sys; from src.project_config import load_review_config; load_review_config(sys.argv[1])' \
    "/project/$PROJECT_CONFIG"

# *!*! Reuse an existing database container and volume; never run down or prune.
"${compose[@]}" up -d --no-recreate --wait --wait-timeout 180 db
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps bootstrap

# *!*! Recreate application services even for YAML-only changes, then await health.
"${compose[@]}" up -d --no-deps --no-build --force-recreate --wait --wait-timeout 180 app api export
"${compose[@]}" run --rm --no-deps startup
"${compose[@]}" ps
printf 'Update complete. Database volume and local exports retained.\n'
