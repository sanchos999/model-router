# Rollback

    scripts/rollback.sh r7-legacy   # restore the previous deployment tree
    scripts/rollback.sh git-tag <tag>  # checkout an older release in-place

## r7-legacy mode

Restores the pre-V1 deployment from the legacy tree (kept frozen as
FROZEN_LEGACY_ROLLBACK with its git tag model-router-v1-pre-release):
stops the new units, starts the legacy units, verifies :4100 health.

Legacy tree status: do not run production from it after acceptance,
except for an emergency rollback. Deletion is a separate future decision.

## git-tag mode

git checkout <previous-tag> in the product tree, then scripts/upgrade.sh
reinstalls deps and restarts. Control DB schema versioning protects
against new-code data being read by old code (future-schema DB -> refuse
to start).

## Config rollback

Independent of code: POST /admin/config/revisions/{rid}/rollback restores
a previous policy revision (creates a new revision, audited).
