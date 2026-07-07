#!/bin/sh
# Publish a sanitized snapshot of HEAD to the public distribution repo (the
# `public` git remote), as a SINGLE ORPHAN COMMIT — never shared history, so
# nothing from this repo's log/comments/docs ever leaks.
#
# Excluded from the snapshot: every *.md (incl. README.md — maintained by hand
# on the public repo), the docs/ directory, and this script.
#
#   sh scripts/make-public.sh             # push snapshot -> public/main
#   sh scripts/make-public.sh --release   # ...and push tag v<version>, which
#                                         # triggers the Release workflow
set -e
cd "$(dirname "$0")/.."
REPO_DIR=$(pwd)
REMOTE=${PUBLIC_REMOTE:-public}

git remote get-url "$REMOTE" >/dev/null 2>&1 || {
    echo "error: no '$REMOTE' remote. Create the repo and add it:" >&2
    echo "  gh repo create <name> --public" >&2
    echo "  git remote add $REMOTE https://github.com/<you>/<name>.git" >&2
    exit 1
}

VERSION=$(git show HEAD:pyproject.toml | grep -m1 '^version' | sed 's/.*"\(.*\)".*/\1/')
SHORT=$(git rev-parse --short HEAD)

# export HEAD's tracked tree, then sanitize
TREE_DIR=$(mktemp -d)
trap 'rm -rf "$TREE_DIR" "$TREE_DIR.index"' EXIT
git archive HEAD | tar -x -C "$TREE_DIR"
find "$TREE_DIR" -type f -name '*.md' -delete
rm -rf "$TREE_DIR/docs" "$TREE_DIR/scripts/make-public.sh"

# orphan commit of the sanitized tree (temporary index; no parent)
export GIT_INDEX_FILE="$TREE_DIR.index"
(cd "$TREE_DIR" && git --git-dir="$REPO_DIR/.git" --work-tree="$TREE_DIR" add -A)
TREE=$(git write-tree)
COMMIT=$(printf 'Athens %s (snapshot %s)\n' "$VERSION" "$SHORT" | git commit-tree "$TREE")
unset GIT_INDEX_FILE

# safety: the snapshot must contain no md/docs
LEAKS=$(git ls-tree -r --name-only "$COMMIT" | grep -ciE '\.md$|^docs/' || true)
[ "$LEAKS" = "0" ] || { echo "error: sanitization leak ($LEAKS files)" >&2; exit 1; }

git push -f "$REMOTE" "$COMMIT:refs/heads/main"
echo "pushed snapshot $COMMIT -> $REMOTE/main (Athens $VERSION, from $SHORT)"

if [ "$1" = "--release" ]; then
    git push -f "$REMOTE" "$COMMIT:refs/tags/v$VERSION"
    echo "pushed tag v$VERSION -> the Release workflow builds the installers"
fi
