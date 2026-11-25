# MediaTek TV Picture Quality Inspector (`mediatek-tv-pq`)

A lightweight command-line tool and hardware reverse-engineering reference to inspect, compare, and live-monitor Picture Quality (PQ) parameters and factory presets on MediaTek-powered Smart TVs (Fire TV Omni/4-Series, Sony Bravia, TCL, Philips, Panasonic) over ADB with no root required.

---

## Why This Exists

Modern smart televisions apply complex, multi-stage image processing pipelines to incoming video signals:
- **AI Super Resolution (AISR):** High-frequency neural edge enhancement that can introduce severe ringing, edge halos, and false textures on clean source feeds.
- **Polyphase Spatial Peaking (`SR_Strength`):** Edge oversharpening that accentuates compression macroblocks in 1080p and 720p content.
- **Dynamic Contrast & Backlight Modulation (`DLC` / `Local Dimming`):** Algorithmic contrast adjustments that can clip highlights or crush shadow detail.
- **Temporal Motion Estimation & Smoothing (`MEMC`):** Frame interpolation ("soap opera effect") that often remains subtly active across certain picture modes even when switched off in standard menus.

Consumer TV settings menus abstract away these hardware parameters behind vague labels or hide them completely. `mediatek-tv-pq` reads directly from the TV's runtime state and vendor calibration database (`/vendor/tvconfig/.../presetdb/`), giving calibrators, PC gamers, and AV enthusiasts exact visibility into:
1. **Live Active Register Values:** What signal processors are currently applying to the active HDMI or app feed.
2. **Factory Baseline Presets:** The exact reference values configured by the panel manufacturer for each picture mode (PC, Game, Film, Standard, Dynamic).
3. **Real-time Tweaks (`--watch`):** Immediate terminal streaming showing exactly which underlying parameters move when a remote button is pressed.

---

## Features

- **Live Active Input Inspection:** Detects the currently tuned input via `dumpsys tv_input` and displays hardware parameters alongside factory baseline defaults.
- **Input & Mode Diffing (`--diff`):** Compare settings between inputs (e.g. `HDMI1` vs `HDMI2`) or between picture modes (e.g. `PC` vs `Standard`) to confirm true 4:4:4 chroma bypass and latency modes.
- **Real-Time Setting Monitor (`--watch`):** Streams parameter modifications live as you adjust options with the physical remote or on-screen menu.
- **Preset Database Extraction (`--preset`):** Reads manufacturer calibration databases directly from vendor partitions across SDR, HDR10, and Dolby Vision profiles.
- **Machine-Readable Export (`--json`):** Clean JSON output for automated display calibration and integration testing.

---

## Hardware Architecture Teardown

Detailed hardware pipeline reference and register mapping:
- [docs/mediatek-mt5896.md](docs/mediatek-mt5896.md): Complete 14-stage hardware rendering pipeline teardown (IPDMA -> AISR -> VIP2 -> CFD -> TCON), based on reverse engineering of unstripped 32-bit RISC-V PQU firmware (`pqu_mt5896_2048.elf`).
- [docs/TUNING_GUIDE.md](docs/TUNING_GUIDE.md): Recommended profiles for low-latency PC gaming (clean 4:4:4 integer bypass) and artifact-free ambient photo displays.

---

## Installation & Requirements

### Requirements
- Python 3.8+
- Android Debug Bridge (`adb`) on your PATH (or via `ADB_SERIAL=<ip>:5555`)
- A MediaTek-based Smart TV connected over local Wi-Fi or Ethernet with ADB debugging enabled.

### Quick Start

Clone the repository:
```bash
git clone https://github.com/VladislavEkimtcov/mediatek-tv-pq.git
cd mediatek-tv-pq
```

Connect to your TV:
```bash
adb connect <tv-ip>:5555
```

Inspect active input and live parameters:
```bash
./picture.sh
```

---

## Usage Examples

### 1. View Live Settings on the Active Input
```bash
./picture.sh
```
Displays categorized settings (Mode & Gaming, Clarity & Sharpness, Noise Reduction, Motion & Cadence, Color & Balance, Backlight & Contrast) showing current live values alongside factory baseline presets.

### 2. Stream Parameter Changes in Real Time
```bash
./picture.sh --watch
```
Keep this running in a terminal while adjusting picture settings with the TV remote to observe exactly which internal registers change.

### 3. Compare Picture Modes
Verify whether PC Mode disables unwanted spatial peaking and AI processing compared to Standard Mode:
```bash
./picture.sh --diff PC Standard
```

### 4. Compare Two Inputs
Check configuration differences between your gaming console on HDMI 2 and a streaming device on HDMI 1:
```bash
./picture.sh --diff HDMI1 HDMI2
```

### 5. Inspect Factory Presets
Dump the baseline vendor configuration for a specific input and picture mode:
```bash
./picture.sh --preset HDMI2 --mode PC
```

### 6. JSON Export
```bash
./picture.sh --json
```

---

## Supported Platforms

- **Amazon Fire TV:** Omni Series, 4-Series, Fire TV 2-Series, Fire TV Stick 4K Max (MediaTek MT5895 / MT5896 / MT8696 platforms).
- **Sony Bravia:** Cognitive Processor XR / MediaTek Pentonic series (MT5895, MT5896).
- **TCL / Philips / Hisense:** Select Android TV and Google TV models utilizing MediaTek video processing SoCs with vendor `tvconfig` structures.

---

## Relationship to dumbtv & Credits

This project originated as diagnostic tooling and documentation contributed in PR [#229](https://github.com/humblemuzzu/dumbtv/pull/229) to [dumbtv](https://github.com/humblemuzzu/dumbtv) by [humblemuzzu](https://github.com/humblemuzzu). Because `dumbtv` focuses strictly on debloating and lean TV launchers, this tool is maintained as an independent utility for the broader display calibration and home theater communities. It runs completely standalone and does not require any launcher replacement or debloating scripts.
