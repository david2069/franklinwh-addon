#!/usr/bin/env bash
# Publish the Home Assistant add-on.
#
# The Supervisor clones github.com/<owner>/franklinwh-addon and builds the
# add-on on the user's machine, so publishing is: regenerate that repository's
# contents from this one, and push. No container registry, no image tags, no
# per-package visibility — one push per release.
#
#   ./scripts/publish_addon.sh            # preflight, then confirm before pushing
#   ./scripts/publish_addon.sh preflight  # checks only, changes nothing
#   ./scripts/publish_addon.sh --yes      # no prompts
#
# Earlier versions of this script also tagged a release, waited on a CI image
# build and tried to make GHCR packages public. That route is gone: it needed a
# manual visit to each package's settings that no API could replace, and it
# could leave a public manifest pointing at private images — an add-on that
# appears in the store and then fails to install.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OWNER="david2069"
ADDON_REPO="franklinwh-addon"
SRC_REPO="franklinwh-ha-integrator"
SLUG="franklinwh_ha_integrator"
VERSION="$(cat VERSION)"

ASSUME_YES=0
STAGE="preflight_and_push"
for arg in "$@"; do
    case "$arg" in
        --yes) ASSUME_YES=1 ;;
        preflight) STAGE="preflight" ;;
        *) printf 'usage: %s [preflight] [--yes]\n' "$0"; exit 2 ;;
    esac
