#!/bin/bash
set -e

CONFIG_PATH="${CONFIG_PATH:-config.json}"

# Check if we're on main branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "Error: Publishing is only allowed from the 'main' branch."
    echo "Current branch: $CURRENT_BRANCH"
    echo "Please switch to main branch and try again."
    exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "Error: Config file not found: $CONFIG_PATH"
    echo "Create it from config.example.json and set pypi.publish_token"
    exit 1
fi

PUBLISH_TOKEN=$(jq -r '.pypi.publish_token // empty' "$CONFIG_PATH")

if [ -z "$PUBLISH_TOKEN" ]; then
    echo "Error: pypi.publish_token is not set in $CONFIG_PATH"
    exit 1
fi

poetry config pypi-token.pypi "$PUBLISH_TOKEN"
VERSION=$(poetry version -s)

poetry publish || {
    echo "Publishing failed. Check token, package name availability, and version uniqueness."
    exit 1
}

echo "Published version $VERSION to PyPI"

