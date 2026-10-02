"""Per-Pi role overlay for config.json — pure functions, wired up by app.py.

Model
-----
One config.json can drive both Pis. Top-level keys are shared defaults; an
optional "roles": {"rack": {...}, "venue": {...}} block holds per-role
overrides overlaid at load (effective = top-level (+) roles[active]). The
active role is the gitignored one-word `pi_role` file.

Two classes of per-Pi keys
--------------------------
* DEVICE_KEYS follow the PHYSICAL Pi (its touchscreen / kiosk), never the
  role: they always live at the top level, so flipping rack <-> venue keeps
  the same touch grid, custom faders and kiosk PIN. Older files that stored
  them inside a role block are migrated at boot by hoist_device_keys().
* ROLE_KEYS are operational (output routing, show selection, remote map).
  They fold into roles[active] when they differ from the shared default, and
  seed_role_block() copies the current values into a role's block when you
  switch to it, so a switch never silently changes what goes out on the wire.
"""
import copy

ROLES = ("rack", "venue")

DEVICE_KEYS = frozenset({
    "touch_grid", "touch_reload_ts", "custom_faders", "kiosk_pin",
})

ROLE_KEYS = frozenset({
    "active_show",
    "dmx_driver", "dmx_port", "artnet_target", "artnet_universe",
    "sacn_target", "sacn_universe", "sacn_priority", "sacn_multicast",
    "remote_universe_map", "remote_timeout_s",
})


def _roles(raw):
    r = raw.get("roles")
    return r if isinstance(r, dict) else None


def _prune(raw):
    """Drop empty role blocks, and the roles key itself when empty."""
    roles = _roles(raw)
    if roles is None:
        return
    for name in [n for n, b in roles.items() if isinstance(b, dict) and not b]:
        roles.pop(name)
    if not roles:
        raw.pop("roles", None)


def merge_role(raw, role):
    """Effective config = shared top level overlaid by roles[role].
    Device keys are never taken from a role block."""
    eff = {k: v for k, v in raw.items() if k != "roles"}
    blk = (_roles(raw) or {}).get(role) or {}
    eff.update({k: v for k, v in blk.items() if k not in DEVICE_KEYS})
    return eff


def hoist_device_keys(raw, role):
    """Move DEVICE_KEYS out of role blocks to the top level (in place).

    Precedence for each key: the active role's copy, else another role's
    copy, else the existing top-level value. Role-block copies win over the
    top level because they only ever got there from a deliberate save.
    Returns the sorted list of keys whose top-level value was replaced."""
    roles = _roles(raw)
    if not roles:
        return []
    order = [role] + [n for n in roles if n != role]
    moved = []
    for k in sorted(DEVICE_KEYS):
        for name in order:
            blk = roles.get(name)
            if isinstance(blk, dict) and k in blk:
                raw[k] = blk[k]
                moved.append(k)
                break
        for blk in roles.values():
            if isinstance(blk, dict):
                blk.pop(k, None)
    _prune(raw)
    return moved


def fold_config(raw, cfg, role):
    """Fold the runtime effective config back into raw (in place).

    Device keys and shared keys go to the top level. A ROLE_KEY goes into
    roles[role] if it already lives there or differs from the shared default.
    The other role's block is left untouched."""
    roles = raw.setdefault("roles", {})
    block = roles.setdefault(role, {})
    for k in DEVICE_KEYS:
        block.pop(k, None)
    top = {k: v for k, v in raw.items() if k != "roles"}
    for k, v in cfg.items():
        if k == "roles":
            continue
        if k not in DEVICE_KEYS and (k in block or (k in ROLE_KEYS and v != top.get(k))):
            block[k] = v
        else:
            raw[k] = v
    _prune(raw)


def seed_role_block(raw, cfg, new_role):
    """Before switching to new_role, copy the current effective ROLE_KEYS
    into roles[new_role] wherever that block doesn't define them and the
    value differs from the shared default (in place). Keys the target role
    already defines are kept, so roles that were deliberately set up
    differently stay that way. Returns the sorted list of seeded keys."""
    top = {k: v for k, v in raw.items() if k != "roles"}
    roles = raw.setdefault("roles", {})
    block = roles.setdefault(new_role, {})
    seeded = []
    for k in sorted(ROLE_KEYS):
        if k in cfg and k not in block and cfg[k] != top.get(k):
            block[k] = copy.deepcopy(cfg[k])
            seeded.append(k)
    _prune(raw)
    return seeded
