#!/usr/bin/env python3
"""Off-hardware tests for wifi_routes.py: the Internet-via selector (which eth0 profile it edits,
netplan boxes, static Lightboard profiles), default-route parsing, and the connect retry after a
handshake timeout ("Secrets were required"). nmcli / ip are faked -- nothing touches the network.

  python3 test_wifi_routes.py
"""
import sys

import wifi_routes as w

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    print("default route parsing")
    w._run = lambda args, timeout=20: (0, "default via 192.168.0.1 dev eth0 proto dhcp src 192.168.0.132 metric 100\n"
                                         "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.23 metric 600\n", "")
    r = w._default_route()
    check("eth0 (metric 100) beats wlan0 (600)", r["dev"] == "eth0" and r["via"] == "192.168.0.1", str(r))
    w._run = lambda args, timeout=20: (0, "", "")
    check("no default route -> None", w._default_route()["dev"] is None)

    # ── selector, with a fake NetworkManager ──
    profs, active, calls, route = {}, {"eth": None, "wlan": "Venue Guest"}, [], {"dev": "wlan0", "via": "10.0.0.1"}

    def fake_run(args, timeout=20):
        calls.append(args)
        if args[:4] == ["ip", "-4", "route", "show"]:
            return (0, f"default via {route['via']} dev {route['dev']} metric 600\n" if route["dev"] else "", "")
        if args[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,FILENAME"]:
            lines = ["Venue Guest:802-11-wireless:wlan0:/etc/NetworkManager/system-connections/vg.nmconnection"]
            for n, p in profs.items():
                lines.append(f"{n}:802-3-ethernet:{'eth0' if active['eth'] == n else ''}:{p['file']}")
            return 0, "\n".join(lines) + "\n", ""
        if args[:4] == ["nmcli", "-t", "-f", "connection.interface-name,ipv4.method,ipv4.never-default"]:
            p = profs[args[-1]]
            return 0, f"connection.interface-name:{p['iface']}\nipv4.method:{p['method']}\nipv4.never-default:{p['nd']}\n", ""
        if args[:3] == ["nmcli", "connection", "modify"] and "ipv4.never-default" in args:
            profs[args[3]]["nd"] = args[args.index("ipv4.never-default") + 1]
            return 0, "", ""
        if args[:3] == ["nmcli", "connection", "add"]:
            n = args[args.index("con-name") + 1]
            profs[n] = {"iface": args[args.index("ifname") + 1], "method": "auto",
                        "nd": args[args.index("ipv4.never-default") + 1],
                        "file": "/etc/NetworkManager/system-connections/" + n + ".nmconnection"}
            return 0, "", ""
        if args[:3] == ["nmcli", "connection", "up"] and args[3] in profs:
            active["eth"] = args[3]
            return 0, "", ""
        return 0, "", ""
    w._run = fake_run

    print("selector: plain mixer box (mixer-network active)")
    profs["mixer-network"] = {"iface": "eth0", "method": "auto", "nd": "yes", "file": "/etc/NetworkManager/system-connections/mixer-network.nmconnection"}
    active["eth"] = "mixer-network"
    i = w.internet_source()
    check("reads wifi mode", i["mode"] == "wifi" and i["eth_profile"] == "mixer-network" and i["effective"] == "wifi", str(i))
    del calls[:]
    ok, msg, prof = w.set_internet_source("ethernet")
    check("ethernet: modified in place", ok and prof == "mixer-network" and profs["mixer-network"]["nd"] == "no", msg)
    check("same active profile -> device reapply, no link drop", ["nmcli", "device", "reapply", "eth0"] in calls
          and not any(c[:3] == ["nmcli", "connection", "up"] for c in calls), str(calls))
    route.update(dev="eth0", via="192.168.0.1")
    i = w.internet_source()
    check("effective ethernet", i["mode"] == "ethernet" and i["effective"] == "ethernet", str(i))
    ok, msg, prof = w.set_internet_source("wifi")
    check("back to wifi", ok and profs["mixer-network"]["nd"] == "yes", msg)
    check("bad mode refused", w.set_internet_source("usb")[0] is False)
    route.update(dev="wlan0", via="10.0.0.1")

    print("selector: netplan box -- ephemeral eth0 profile")
    profs.clear()
    profs["netplan-eth0"] = {"iface": "eth0", "method": "auto", "nd": "no", "file": "/run/NetworkManager/system-connections/netplan-eth0.nmconnection"}
    active["eth"] = "netplan-eth0"
    check("reports the netplan profile, ethernet mode", w.internet_source()["mode"] == "ethernet")
    del calls[:]
    ok, msg, prof = w.set_internet_source("wifi")
    check("made mixer-network instead of trusting netplan-eth0", ok and prof == "mixer-network" and profs["mixer-network"]["nd"] == "yes", msg)
    add = next(c for c in calls if c[:3] == ["nmcli", "connection", "add"])
    check("priority 10", add[add.index("connection.autoconnect-priority") + 1] == "10")
    check("netplan twin followed along", profs["netplan-eth0"]["nd"] == "yes")
    check("brought up now", active["eth"] == "mixer-network")

    print("selector: Lightboard rig (static Art-Net eth0 profile) -- edited in place, never replaced")
    profs.clear()
    profs["lightboard-artnet"] = {"iface": "eth0", "method": "manual", "nd": "yes", "file": "/etc/NetworkManager/system-connections/lb.nmconnection"}
    active["eth"] = "lightboard-artnet"
    ok, msg, prof = w.set_internet_source("ethernet")
    check("static profile modified, no mixer-network made", ok and prof == "lightboard-artnet" and "mixer-network" not in profs
          and profs["lightboard-artnet"]["nd"] == "no", msg)
    w.set_internet_source("wifi")
    check("and back", profs["lightboard-artnet"]["nd"] == "yes")

    # ── connect retry ──
    print("connect: handshake timeout on the first try -> retried once, then online")
    w._scan_sec["MixedNet"] = "WPA2 WPA3"
    w._internet_ok = lambda: True
    w.saved_wifi_profiles = lambda: []

    def flaky_run(args, timeout=20):
        calls.append(args)
        if args[:3] == ["nmcli", "device", "wifi"] and "connect" in args:
            return 4, "", "Error: Connection activation failed: Secrets were required, but not provided."
        if args[:3] == ["nmcli", "connection", "up"]:
            active["wlan"] = args[3]
        return 0, "", ""
    w._run = flaky_run
    del calls[:]
    w._do_connect("MixedNet", "pw", "Old Net")
    check("ended in success", w._connect_state["status"] == "success", w._connect_state["status"])
    check("retried with `connection up` on the profile NM left behind",
          ["nmcli", "connection", "up", "MixedNet", "ifname", "wlan0"] in calls, str(calls))
    check("mixed WPA2/WPA3: pinned to WPA2-PSK first",
          ["nmcli", "connection", "modify", "MixedNet", "wifi-sec.key-mgmt", "wpa-psk"] in calls)
    check("profile not deleted", not any(c[:3] == ["nmcli", "connection", "delete"] for c in calls))

    print("connect: wrong password fails both tries")
    def wrong_run(args, timeout=20):
        calls.append(args)
        if args[:3] == ["nmcli", "connection", "up"] and args[3] == "Old Net":
            active["wlan"] = "Old Net"
            return 0, "", ""
        if (args[:3] == ["nmcli", "device", "wifi"] and "connect" in args) or args[:3] == ["nmcli", "connection", "up"]:
            return 4, "", "Error: Connection activation failed: Secrets were required, but not provided."
        return 0, "", ""
    w._run = wrong_run
    w._scan_sec["Venue2"] = "WPA2"
    del calls[:]
    w._do_connect("Venue2", "bad", "Old Net")
    check("failed, says tried twice", w._connect_state["status"] == "failed" and "tried twice" in w._connect_state["detail"],
          w._connect_state["detail"])
    check("half-made profile dropped, back on the old network",
          ["nmcli", "connection", "delete", "Venue2"] in calls and active["wlan"] == "Old Net")
    check("WPA2-only: no key-mgmt pin", not any("wifi-sec.key-mgmt" in c for c in calls))

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
