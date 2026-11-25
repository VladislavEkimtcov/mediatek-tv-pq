"""Unit tests for scripts/picture.py and picture settings inspection.

Run: pytest tests/test_picture.py
"""

from pathlib import Path
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import picture

PRESETDB = "/vendor/tvconfig/model/hydrot/NS55F501/pq/presetdb"
GENERIC = "/vendor/tvconfig/bsp/chip/mt5873/pq/presetdb"


def preset(sr, ct_gain=1024, pc_mode="On"):
    return {
        "PC_Mode": pc_mode,
        "SR_Strength": sr,
        "NR": "Off",
        "Color_Temperature": {
            "Standard": {"R_Gain": ct_gain, "G_Gain": 1024},
            "Color_Temperature": "Standard",
        },
    }


def presetdb_file(pc_sr=0, std_sr=10, pc_gain=1024):
    return {"Preset_PQ_Value": {
        "$version": "1",
        "PC": {"SDR": preset(pc_sr, pc_gain), "HDR10": preset(1)},
        "Standard": {"SDR": preset(std_sr, pc_mode="Off")},
    }}


class FakeTv(picture.TvPictureQuality):
    """Answers adb commands from in-memory TV state instead of running adb."""

    def __init__(self, files=None, dirs=(PRESETDB,), model="NS-55F501NA26", dumpsys="", logcat=""):
        super().__init__(serial="fake-serial", color=False)
        self.files = files if files is not None else {
            f"{PRESETDB}/HDMI1_initial.json": json.dumps(presetdb_file()),
            f"{PRESETDB}/HDMI2_initial.json": json.dumps(presetdb_file(pc_sr=3)),
            f"{PRESETDB}/DTV_initial.json": json.dumps(presetdb_file(pc_sr=7)),
        }
        self.dirs = list(dirs)
        self.model = model
        self.dumpsys = dumpsys
        self.logcat_text = logcat
        self.calls = []

    def check_device(self):
        return self.serial

    def adb_result(self, *args):
        self.calls.append(args)
        if args == ("shell", "getprop", "ro.vendor.sys.idme.mfr_model"):
            return 0, self.model + "\n", ""
        if args[:1] == ("shell",) and len(args) == 2 and args[1].startswith("ls -d "):
            out = "".join(d + "\n" for d in self.dirs)
            return (0 if out else 1), out, ""
        if args[:2] == ("shell", "ls"):
            names = [p.rsplit("/", 1)[1] for p in self.files if p.startswith(args[2] + "/")]
            return 0, "\n".join(names + ["InputSources.json"]) + "\n", ""
        if args[:2] == ("shell", "cat"):
            if args[2] in self.files:
                return 0, self.files[args[2]], ""
            return 1, "", f"cat: {args[2]}: No such file or directory"
        if args == ("shell", "dumpsys", "tv_input"):
            return 0, self.dumpsys, ""
        if args == ("logcat", "-d"):
            return 0, self.logcat_text, ""
        raise AssertionError(f"unexpected adb call {args}")


