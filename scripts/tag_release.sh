#!/usr/bin/env bash
# Creates the next semver git tag for this repo (vMAJOR.MINOR.PATCH), based
# on the highest existing vX.Y.Z tag - not on pyproject.toml's version
# field, which has already drifted from the real tag history (pyproject.toml
# says 0.1.1 while v0.1.2 exists) and isn't what this repo's releases have
# actually tracked so far.
#
# Usage:
#   scripts/tag_release.sh [major|minor|patch] [-m "message"] [--push] [--dry-run]
#
# Defaults to a patch bump - matches every release so far (0.1.0 -> 0.1.1 ->
# 0.1.2), regardless of whether the change was a fix or a feature. This repo
# has never used a minor/major bump, so patch is the conservative default;
# pass "minor" or "major" explicitly to bump those instead.
#
# Refuses to run with uncommitted changes - a tag should point at a clean,
# intentional snapshot, not whatever happens to be sitting in the working
# tree. Never pushes unless --push is passed explicitly (tag and push are
# separate, always-confirm actions - this script defaults to local-only so
# a future run can't silently push without you asking for it that time).

set -euo pipefail

BUMP="patch"
MESSAGE=""
PUSH=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: scripts/tag_release.sh [major|minor|patch] [-m "message"] [--push] [--dry-run]

  major|minor|patch   Which part to bump (default: patch)
  -m, --message TEXT  Annotated tag message (default: auto-generated from
                       the one-line log of commits since the last tag)
  --push              Push the new tag to origin after creating it
  --dry-run           Print what would happen without creating anything
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    major|minor|patch) BUMP="$1"; shift ;;
    -m|--message) MESSAGE="$2"; shift 2 ;;
    --push) PUSH=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Not inside a git repository." >&2
  exit 1
}
cd "$repo_root"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree has uncommitted changes - commit or stash first, then re-run." >&2
  git status --short >&2
  exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "main" ]]; then
  echo "Warning: tagging from branch '$current_branch', not 'main'." >&2
fi

echo "Fetching tags from origin..." >&2
git fetch --tags --quiet || echo "Warning: could not fetch tags from origin (continuing with local tags)." >&2

# Highest existing vX.Y.Z tag, sorted numerically per field (lexical sort
# would wrongly put v0.1.10 before v0.1.2) - portable across BSD sort
# (macOS default) and GNU sort, unlike GNU-only `sort -V`.
existing_tags="$(git tag -l 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true)"
latest_tag="$(printf '%s\n' "$existing_tags" | sed -E 's/^v//' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"

if [[ -z "$latest_tag" ]]; then
  echo "No existing vX.Y.Z tags found - starting at v0.1.0." >&2
  next_tag="v0.1.0"
else
  IFS='.' read -r major minor patch <<< "$latest_tag"
  case "$BUMP" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
  esac
  next_tag="v${major}.${minor}.${patch}"
fi

if git rev-parse "$next_tag" >/dev/null 2>&1; then
  echo "Tag $next_tag already exists." >&2
  exit 1
fi

if [[ -z "$MESSAGE" ]]; then
  if [[ -n "$latest_tag" ]]; then
    commits="$(git log "v${latest_tag}..HEAD" --oneline)"
  else
    commits="$(git log --oneline)"
  fi
  if [[ -z "$commits" ]]; then
    MESSAGE="Release $next_tag"
  else
    MESSAGE="$(printf 'Release %s\n\n%s\n' "$next_tag" "$commits")"
  fi
fi

echo "Next tag: $next_tag (bump: $BUMP, from $( [[ -n "$latest_tag" ]] && echo "v$latest_tag" || echo "none" ))"
echo "Tag message:"
echo "---"
echo "$MESSAGE"
echo "---"

if $DRY_RUN; then
  echo "Dry run - nothing created."
  exit 0
fi

git tag -a "$next_tag" -m "$MESSAGE"
echo "Created annotated tag $next_tag at $(git rev-parse --short HEAD)."

if $PUSH; then
  echo "Pushing $next_tag to origin..."
  git push origin "$next_tag"
  echo "Pushed."
else
  echo "Not pushed. To push later, run:"
  echo "  git push origin $next_tag"
fi
