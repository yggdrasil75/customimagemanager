#!/usr/bin/env bash
# Pull the latest code and re-sync the install.
#
# git pull, then re-run install.sh for the profile already recorded in
# app_config.json, with system packages skipped and your module toggles left
# alone — so new deps and newly-shipped modules land, and nothing you turned
# off turns back on.
#
#   ./update.sh                 pull + re-sync
#   ./update.sh --code-only     pull only, don't touch pip
#   ./update.sh --system        also re-run the system package step
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

CODE_ONLY=0
SYSTEM=0

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --code-only) CODE_ONLY=1; shift ;;
        --system) SYSTEM=1; shift ;;
        -h|--help) sed -n '2,${/^#/!q; s/^# \{0,1\}//p;}' "$0"; exit 0 ;;
        *) die "unknown option '$1' (--help)" ;;
    esac
done

command -v git >/dev/null 2>&1 || die "git not found"
[ -d .git ] || die "not a git checkout; update by hand"
[ -z "$(git status --porcelain --untracked-files=no)" ] \
    || warn "you have local changes; resolve any conflicts yourself"

BEFORE="$(git rev-parse HEAD)"
say "pulling"
git pull --ff-only || die "pull failed (diverged? rebase or stash first)"
AFTER="$(git rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
    say "already up to date"
else
    say "updated $(git rev-parse --short "$BEFORE") -> $(git rev-parse --short "$AFTER")"
    git --no-pager log --oneline "$BEFORE..$AFTER" | head -20
fi

if [ "$CODE_ONLY" = 1 ]; then
    say "code only; ./run.sh installs any new module deps on start"
    exit 0
fi

read -r PROFILE BACKEND <<<"$(python3 modules/deps.py state)"
ARGS=(--profile "$PROFILE" --backend "$BACKEND" --keep-modules -y)
[ "$SYSTEM" = 1 ] || ARGS+=(--no-system)

say "re-syncing deps (profile=$PROFILE backend=$BACKEND)"
./install.sh "${ARGS[@]}"

say "restart to pick it up: ./run.sh"