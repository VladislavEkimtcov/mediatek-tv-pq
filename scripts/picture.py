#!/usr/bin/env python3
"""dumbtv picture: inspect and monitor Picture Quality (PQ) settings per input.

Works on Fire OS / MediaTek televisions over ADB with no root.
Reads:
  1. Live active input state and hardware parameters from the TV.
  2. Factory baseline preset databases from /vendor/tvconfig/.../presetdb/.
  3. Real-time setting adjustments via --watch as you configure an input with the remote.

A preset that cannot be found is an error, never an empty result: an empty preset would
read as "no differences" or "nothing customized".
"""

import argparse
import json
import os
import re
import subprocess
import sys

# Key parameters grouped by feature domain
PARAM_CATEGORIES = {
    "Mode & Gaming": [
        ("Picture_Mode", "Picture Mode"),
        ("ALLM", "Auto Low Latency Mode (ALLM)"),
        ("PC_Mode", "PC Mode (4:4:4 bypass)"),
        ("Game_Mode", "Game Mode"),
    ],
    "Clarity & Sharpness": [
        ("SR_Strength", "Sharpness (VIP Peaking)"),
        ("AISR", "Super Resolution (AISR)"),
        ("EDGE_ENHANCER", "Edge Enhancer"),
        ("ULTRA_CLEAR", "Ultra Clear Detail"),
    ],
    "Noise Reduction": [
        ("MPEG_NR", "MPEG Noise Reduction (DBK)"),
        ("NR", "Noise Reduction (Spatial/Temporal)"),
        ("ABF", "Debanding Filter (ABF)"),
    ],
    "Motion & Cadence": [
        ("MJC_Effect", "Motion Smoothing (MEMC)"),
        ("MJC_Level_Cus", "MEMC Custom Level"),
        ("MJC_Dejudder", "De-judder"),
        ("MJC_Deblur", "De-blur"),
        ("Real_Cinema", "Natural Cinema (24p pull-down)"),
    ],
    "Contrast & Brightness": [
        ("Backlight", "Backlight Level"),
        ("Brightness", "Brightness (Black Level)"),
        ("Contrast", "Contrast (White Level)"),
        ("LD_MODE", "Local Dimming"),
        ("DLC_STRENGTH", "Dynamic Contrast"),
        ("Local_Contrast", "Local Contrast Enhancement"),
        ("HDR_ENHANCER", "HDR Contrast Enhancer"),
        ("Gamma", "Gamma Curve"),
    ],
    "Color & Tint": [
        ("Color_Temperature", "Color Temperature"),
        ("Saturation", "Color Saturation"),
        ("Hue", "Tint / Hue"),
        ("Dynamic_Color_Booster", "Dynamic Color"),
        ("Flesh_Tone", "Flesh Tone Correction"),
        ("Color_Space", "Color Space / Gamut"),
    ],
}

PORT_MAP = {
    "HW0": "DTV (Antenna)",
    "HW1": "Analog (Cable)",
    "HW2": "HDMI 1",
    "HW3": "HDMI 2",
    "HW4": "HDMI 3",
    "HW5": "Composite (AV)",
    "HDMI100004": "HDMI (CEC)",
}

# presetdb source names (<name>_initial.json) to try for each TIS hardware id, first
# match wins. Checked against the files actually on the TV, case-insensitively. The
# analog and composite names are guesses; when none exists the error lists what does.
# HDMI100004 (a CEC session) is absent on purpose: the HDMI port behind it comes from
# the log's "Current Source:" line instead.
HW_SOURCES = {
    "HW0": ["DTV"],
    "HW1": ["ATV", "Analog"],
    "HW2": ["HDMI1"],
    "HW3": ["HDMI2"],
    "HW4": ["HDMI3"],
    "HW5": ["AV", "Composite", "CVBS"],
}

PRESET_SUFFIX = "_initial.json"
CURRENT_SOURCE_RE = re.compile(r"Current Source:\s*([A-Za-z0-9_]+)")
PQCACHE_CONTEXT_RE = re.compile(r"<([A-Za-z0-9_]+),\s*([A-Za-z0-9_]+),\s*([A-Za-z0-9_]+)")
PQCACHE_SET_RE = re.compile(r"set\(([A-Za-z0-9_]+),\s*([^)]+)\)")
OEM_SET_RE = re.compile(r"defaultSetCall, successfully set\s*:\s*([A-Za-z0-9_]+)\s*:\s*(.+)")
OEM_GET_RE = re.compile(r"defaultGetCall, fetched value for\s+([A-Za-z0-9_]+)\s*:\s*(.+)")