def run_main(pq, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = picture.main(list(argv), pq=pq)
    return rc, out.getvalue(), err.getvalue()


def session(hw, service="HdmiInputService"):
    return f"  sessionStateMap:\n    inputId: com.mediatek.tis/.{service}/{hw}\n"


class PictureToolTest(unittest.TestCase):
    def setUp(self):
        self.pq = picture.TvPictureQuality(serial="fake-serial", color=False)

    def test_port_map(self):
        self.assertEqual(picture.PORT_MAP.get("HW2"), "HDMI 1")
        self.assertEqual(picture.PORT_MAP.get("HW3"), "HDMI 2")
        self.assertEqual(picture.PORT_MAP.get("HW0"), "DTV (Antenna)")

    def test_every_hw_port_has_a_source_or_uses_the_log(self):
        for hw in picture.PORT_MAP:
            if hw != "HDMI100004":
                self.assertIn(hw, picture.HW_SOURCES)

    def test_format_table_single(self):
        params = {
            "Picture_Mode": "PC",
            "ALLM": "On",
            "SR_Strength": "0",
            "NR": "Off",
            "MPEG_NR": "Off",
        }
        table = self.pq.format_table("Test Profile", params)
        self.assertIn("=== Test Profile ===", table)
        self.assertIn("Picture Mode", table)
        self.assertIn("Auto Low Latency Mode (ALLM)", table)
        self.assertIn("PC", table)

    def test_format_table_with_preset_diff(self):
        live_params = {
            "Picture_Mode": "PC",
            "SR_Strength": "5",
            "NR": "Off",
        }
        preset_params = {
            "Picture_Mode": "PC",
            "SR_Strength": 0,
            "NR": "Off",
            "Color_Temperature": {
                "Standard": {},
                "Color_Temperature": "Standard",
            },
        }
        table = self.pq.format_table("Diff Test", live_params, preset_params)
        self.assertIn("Live / Active", table)
        self.assertIn("Preset Default", table)
        # SR_Strength differs (5 vs 0) so diff marker should appear
        self.assertIn("*", table)
        # Nested Color_Temperature extracted cleanly
        self.assertIn("Standard", table)

    def test_cli_help(self):
        cmd = [sys.executable, str(ROOT / "scripts" / "picture.py"), "--help"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("--watch", res.stdout)
        self.assertIn("--preset", res.stdout)
        self.assertIn("--diff", res.stdout)


class ModelDirTest(unittest.TestCase):
    def test_model_dir_name_keeps_brand(self):
        self.assertEqual(picture.model_dir_name("NS-55F501NA26"), "NS55F501")
        self.assertEqual(picture.model_dir_name("NS-50F502CA26\r"), "NS50F502")

    def test_model_specific_dir_chosen_over_generic(self):
        tv = FakeTv(dirs=[GENERIC, PRESETDB])
        self.assertEqual(tv.find_preset_dir(), PRESETDB)

    def test_other_model_falls_back_with_warning(self):
        tv = FakeTv(dirs=[GENERIC, PRESETDB], model="NS-50F502NA26")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(tv.find_preset_dir(), PRESETDB)
        self.assertIn("NS50F502", err.getvalue())

    def test_search_covers_three_level_layout(self):
        tv = FakeTv()
        tv.find_preset_dir()
        ls = [c for c in tv.calls if len(c) == 2 and c[1].startswith("ls -d ")][0][1]
        self.assertIn("/vendor/tvconfig/*/*/*/pq/presetdb", ls)


class PresetLookupTest(unittest.TestCase):
    def test_names_resolve_case_insensitively(self):
        tv = FakeTv()
        src, mode, hdr, settings = tv.lookup_preset("hdmi2", "pc", "sdr")
        self.assertEqual((src, mode, hdr), ("HDMI2", "PC", "SDR"))
        self.assertEqual(settings["SR_Strength"], 3)

    def test_missing_mode_is_an_error_naming_what_exists(self):
        tv = FakeTv()
        with self.assertRaises(picture.PresetLookupError) as cm:
            tv.lookup_preset("HDMI1", "Filmmaker", "SDR")
        self.assertIn("PC", str(cm.exception))
        self.assertNotIn("$version", str(cm.exception))

    def test_missing_hdr_is_an_error(self):
        with self.assertRaises(picture.PresetLookupError):
            FakeTv().lookup_preset("HDMI1", "Standard", "HDR10")

    def test_missing_source_is_an_error(self):
        with self.assertRaises(picture.PresetLookupError) as cm:
            FakeTv().lookup_preset("HDMI4", "PC", "SDR")
        self.assertIn("HDMI1", str(cm.exception))

    def test_unparseable_file_is_an_error(self):
        tv = FakeTv(files={f"{PRESETDB}/HDMI1_initial.json": "not json"})
        with self.assertRaises(picture.PresetLookupError):
            tv.lookup_preset("HDMI1", "PC", "SDR")

    def test_no_presetdb_is_an_error(self):
        with self.assertRaises(picture.PresetLookupError):
            FakeTv(dirs=[]).lookup_preset("HDMI1", "PC", "SDR")


class DiffCliTest(unittest.TestCase):
    def test_mode_diff_lowercase(self):
        tv = FakeTv(dumpsys=session("HW2"))
        rc, out, _ = run_main(tv, "--diff", "pc", "standard")
        self.assertEqual(rc, 0)
        self.assertIn("Mode PC vs Standard (HDMI1", out)
        self.assertIn("SR_Strength", out)
        self.assertNotIn("No differences", out)

    def test_source_diff_lowercase(self):
        rc, out, _ = run_main(FakeTv(), "--diff", "hdmi1", "hdmi2", "--mode", "pc")
        self.assertEqual(rc, 0)
        self.assertIn("HDMI1 vs HDMI2", out)
        self.assertIn("SR_Strength", out)

    def test_missing_source_fails_instead_of_no_differences(self):
        rc, out, err = run_main(FakeTv(), "--diff", "HDMI1", "HDMI4")
        self.assertEqual(rc, 1)
        self.assertNotIn("No differences", out)
        self.assertIn("error:", err)

    def test_missing_hdr_fails(self):
        rc, out, err = run_main(FakeTv(), "--diff", "HDMI1", "HDMI2", "--hdr", "HDR10", "--mode", "Standard")
        self.assertEqual(rc, 1)
        self.assertIn("HDR format", err)

    def test_mixed_source_and_mode_fails(self):
        rc, _, err = run_main(FakeTv(), "--diff", "HDMI1", "PC")
        self.assertEqual(rc, 1)
        self.assertIn("not one of each", err)

    def test_nested_white_balance_difference_is_shown(self):
        files = {
            f"{PRESETDB}/HDMI1_initial.json": json.dumps(presetdb_file(pc_gain=1000)),
            f"{PRESETDB}/HDMI2_initial.json": json.dumps(presetdb_file(pc_gain=1024)),
        }
        rc, out, _ = run_main(FakeTv(files=files), "--diff", "HDMI1", "HDMI2")
        self.assertEqual(rc, 0)
        self.assertIn("Color_Temperature.Standard.R_Gain", out)
        self.assertNotIn("No differences", out)

    def test_identical_presets_say_so(self):
        files = {
            f"{PRESETDB}/HDMI1_initial.json": json.dumps(presetdb_file()),
            f"{PRESETDB}/HDMI2_initial.json": json.dumps(presetdb_file()),
        }
        rc, out, _ = run_main(FakeTv(files=files), "--diff", "HDMI1", "HDMI2")
        self.assertEqual(rc, 0)
        self.assertIn("No differences found.", out)

    def test_flatten_keeps_selected_name_under_plain_key(self):
        flat = picture.flatten(preset(0))
        self.assertEqual(flat["Color_Temperature"], "Standard")
        self.assertEqual(flat["Color_Temperature.Standard.R_Gain"], 1024)


class PresetCliTest(unittest.TestCase):
    def test_preset_honours_source(self):
        tv = FakeTv(dumpsys=session("HW2"))
        rc, out, _ = run_main(tv, "--preset", "--source", "hdmi2", "--mode", "PC", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["HDMI2"]["PC"]["SDR"]["SR_Strength"], 3)

    def test_preset_defaults_to_active_input(self):
        tv = FakeTv(dumpsys=session("HW0", "AtscTunerInputService"))
        rc, out, _ = run_main(tv, "--preset", "--json")
        self.assertEqual(rc, 0)
        self.assertIn("DTV", json.loads(out))

    def test_composite_without_preset_file_is_an_error(self):
        tv = FakeTv(dumpsys=session("HW5", "CompositeInputService"))
        rc, _, err = run_main(tv, "--preset")
        self.assertEqual(rc, 1)
        self.assertIn("Composite (AV)", err)
        self.assertIn("available:", err)

    def test_cec_session_uses_logged_source(self):
        tv = FakeTv(dumpsys=session("HDMI100004"), logcat="I/x: Current Source: HDMI2\n")
        rc, out, _ = run_main(tv, "--preset", "--json")
        self.assertEqual(rc, 0)
        self.assertIn("HDMI2", json.loads(out))


LOG = """\
11-21 08:50:00.0 I MTSPQ: Current Source: HDMI1
11-21 08:50:01.0 I MTSPQ-OEMService: defaultGetCall, fetched value for SR_Strength : 9
11-21 08:50:01.0 I MTSPQ-OEMService: defaultGetCall, fetched value for Picture_Mode : Standard
11-21 08:51:11.5 I MTSPQ: Current Source: HDMI2
11-21 08:51:12.0 I MTSPQ-OEMService: defaultGetCall, fetched value for Picture_Mode : Standard
11-21 08:51:16.8 I MTSPQ-OEMService: defaultSetCall, successfully set : Picture_Mode : PC
11-21 08:51:17.0 I MTSPQ-OEMService: defaultSetCall, successfully set : ALLM : On
"""


class LiveParametersTest(unittest.TestCase):
    def test_values_from_earlier_input_are_dropped(self):
        live = FakeTv(logcat=LOG).get_live_parameters()
        self.assertEqual(live["source"], "HDMI2")
        self.assertNotIn("SR_Strength", live["params"])

    def test_set_calls_update_the_snapshot(self):
        live = FakeTv(logcat=LOG).get_live_parameters()
        self.assertEqual(live["mode"], "PC")
        self.assertEqual(live["params"]["ALLM"], "On")

    def test_default_view_compares_with_live_mode(self):
        tv = FakeTv(dumpsys=session("HW3"), logcat=LOG)
        rc, out, _ = run_main(tv, "--json")
        self.assertEqual(rc, 0)
        res = json.loads(out)
        self.assertEqual(res["preset_source"], "HDMI2")
        self.assertEqual(res["preset_baseline"]["SR_Strength"], 3)

    def test_default_view_reports_missing_preset(self):
        tv = FakeTv(dumpsys=session("HW3"), logcat="I/x: Current Source: HDMI2\n")
        rc, out, err = run_main(tv)
        self.assertEqual(rc, 0)
        self.assertIn("pass --mode", err)
        self.assertIn("Active Mode: unknown", out)


class DevicePreflightTest(unittest.TestCase):
    def fake_adb(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "adb")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nif [ \"$1\" = devices ]; then printf '%s' \"$FAKE_DEVICES\"; exit 0; fi\nexit 1\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def run_cli(self, devices_output, serial=None, *argv):
        env = dict(os.environ, ADB=self.fake_adb(), FAKE_DEVICES=devices_output)
        env.pop("ADB_SERIAL", None)
        env.pop("SERIAL", None)
        if serial:
            env["ADB_SERIAL"] = serial
        cmd = [sys.executable, str(ROOT / "scripts" / "picture.py"), *argv]
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def test_no_device(self):
        res = self.run_cli("List of devices attached\n\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("no adb device", res.stderr)

    def test_several_devices(self):
        res = self.run_cli("List of devices attached\na\tdevice\nb\tdevice\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("set ADB_SERIAL", res.stderr)

    def test_unauthorized_serial(self):
        res = self.run_cli("List of devices attached\n10.0.0.12:5555\tunauthorized\n", "10.0.0.12:5555")
        self.assertEqual(res.returncode, 1)
        self.assertIn("unauthorized", res.stderr)

    def test_reachable_device_but_failing_command_is_an_error(self):
        res = self.run_cli("List of devices attached\nx\tdevice\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("error:", res.stderr)


if __name__ == "__main__":
    unittest.main()
