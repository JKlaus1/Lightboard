"""Tests for config_roles: touchscreen keys follow the physical Pi, and a
rack <-> venue role switch is lossless.

Run: python3 test_roles.py
"""
import copy
import json
import sys
sys.path.insert(0, ".")

import config_roles as cr

PASS = 0


def check(cond, msg):
    global PASS
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    PASS += 1


def boot(raw, role):
    """Mimic app.py startup: hoist, then merge."""
    cr.hoist_device_keys(raw, role)
    return cr.merge_role(raw, role)


# Shape of the Venue Pi's real config.json (2026-10-02): kiosk set up while
# pi_role=rack, a stray kiosk_pin left in roles.venue by a brief switch.
GRID = {"cols": 3, "rows": 4, "font_size": 14,
        "cells": [{"scene_id": "abc", "scene_type": "main"}], "exclusive_mode": True}
FADERS = [{"id": "f1", "label": "Wash", "level": 0.6}]
VENUE_FILE = {
    "dmx_port": "/dev/ttyUSB0", "shows_dir": "shows", "active_show": "harwood_lights",
    "web_host": "0.0.0.0", "web_port": 5000, "dmx_driver": "artnet",
    "artnet_target": "10.0.0.50", "artnet_universe": 0,
    "touch_grid": {"cols": 2, "rows": 6, "font_size": 13, "cells": []},
    "touch_reload_ts": 0,
    "roles": {
        "rack": {"artnet_target": ["10.42.0.5", "10.0.0.60"], "custom_faders": FADERS,
                 "kiosk_pin": "123", "touch_grid": GRID, "touch_reload_ts": 1720000000.5},
        "venue": {"kiosk_pin": "999"},
    },
}
WATCH = sorted(cr.DEVICE_KEYS | cr.ROLE_KEYS)


def snap(cfg):
    return {k: copy.deepcopy(cfg.get(k)) for k in WATCH}


# 1. Before the patch: what the Pi shows under rack (old merge semantics).
old_eff = {k: v for k, v in VENUE_FILE.items() if k != "roles"}
old_eff.update(VENUE_FILE["roles"]["rack"])
expected = snap(old_eff)

# 2. Boot with the patch as rack: nothing visible changes.
raw = copy.deepcopy(VENUE_FILE)
moved = cr.hoist_device_keys(raw, "rack")
check(set(moved) == {"touch_grid", "touch_reload_ts", "custom_faders", "kiosk_pin"},
      f"hoisted keys {moved}")
eff = cr.merge_role(raw, "rack")
check(snap(eff) == expected, "rack effective config unchanged after hoist")
check(raw["touch_grid"] == GRID and raw["kiosk_pin"] == "123",
      "active role's touch grid / PIN win (not venue's stray 999)")
check("venue" not in raw.get("roles", {}), "empty venue block pruned")
for blk in raw.get("roles", {}).values():
    check(not (set(blk) & cr.DEVICE_KEYS), "no device keys left in role blocks")

# 3. Switch rack -> venue, restart: identical touchscreen AND output.
seeded = cr.seed_role_block(raw, eff, "venue")
check("artnet_target" in seeded, f"artnet_target seeded ({seeded})")
check("touch_grid" not in seeded, "device keys never seeded into a role")
raw = json.loads(json.dumps(raw))          # round-trip through disk
eff_v = boot(raw, "venue")
check(snap(eff_v) == expected, "venue effective config == rack's (lossless switch)")

# 4. Edit the touch grid while venue; switch back to rack: edit is kept.
eff_v["touch_grid"] = dict(GRID, cols=4)
eff_v["kiosk_pin"] = "456"
cr.fold_config(raw, eff_v, "venue")
check(raw["touch_grid"]["cols"] == 4 and raw["kiosk_pin"] == "456",
      "touch edits under venue land at top level")
check(not (set(raw["roles"].get("venue", {})) & cr.DEVICE_KEYS),
      "fold never writes device keys into a role block")
cr.seed_role_block(raw, eff_v, "rack")
eff_r = boot(json.loads(json.dumps(raw)), "rack")
check(eff_r["touch_grid"]["cols"] == 4 and eff_r["kiosk_pin"] == "456",
      "rack sees the grid/PIN edited under venue")

# 5. Role routing still diverges when deliberately set.
eff_v["artnet_target"] = "10.0.0.99"
cr.fold_config(raw, eff_v, "venue")
check(cr.merge_role(raw, "venue")["artnet_target"] == "10.0.0.99", "venue routing edit")
check(cr.merge_role(raw, "rack")["artnet_target"] == ["10.42.0.5", "10.0.0.60"],
      "rack routing untouched by venue edit")
pre = copy.deepcopy(raw["roles"]["venue"])
cr.seed_role_block(raw, cr.merge_role(raw, "rack"), "venue")
check(raw["roles"]["venue"] == pre, "seeding never overwrites a key the role defines")

# 6. Legacy file with no roles block (the rack's committed config): no-ops.
with open("config.json") as f:
    legacy = json.load(f)
lr = copy.deepcopy(legacy)
check(cr.hoist_device_keys(lr, "rack") == [] and lr == legacy, "hoist no-op on legacy file")
le = cr.merge_role(lr, "rack")
cr.fold_config(lr, le, "rack")
check(lr == legacy, "fold of unchanged legacy config is byte-identical (no roles key added)")
le2 = dict(le, touch_grid={"cols": 9})
cr.fold_config(lr, le2, "rack")
check("roles" not in lr and lr["touch_grid"] == {"cols": 9}, "legacy touch edit stays top-level")

# 7. Hoist falls back to another role's copy when the active role lacks it.
r2 = {"touch_grid": {"cols": 2}, "roles": {"rack": {"touch_grid": GRID}, "venue": {"x": 1}}}
cr.hoist_device_keys(r2, "venue")
check(r2["touch_grid"] == GRID, "other role's saved grid beats the top-level default")

print(f"test_roles: {PASS} checks passed")
