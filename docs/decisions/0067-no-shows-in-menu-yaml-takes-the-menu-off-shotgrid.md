# 0067. No shows in menu.yaml takes the farm's menu off ShotGrid

Date: 2026-11-25. Status: accepted.

## Context

The manager keeps ShotGrid's right-click menu in step with `deploy/shotgrid/menu.yaml`
(`shotgrid.menu_sync`, README: ShotGrid right-click). On 25 November Peter took every show out
of `menu.yaml` to take the menu off ShotGrid. The manager didn't sync it: the sync runs the
pipeline's tool in a show's context (`set_context.sh {show}`), the show was `menu.yaml`'s
first, and there was none. The log said "ShotGrid menu not synced: no show for the pipeline's
context". With the shows back, the next restart synced as it should.

Peter then asked: "when no shows are listed, remove all menus from shotgrid action".

`tools/sg_ami.py` already deletes every item posting to the farm that the farm's menu
doesn't have, so a menu with no items takes them all off. Only the context's show was
missing.

## Decision

- **A `menu.yaml` that names no shows takes every ComfyFarm item off ShotGrid** at the next
  sync: 20 s after a restart, or at the next check when it changed.
- **The pipeline's context, when no show is named:** `shotgrid.menu_sync.show` if set, else
  the show the last good sync on this manager used. The manager keeps that show in its data
  folder, `shotgrid_menu_sync.json`, after each good sync, so it is still known after a
  restart. A show the context has already worked with is the safest one to use again.
- **None known** (no good sync on this manager yet, and no `menu_sync.show`): it isn't
  synced, and the status and the log say to set `shotgrid.menu_sync.show`.
- What stops a sync stays as it was: a menu with problems (a `menu.yaml` that isn't there or
  can't be read), no `farm_url` (an empty file), or shows named with no items (the workflows
  didn't load). So only shows taken out on purpose remove the menu.
- No new switch: this is inside `shotgrid.menu_sync`, which stays off by default.

## Consequences

- Taking the menu off ShotGrid and putting it back is an edit of `menu.yaml` and a restart of
  the manager.
- To hide a few workflows rather than the whole menu, `disabled_workflows` (0066) is the way.