class PresetLookupError(Exception):
    """A preset source, mode or HDR format that is not in the TV's presetdb."""


class DeviceError(Exception):
    """adb cannot reach exactly one TV, or a command on it failed."""


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def norm(name):
    """Compare names the way a person types them: case, spaces, '-' and '_' ignored."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def pick(name, keys, what, where):
    """The entry of `keys` that `name` means (exact, then normalized), or PresetLookupError."""
    keys = [k for k in keys if not str(k).startswith("$")]
    if name in keys:
        return name
    for k in keys:
        if norm(k) == norm(name):
            return k
    avail = ", ".join(sorted(keys)) or "none"
    raise PresetLookupError(f"no {what} '{name}' in {where}; available: {avail}")


def model_dir_name(model):
    """ro.vendor.sys.idme.mfr_model -> the presetdb model directory: NS-55F501NA26 -> NS55F501."""
    model = model.strip().replace("-", "")
    return re.sub(r"(NA|CA)\d{2}$", "", model, flags=re.IGNORECASE)


def flatten(d, prefix=""):
    """Nested preset values as dotted keys, so --diff compares every leaf.

    A table that names its own selection ({"Standard": {...}, "Color_Temperature":
    "Standard"} under Color_Temperature) keeps that selection under the plain key."""
    out = {}
    for k, v in d.items():
        if prefix and k == prefix.split(".")[-1]:
            key = prefix
        else:
            key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def parse_pq_line(line):
    """Events in one logcat line, in the order they apply:
    ("source", name)            an input switch
    ("context", src, hdr, mode) a PqCache <src, hdr, mode> line
    ("value", key, value)       a PQ value fetched or set (OEMService or PqCache)
    """
    events = []
    m = CURRENT_SOURCE_RE.search(line)
    if m:
        events.append(("source", m.group(1)))
    if "PqCache" in line:
        m = PQCACHE_CONTEXT_RE.search(line)
        if m:
            events.append(("context", m.group(1), m.group(2), m.group(3)))
        m = PQCACHE_SET_RE.search(line)
        if m and m.group(1) != "Source":
            events.append(("value", m.group(1), m.group(2).strip()))
    for rx in (OEM_SET_RE, OEM_GET_RE):
        m = rx.search(line)
        if m:
            events.append(("value", m.group(1), m.group(2).strip()))
    return events


class Colors:
    def __init__(self, enabled=True):
        self.enabled = enabled and sys.stdout.isatty()

    def blue(self, s): return f"\033[94m{s}\033[0m" if self.enabled else s
    def green(self, s): return f"\033[92m{s}\033[0m" if self.enabled else s
    def yellow(self, s): return f"\033[93m{s}\033[0m" if self.enabled else s
    def red(self, s): return f"\033[91m{s}\033[0m" if self.enabled else s
    def bold(self, s): return f"\033[1m{s}\033[0m" if self.enabled else s
    def dim(self, s): return f"\033[2m{s}\033[0m" if self.enabled else s


class TvPictureQuality:
    def __init__(self, serial=None, adb_bin=None, color=True):
        self.adb = adb_bin or os.environ.get("ADB", "adb")
        self.serial = serial or os.environ.get("ADB_SERIAL") or os.environ.get("SERIAL")
        self.colors = Colors(color)
        self._preset_dir = None
        self._preset_sources = None
        self._preset_db = {}
        self._logcat = None

    def _adb_cmd(self, *args):
        cmd = [self.adb]
        if self.serial:
            cmd.extend(["-s", self.serial])
        cmd.extend(args)
        return cmd

    def adb_result(self, *args):
        """(returncode, stdout, stderr) of one adb command on the TV."""
        try:
            res = subprocess.run(self._adb_cmd(*args), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise DeviceError(f"adb not found ({self.adb}); install platform-tools or set ADB")
        return res.returncode, res.stdout, res.stderr

    def run_adb(self, *args, check=True):
        rc, out, err = self.adb_result(*args)
        if check and rc != 0:
            raise DeviceError(f"adb {' '.join(args)} failed ({rc}): {err.strip() or out.strip()}")
        return out

    def check_device(self):
        """Exactly one reachable TV, as scripts/lib.sh's need_device requires."""
        try:
            res = subprocess.run([self.adb, "devices"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise DeviceError(f"adb not found ({self.adb}); install platform-tools or set ADB")
        if res.returncode != 0:
            raise DeviceError(f"adb devices failed: {res.stderr.strip()}")
        states = {}
        for line in res.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
        if self.serial:
            state = states.get(self.serial)
            if state is None:
                raise DeviceError(f"device {self.serial} is not attached; adb connect {self.serial}")
            if state != "device":
                raise DeviceError(f"device {self.serial} is {state}; on the TV, allow ADB debugging "
                                  "or toggle it off and on")
            return self.serial
        found = [s for s, st in states.items() if st == "device"]
        if not found:
            raise DeviceError("no adb device. Enable ADB debugging on the TV, then: adb connect <tv-ip>:5555")
        if len(found) > 1:
            raise DeviceError(f"several devices attached; set ADB_SERIAL to the one you mean: {' '.join(found)}")
        self.serial = found[0]
        return self.serial

    def logcat(self):
        if self._logcat is None:
            self._logcat = self.run_adb("logcat", "-d")
        return self._logcat

    def last_logged_source(self):
        for line in reversed(self.logcat().splitlines()):
            m = CURRENT_SOURCE_RE.search(line)
            if m:
                return m.group(1)
        return None

    def get_active_input(self):
        """Identify what input is currently tuned and active on screen."""
        out = self.run_adb("shell", "dumpsys", "tv_input")
        active_id = None
        active_hw = None
        active_port = "Unknown"

        m = re.search(r"inputId:\s*com\.mediatek\.tis/\.(HdmiInputService|CompositeInputService|AnalogInputService|AtscTunerInputService)/([A-Za-z0-9]+)", out)
        if m:
            active_id = m.group(0).split()[-1]
            active_hw = m.group(2)
            active_port = PORT_MAP.get(active_hw, active_hw)

        candidates = list(HW_SOURCES.get(active_hw, []))
        if not candidates:
            # No session, or one (HDMI-CEC) whose port the id does not say: use the log.
            logged = self.last_logged_source()
            if logged:
                candidates = [logged]
                if active_port == "Unknown":
                    active_port = logged

        return {
            "id": active_id,
            "port": active_port,
            "hw": active_hw,
            "source_candidates": candidates,
        }

    def active_preset_source(self, active_input=None):
        """The presetdb source name for what is on screen, or PresetLookupError."""
        info = active_input or self.get_active_input()
        cands = info.get("source_candidates") or []
        if not cands:
            raise PresetLookupError("cannot tell which input is active (no TV input session, no "
                                    "'Current Source:' in the log); pass --source")
        sources = self.list_preset_sources()
        for c in cands:
            for s in sources:
                if norm(s) == norm(c):
                    return s
        raise PresetLookupError(f"no preset file for {info.get('port')} (tried {', '.join(cands)}) "
                                f"in {self._preset_dir}; available: {', '.join(sources) or 'none'}")

    def get_live_parameters(self):
        """The PQ values last fetched or set for the current input, from the log.

        Values logged before the last input switch belong to another input and are dropped."""
        params = {}
        source = hdr = ctx_mode = None
        for line in self.logcat().splitlines():
            for ev in parse_pq_line(line):
                if ev[0] == "source":
                    if source is None or norm(ev[1]) != norm(source):
                        params, hdr, ctx_mode = {}, None, None
                    source = ev[1]
                elif ev[0] == "context":
                    source = source or ev[1]
                    hdr, ctx_mode = ev[2], ev[3]
                else:
                    params[ev[1]] = ev[2]

        return {
            "source": source,
            "mode": params.get("Picture_Mode") or ctx_mode,
            "hdr": params.get("HdrType") or hdr,
            "params": params,
        }

    def find_preset_dir(self):
        """Locate this model's /vendor/tvconfig/.../pq/presetdb directory on the TV."""
        if self._preset_dir:
            return self._preset_dir

        model = self.run_adb("shell", "getprop", "ro.vendor.sys.idme.mfr_model").strip()
        model_dir = model_dir_name(model) if model else ""

        out = self.run_adb("shell", "ls -d /vendor/tvconfig/*/*/pq/presetdb "
                           "/vendor/tvconfig/*/*/*/pq/presetdb 2>/dev/null", check=False)
        found = [l.strip() for l in out.splitlines() if l.strip().endswith("/presetdb")]
        if not found:
            return None

        if model_dir:
            for d in found:
                if any(part.lower() == model_dir.lower() for part in d.split("/")):
                    self._preset_dir = d
                    return d
        chosen = next((d for d in found if "/model/" in d), found[0])
        warn(f"no presetdb for model {model or 'unknown'} ({model_dir or '?'}); using {chosen}, "
             "which may be another model's or the chip's generic presets")
        self._preset_dir = chosen
        return chosen

    def require_preset_dir(self):
        d = self.find_preset_dir()
        if not d:
            raise PresetLookupError("no presetdb directory under /vendor/tvconfig on this TV")
        return d

    def list_preset_sources(self):
        """Source names that have a <name>_initial.json in the presetdb."""
        if self._preset_sources is None:
            d = self.require_preset_dir()
            out = self.run_adb("shell", "ls", d)
            self._preset_sources = sorted(
                n[:-len(PRESET_SUFFIX)] for n in (l.strip() for l in out.split()) if n.endswith(PRESET_SUFFIX))
        return self._preset_sources

    def resolve_source(self, name):
        return pick(name, self.list_preset_sources(), "preset source", self._preset_dir)

    def load_preset_db(self, source):
        """The parsed <source>_initial.json; `source` must already be resolved."""
        if source not in self._preset_db:
            path = f"{self.require_preset_dir()}/{source}{PRESET_SUFFIX}"
            rc, raw, err = self.adb_result("shell", "cat", path)
            if rc != 0 or not raw.strip():
                raise PresetLookupError(f"cannot read {path}: {err.strip() or raw.strip() or 'empty file'}")
            try:
                self._preset_db[source] = json.loads(raw)
            except ValueError as e:
                raise PresetLookupError(f"{path} is not valid JSON: {e}")
        return self._preset_db[source]

    def lookup_preset(self, source, mode, hdr):
        """Resolve names case-insensitively; returns (source, mode, hdr, settings)."""
        src = self.resolve_source(source)
        where = f"{src}{PRESET_SUFFIX}"
        pq_val = self.load_preset_db(src).get("Preset_PQ_Value", {})
        m = pick(mode, pq_val.keys(), "picture mode", where)
        by_hdr = pq_val[m]
        if not isinstance(by_hdr, dict):
            raise PresetLookupError(f"{where}: mode {m} is not a table of HDR formats")
        h = pick(hdr, by_hdr.keys(), "HDR format", f"{where} mode {m}")
        settings = by_hdr[h]
        if not isinstance(settings, dict):
            raise PresetLookupError(f"{where}: {m}/{h} is not a table of settings")
        return src, m, h, settings

    def get_preset_settings(self, source="HDMI1", mode="Standard", hdr="SDR"):
        """Factory preset for a source, picture mode and HDR format; raises PresetLookupError."""
        return self.lookup_preset(source, mode, hdr)[3]

    def get_available_preset_modes(self, source="HDMI1"):
        """All picture modes in a source's preset database."""
        src = self.resolve_source(source)
        pq_val = self.load_preset_db(src).get("Preset_PQ_Value", {})
        return [k for k in pq_val.keys() if not k.startswith("$")]

    def format_table(self, title, params, preset_params=None):
        """Format parameters cleanly grouped by category."""
        lines = []
        lines.append(self.colors.bold(f"=== {title} ==="))
        lines.append("")

        has_preset = preset_params is not None
        if has_preset:
            header = f"{'Setting':<38} {'Live / Active':<16} {'Preset Default':<16}"
        else:
            header = f"{'Setting':<38} {'Value':<20}"
        lines.append(self.colors.dim(header))
        lines.append(self.colors.dim("-" * len(header)))

        for category, items in PARAM_CATEGORIES.items():
            cat_lines = []
            for key, label in items:
                live_val = params.get(key)
                if live_val is None and not has_preset:
                    continue
                preset_val = preset_params.get(key) if has_preset else None
                if live_val is None and preset_val is None:
                    continue

                # Format nested dictionary for Color_Temperature if present
                if isinstance(live_val, dict):
                    live_val = live_val.get("Color_Temperature", "Standard")
                if isinstance(preset_val, dict):
                    preset_val = preset_val.get("Color_Temperature", "Standard")

                live_str = str(live_val) if live_val is not None else "-"
                if has_preset:
                    preset_str = str(preset_val) if preset_val is not None else "-"
                    diff_marker = "  "
                    formatted_live = f"{live_str:<16}"
                    if live_val is not None and preset_val is not None and str(live_val) != str(preset_val):
                        diff_marker = "* "
                        formatted_live = f"{self.colors.yellow(live_str):<25}"
                    row = f"  {label:<36} {formatted_live} {preset_str:<16} {diff_marker}"
                else:
                    row = f"  {label:<36} {self.colors.green(live_str):<20}"
                cat_lines.append(row)

            if cat_lines:
                lines.append(self.colors.bold(f"[{category}]"))
                lines.extend(cat_lines)
                lines.append("")

        return "\n".join(lines)

    def watch(self):
        """Stream real-time setting and input changes from logcat."""
        print(self.colors.bold("Monitoring Picture Quality changes in real time... (Ctrl+C to stop)"))
        print(self.colors.dim("Adjust picture settings with the TV remote to see live parameter updates."))
        print("-" * 75)

        cmd = self._adb_cmd("logcat", "-v", "time", "-T", "1")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

        try:
            for line in proc.stdout:
                line = line.strip()
                t = line[:18]
                if "Current Source:" in line:
                    m = CURRENT_SOURCE_RE.search(line)
                    if m:
                        print(f"[{t}] {self.colors.blue('INPUT SWITCH')}: Current Source -> {self.colors.bold(m.group(1))}")
                elif "defaultSetCall, successfully set" in line:
                    m = OEM_SET_RE.search(line)
                    if m:
                        k, v = m.group(1), m.group(2).strip()
                        print(f"[{t}] {self.colors.green('SET')}: {k:<25} = {self.colors.bold(v)}")
                elif "PqCache" in line and "set(" in line:
                    m = PQCACHE_SET_RE.search(line)
                    if m:
                        k, v = m.group(1), m.group(2).strip()
                        if k != "Source":
                            print(f"[{t}] {self.colors.yellow('PQ_CACHE')}: {k:<23} = {self.colors.bold(v)}")
        except KeyboardInterrupt:
            print("\nStopped monitoring.")
            return 0
        finally:
            if proc.poll() is None:
                proc.terminate()
        proc.wait()
        err = proc.stderr.read().strip()
        raise DeviceError(f"logcat stopped ({proc.returncode}){': ' + err if err else ''}")


def diff_presets(p1, p2):
    f1, f2 = flatten(p1), flatten(p2)
    return {k: (f1.get(k), f2.get(k)) for k in set(f1) | set(f2) if f1.get(k) != f2.get(k)}


def run(args, pq):
    pq.check_device()

    # 1. Real-time watch mode
    if args.watch:
        return pq.watch()

    hdr_arg = args.hdr or "SDR"

    # 2. Diff mode
    if args.diff:
        arg1, arg2 = args.diff
        sources = pq.list_preset_sources()
        is_src = [any(norm(a) == norm(s) for s in sources) for a in (arg1, arg2)]

        if all(is_src):
            mode = args.mode or "PC"
            s1, m1, h1, p1 = pq.lookup_preset(arg1, mode, hdr_arg)
            s2, _, _, p2 = pq.lookup_preset(arg2, mode, hdr_arg)
            title = f"Diff: {s1} vs {s2} (Mode: {m1}, HDR: {h1})"
            col1, col2 = s1, s2
        elif any(is_src):
            raise PresetLookupError(f"--diff takes two inputs or two picture modes, not one of each: {arg1} {arg2}")
        else:
            source = args.source or pq.active_preset_source()
            s1, m1, h1, p1 = pq.lookup_preset(source, arg1, hdr_arg)
            _, m2, _, p2 = pq.lookup_preset(source, arg2, hdr_arg)
            title = f"Diff: Mode {m1} vs {m2} ({s1}, HDR: {h1})"
            col1, col2 = m1, m2

        if args.json:
            print(json.dumps({"diff": {col1: p1, col2: p2}}, indent=2))
        else:
            diff_dict = diff_presets(p1, p2)
            print(pq.colors.bold(f"=== {title} ==="))
            if not diff_dict:
                print(pq.colors.green("No differences found."))
            else:
                print(f"{'Parameter':<35} {col1:<20} {col2:<20}")
                print("-" * 75)
                for k, (v1, v2) in sorted(diff_dict.items()):
                    print(f"  {k:<33} {str(v1):<20} {str(v2):<20}")
        return 0

    # 3. Preset inspection mode
    if args.preset:
        source = args.preset
        if source == "AUTO":
            source = args.source or pq.active_preset_source()
        src, mode, hdr, preset_data = pq.lookup_preset(source, args.mode or "PC", hdr_arg)
        if args.json:
            print(json.dumps({src: {mode: {hdr: preset_data}}}, indent=2))
        else:
            print(pq.format_table(f"Baseline Preset: {src} (Mode: {mode}, HDR: {hdr})", preset_data))
        return 0

    # 4. Default: Live active input inspection
    active_input = pq.get_active_input()
    live = pq.get_live_parameters()
    mode_key = args.mode or live.get("mode")
    hdr_key = args.hdr or live.get("hdr") or "SDR"

    preset_data, preset_error, source_key = None, None, None
    try:
        if args.source:
            source_key = pq.resolve_source(args.source)
        else:
            try:
                source_key = pq.active_preset_source(active_input)
            except PresetLookupError:
                if not live.get("source"):
                    raise
                source_key = pq.resolve_source(live["source"])
        if live.get("source") and norm(live["source"]) != norm(source_key) and not args.source:
            warn(f"the log's last input switch was to {live['source']} but {active_input['port']} is on "
                 f"screen; the live values may belong to {live['source']}")
        if not mode_key:
            raise PresetLookupError("the picture mode is not in the log; pass --mode to compare with a preset")
        source_key, mode_key, hdr_key, preset_data = pq.lookup_preset(source_key, mode_key, hdr_key)
    except PresetLookupError as e:
        preset_error = str(e)
        if not args.json:
            warn(f"no preset comparison: {preset_error}")

    if args.json:
        result = {
            "active_input": active_input,
            "live_profile": live,
            "preset_source": source_key,
            "preset_baseline": preset_data,
        }
        if preset_error:
            result["preset_error"] = preset_error
        print(json.dumps(result, indent=2))
    else:
        print(pq.colors.bold(f"Active Port: {active_input['port']} (Session ID: {active_input['id'] or 'running'})"))
        print(pq.colors.bold(f"Active Mode: {mode_key or 'unknown'} · Signal: {hdr_key}"))
        print(pq.colors.dim(f"Preset Source Key: {source_key or '-'}  ({pq._preset_dir or 'no presetdb'})"))
        print("")
        print(pq.format_table(f"Live Picture Settings ({active_input['port']})", live["params"], preset_data))

    return 0


def main(argv=None, pq=None):
    parser = argparse.ArgumentParser(description="dumbtv picture: inspect and monitor TV picture quality settings.")
    parser.add_argument("--serial", help="Target device serial (defaults to $ADB_SERIAL)")
    parser.add_argument("--watch", action="store_true", help="Monitor live setting changes in real time as you adjust them")
    parser.add_argument("--preset", nargs="?", const="AUTO", help="Inspect factory baseline presets (e.g. HDMI1, HDMI2, DTV; default: --source or the active input)")
    parser.add_argument("--source", help="Input source for --preset, mode --diff and the default view's preset (e.g. HDMI1, DTV, App; default: the active input)")
    parser.add_argument("--mode", help="Picture mode (default: PC for --preset and input --diff; the live mode for the default view)")
    parser.add_argument("--hdr", help="HDR format (default: SDR; the live signal for the default view)")
    parser.add_argument("--diff", nargs=2, metavar=("A", "B"), help="Diff factory presets of two inputs (HDMI1 HDMI2) or two picture modes (PC Standard)")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    args = parser.parse_args(argv)

    pq = pq or TvPictureQuality(serial=args.serial, color=not args.no_color)
    try:
        return run(args, pq)
    except (PresetLookupError, DeviceError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