done

ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()   { printf '  \033[31m✗\033[0m %s\n' "$1"; }
info()  { printf '    %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    printf '\n  \033[33m%s\033[0m [y/N] ' "$1"
    read -r reply </dev/tty
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

preflight() {
    head_ "Preflight — $VERSION"
    local failed=0

    gh auth status >/dev/null 2>&1 && ok "gh authenticated" \
        || { bad "gh not authenticated — run: gh auth login"; failed=1; }

    if [[ -z "$(git status --porcelain)" ]]; then
        ok "working tree clean"
    else
        bad "working tree dirty — commit or stash first"
        git status --short | head -5 | sed 's/^/      /'
        failed=1
    fi

    # One version, or a Supervisor upgrade does not mean what it says.
    local cfg_version
    cfg_version="$(grep -E '^version:' "$SLUG/config.yaml" | sed 's/.*"\(.*\)".*/\1/')"
    [[ "$cfg_version" == "$VERSION" ]] && ok "VERSION and config.yaml agree ($VERSION)" \
        || { bad "VERSION=$VERSION but config.yaml=$cfg_version"; failed=1; }

    grep -q "$VERSION" CHANGELOG.md && ok "CHANGELOG mentions $VERSION" \
        || { bad "CHANGELOG.md does not mention $VERSION"; failed=1; }

    # The Supervisor builds from the add-on folder; a stale copy ships old code.
    if .venv/bin/python -m pytest tests/test_addon_package.py -q >/dev/null 2>&1; then
        ok "add-on package tests pass (build context in sync)"
    else
        bad "add-on package tests fail — run: .venv/bin/python -m pytest tests/test_addon_package.py"
        failed=1
    fi

    # The source repository carries a repository.json of its own, so a public
    # one would offer the same add-on from the private tree. Keep it private.
    if gh api "repos/$OWNER/$SRC_REPO" --jq .visibility 2>/dev/null | grep -q public; then
        bad "$SRC_REPO is PUBLIC — it has repository.json and would serve the add-on too"
        failed=1
    else
        ok "$SRC_REPO is private"
    fi

    local vis; vis="$(gh api "repos/$OWNER/$ADDON_REPO" --jq .visibility 2>/dev/null)" || vis="unknown"
    if [[ "$vis" == "public" ]]; then
        ok "$ADDON_REPO is public (the Supervisor can clone it)"
    else
        bad "$ADDON_REPO is $vis — Home Assistant cannot clone it"
        info "GitHub returns 404 to anonymous clients for a private repo, so this"
        info "shows up in HA as 'repository not found' rather than 'no access'."
        failed=1
    fi

    [[ $failed -eq 0 ]] || { printf '\n  \033[31mPreflight failed — nothing was changed.\033[0m\n'; exit 1; }
    printf '\n  \033[32mPreflight passed.\033[0m\n'
}

publish() {
    head_ "Generating the public repository"
    ./scripts/make_public_addon_repo.sh

    local out="$ROOT/build/public-addon"

    # The Supervisor builds from a clone of that folder, so anything the
    # Dockerfile COPYs has to be inside it — one level up does not exist.
    local missing=0
    while read -r src; do
        [[ -e "$out/$SLUG/${src%/}" ]] || { bad "Dockerfile COPYs $src, which is not in the context"; missing=1; }
    done < <(grep -E '^COPY' "$out/$SLUG/Dockerfile" | awk '{print $2}')
    [[ $missing -eq 0 ]] && ok "every COPY source is present" || exit 1

    # An `image:` key would make the Supervisor pull instead of build, and
    # there is no image to pull.
    grep -q '^image:' "$out/$SLUG/config.yaml" \
        && { bad "config.yaml has an image: key — the Supervisor would try to pull"; exit 1; } \
        || ok "no image: key (the Supervisor builds)"

    head_ "Publishing to $ADDON_REPO"
    ( cd "$out"
      rm -rf .git
      git init -q && git checkout -q -b main
      git add -A
      git -c user.name="$OWNER" -c user.email="$OWNER@users.noreply.github.com" \
          commit -q -m "FranklinWH HA Integrator add-on $VERSION

Generated from $SRC_REPO by scripts/make_public_addon_repo.sh.
Do not edit here; edit there and regenerate."
      git remote add origin "https://github.com/$OWNER/$ADDON_REPO.git"
    )

    info "a single orphan commit replaces the contents — history is not kept,"
    info "because this repository is generated rather than authored"
    confirm "Force-push $VERSION to $ADDON_REPO?" || { info "not pushed"; return 0; }
    ( cd "$out" && git push -q --force origin main )
    ok "pushed"

    head_ "Verifying as Home Assistant will see it"

    # Checked through the API, not raw.githubusercontent.com.
    #
    # raw is a CDN and caches for minutes: it reported the previous version
    # seconds after a successful push, which reads as a failed publish and
    # invites re-pushing something that is already there. The Supervisor clones
    # over git and sees the new commit immediately, so raw was never what it
    # sees either.
    local base="repos/$OWNER/$ADDON_REPO/contents"
    for f in repository.yaml "$SLUG/config.yaml" "$SLUG/Dockerfile" "$SLUG/CHANGELOG.md"; do
        if gh api "$base/$f" --jq .sha >/dev/null 2>&1; then
            ok "$f present"
        else
            bad "$f is missing from the published repository"
        fi
    done
    local published
    published="$(gh api "$base/$SLUG/config.yaml" --jq .content 2>/dev/null \
                 | base64 -d 2>/dev/null | sed -n 's/^version: *//p' | tr -d '"')"
    [[ "$published" == "$VERSION" ]] && ok "published version is $VERSION" \
        || bad "published version is '$published', expected $VERSION"

    # Anonymous reachability still matters — it is what makes the repo usable —
    # but it is a separate question from whether the push landed.
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' "https://github.com/$OWNER/$ADDON_REPO")"
    [[ "$code" == "200" ]] && ok "repository reachable anonymously" \
        || bad "repository returns HTTP $code anonymously"

    printf '\n  Home Assistant: Settings → Add-ons → Add-on Store → ⋮\n'
    printf '    already listed → Check for updates\n'
    printf '    not listed     → Repositories → https://github.com/%s/%s\n' "$OWNER" "$ADDON_REPO"
}

preflight
[[ "$STAGE" == "preflight" ]] || publish
