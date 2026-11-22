#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 PERFORMANCE MONITOR - PC agent  (v2)
=============================================================================
 Collects REAL-TIME data from this PC and streams it over USB serial to an
 ESP32 + 2.8" TFT dashboard:

   CPU   : usage %, package temperature, Vcore voltage, fan RPM
   GPU   : usage %, game FPS, temperature, VRAM usage %, fan RPM / %
   MEM   : usage %, memory frequency, memory voltage
   NET   : download / upload speed (KB/s or MB/s)
   CLOCK : 12-hour time with AM/PM (from the PC, so it is always correct)

 Data sources (each metric automatically falls back through this chain):
   * psutil                        - CPU load, RAM load, network throughput
   * NVIDIA NVML (nvidia-ml-py)    - GPU temp/load/VRAM/fan  (no other tool
                                     needed for NVIDIA cards!)
   * LibreHardwareMonitor (WMI)    - CPU temp, Vcore, fan RPMs, GPU (any
                                     brand), live DRAM voltage  (FREE,
                                     portable .exe - run it and leave it open)
   * WMI MSAcpi / Win32_PhysicalMemory - CPU temp fallback, RAM freq/volt
   * Intel PresentMon (FREE)       - in-game FPS (ETW frame capture)

 Protocol: one JSON object per line @ 115200 baud
   {"cpu":2,"ct":40,"cv":0.72,"cf":1880,"gpu":14,"fps":60,"gt":47,"vram":16,
    "gf":1043,"gfu":0,"mem":40,"mf":2396,"mv":1.22,"down":0,"up":0,
    "h":10,"m":51,"ap":1}
   -1 on any sensor value = "not available" (display shows "--").

 Usage (run as Administrator - needed for PresentMon ETW capture):
   python monitor_agent.py                      # auto-detect COM port
   python monitor_agent.py --port COM5          # explicit port
   python monitor_agent.py --dry-run            # print JSON, no serial
   python monitor_agent.py --debug-sensors      # list every sensor LHM exposes
   python monitor_agent.py --presentmon off     # disable FPS capture
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque

# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------
try:
    import psutil
except ImportError:
    sys.exit("[FATAL] psutil is missing ->  pip install psutil")

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("[FATAL] pyserial is missing ->  pip install pyserial")

IS_WINDOWS = sys.platform.startswith("win")

wmi_mod = None
if IS_WINDOWS:
    try:
        import wmi as wmi_mod          # needs: pip install wmi  (pulls pywin32)
    except ImportError:
        print("[WARN] python package 'wmi' not installed ->  pip install wmi pywin32")
        print("       Without it: no CPU Vcore/fan/RAM info from LibreHardwareMonitor.")

try:
    import pynvml                     # needs: pip install nvidia-ml-py
except ImportError:
    pynvml = None

UNKNOWN = -1.0


def log(msg: str) -> None:
    print(msg, flush=True)


def is_admin() -> bool:
    try:
        if IS_WINDOWS:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass
    return True


# ===========================================================================
#  NVIDIA GPU via NVML (works with NO extra tools on NVIDIA systems)
# ===========================================================================
class NvidiaGPU:
    def __init__(self) -> None:
        self.ok = False
        self.handle = None
        if pynvml is None:
            return
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(self.handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            self.ok = True
            log(f"[ OK ] NVIDIA NVML connected: {name} (GPU sensors work "
                f"even without LibreHardwareMonitor)")
        except Exception:
            pass

    def temp(self) -> float:
        if not self.ok:
            return UNKNOWN
        try:
            return float(pynvml.nvmlDeviceGetTemperature(
                self.handle, pynvml.NVML_TEMPERATURE_GPU))
        except Exception:
            return UNKNOWN

    def util(self) -> float:
        if not self.ok:
            return UNKNOWN
        try:
            return float(pynvml.nvmlDeviceGetUtilizationRates(self.handle).gpu)
        except Exception:
            return UNKNOWN

    def mem_pct(self) -> float:
        if not self.ok:
            return UNKNOWN
        try:
            mi = pynvml.nvmlDeviceGetMemoryInfo(self.handle)
            if mi.total <= 0:
                return UNKNOWN
            return 100.0 * float(mi.used) / float(mi.total)
        except Exception:
            return UNKNOWN

    def fan_pct(self) -> float:
        """NVML reports fan speed in PERCENT, not RPM."""
        if not self.ok:
            return UNKNOWN
        try:
            return float(pynvml.nvmlDeviceGetFanSpeed(self.handle))
        except Exception:
            return UNKNOWN


# ===========================================================================
#  Hardware sensors via LibreHardwareMonitor's WMI namespace
# ===========================================================================
class HardwareReader:
    """
    Reads temperature / voltage / fan / load sensors published by
    LibreHardwareMonitor (root\\LibreHardwareMonitor).

    Start LibreHardwareMonitor.exe (as Administrator) BEFORE or during this
    agent - while it runs, it publishes all its sensors to WMI.  The agent
    keeps retrying in the background if it is not up yet.
    """

    WEB_URL = "http://127.0.0.1:8085/data.json"   # LHM: Options -> Remote Web Server

    def __init__(self, debug: bool = False) -> None:
        self.conn = None              # WMI connection
        self.channel = None           # "wmi" or "web" - whichever is live
        self.debug = debug
        self.sensors: list[tuple[str, str, str, float]] = []
        self.last_poll = 0.0
        self._last_try = -999.0
        self._wmi_err = ""
        self._web_err = ""
        self._web_tree = {}
        if not self._try_connect():
            log("[WARN] LibreHardwareMonitor data NOT available (yet) - CPU "
                "temp/Vcore/fan will show '--' until it connects.")
            log("       1) Run LibreHardwareMonitor.exe AS ADMINISTRATOR and "
                "leave it open (its own window must show temperatures).")
            log("       2) Run THIS agent from an ADMINISTRATOR terminal too "
                "(the LHM WMI namespace only allows admins by default).")
            log("       3) Backup channel: in LHM use Options -> Remote Web "
                "Server -> Run (port 8085). The agent picks it up automatically.")
            if self._wmi_err:
                log(f"       WMI error: {self._wmi_err}")
            if self._web_err:
                log(f"       Web error: {self._web_err}")
            log("       -> (The agent keeps retrying every few seconds.)")

    def _try_connect(self) -> bool:
        now = time.monotonic()
        if self.channel is not None:
            return True
        if now - self._last_try < 5.0:
            return False
        self._last_try = now
        # ---- channel 1: WMI (fast, needs admin rights) ----
        if wmi_mod is not None:
            for ns in (r"root\LibreHardwareMonitor", r"root\OpenHardwareMonitor"):
                try:
                    c = wmi_mod.WMI(namespace=ns)
                    probe = list(c.Sensor())
                    self.conn = c
                    self.channel = "wmi"
                    log(f"[ OK ] LibreHardwareMonitor WMI connected ({ns}), "
                        f"{len(probe)} sensors visible")
                    if len(probe) == 0:
                        log("[WARN] LHM exposes 0 sensors -> re-run LHM AS "
                            "ADMINISTRATOR (and accept its PawnIO driver "
                            "prompt if it shows one).")
                    return True
                except Exception as e:
                    self._wmi_err = str(e).strip()
        else:
            self._wmi_err = "python package 'wmi' not installed"
        # ---- channel 2: LHM built-in web server (no admin needed) ----
        if self._try_web():
            self.channel = "web"
            self.sensors = self._walk_web()
            kinds = defaultdict(int)
            for _, _, t, _ in self.sensors:
                kinds[t] += 1
            summary = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
            log(f"[ OK ] LibreHardwareMonitor web server connected "
                f"({self.WEB_URL}) -> parsed {len(self.sensors)} sensors"
                + (f" ({summary})" if summary else "  (NOTHING parsed!)"))
            return True
        return False

    def _try_web(self) -> bool:
        try:
            req = urllib.request.Request(self.WEB_URL,
                                         headers={"User-Agent": "perf-monitor"})
            with urllib.request.urlopen(req, timeout=2.0) as r:
                self._web_tree = json.loads(r.read().decode("utf-8", "replace"))
            self._web_err = ""
            return True
        except Exception as e:
            self._web_err = str(e).strip()
            return False

    @staticmethod
    def _web_num(v) -> float | None:
        """Parse a data.json Value - plain number or unit string like
        "33,0 °C" / "1.24 V" / "1,245 RPM" / "4800 MHz". Handles decimal
        commas and thousands separators (the unit tells them apart)."""
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v)
        m = re.search(r"[-+]?\d[\d\s.,]*", s)
        if not m:
            return None
        num = m.group().strip().replace(" ", "")
        tail = s[m.end():].lower()
        if "," in num and "." in num:
            num = num.replace(".", "").replace(",", ".")     # 1.234,5 -> 1234.5
        elif (re.fullmatch(r"\d{1,3}[.,]\d{3}", num)
              and re.search(r"rpm|hz|w\b|gb|mb|kb", tail)):
            num = num.replace(",", "").replace(".", "")       # 1,245 RPM -> 1245
        else:
            num = num.replace(",", ".")                       # 33,0 -> 33.0
        try:
            return float(num)
        except ValueError:
            return None

    # data.json has two shapes across LHM/OHM versions: new sensor nodes carry
    # "Type"/"SensorId"; old-style nodes sit in groups ("Temperatures",
    # "Voltages", "Fans", ... / images_icon/temperature.png, ...) and carry
    # only Text/Min/Value/Max. Both are handled below.
    _WEB_IMG_GROUP = (("temperature", "Temperature"), ("voltage", "Voltage"),
                      ("fan", "Fan"), ("load", "Load"), ("clock", "Clock"),
                      ("power", "Power"), ("data", "Data"), ("flow", "Flow"),
                      ("level", "Level"))
    _WEB_IMG_PARENT = (("cpu", "cpu"), ("chip", "lpc superio"),
                       ("mainboard", "motherboard"), ("ram", "memory"),
                       ("gpu", "gpu"), ("ati", "gpu"), ("nvidia", "gpu"),
                       ("hdd", "hdd"), ("ssd", "ssd"), ("nic", "nic"))
    _WEB_NAME_PARENT = (("nvidia", "gpu"), ("geforce", "gpu"), ("radeon", "gpu"),
                        ("intel core", "cpu"), ("core i", "cpu"), ("ryzen", "cpu"),
                        ("phenom", "cpu"), ("athlon", "cpu"), ("epyc", "cpu"),
                        ("threadripper", "cpu"), ("cpu", "cpu"),
                        ("processor", "cpu"))
    _WEB_TYPE_MAP = {t.lower(): t for t in
                     ("Temperature", "Voltage", "Fan", "Load", "Data", "Clock",
                      "Power", "Flow", "Level", "Control", "Factor")}

    def _walk_web(self) -> list[tuple[str, str, str, float]]:
        """Convert LHM's data.json tree into the same (name, parent,
        sensortype, value) tuples the WMI channel produces, so every sensor
        lookup below works unchanged."""
        out: list[tuple[str, str, str, float]] = []

        def clean(s: str) -> str:
            return re.split(r"\s{2,}", re.split(r"[:\uFF1A]", s)[0])[0].strip()

        def unit_type(raw: str) -> str:
            s = str(raw)
            if re.search(r"rpm|rps", s, re.I):
                return "Fan"
            if re.search(r"\b(w|mw|kw)\b", s, re.I):
                return "Power"
            if re.search(r"\b(hz|khz|mhz|ghz)\b", s, re.I):
                return "Clock"
            if "%" in s:
                return "Load"
            if re.search(r"°|celsius|\bk\b", s, re.I):
                return "Temperature"
            if re.search(r"\bv\b", s, re.I):
                return "Voltage"
            return ""

        def walk(node, parent: str, gtype: str) -> None:
            if not isinstance(node, dict):
                return
            nname = clean(str(node.get("Name") or node.get("Text") or ""))
            img = str(node.get("ImageURL", "")).lower()

            # ---- what type of sensor/group is this node? ----
            ntype = ""
            for key in ("Type", "SensorType"):
                t = str(node.get(key, "")).strip().lower()
                if t in self._WEB_TYPE_MAP:
                    ntype = self._WEB_TYPE_MAP[t]
                    break
            if not ntype:
                sid = str(node.get("SensorId", "")).lower()
                for key, t in self._WEB_TYPE_MAP.items():
                    if "/" + key.replace(" ", "") + "/" in sid:
                        ntype = t
                        break
            if not ntype:
                for frag, t in self._WEB_IMG_GROUP:
                    if frag + ".png" in img:
                        ntype = t
                        break
            if not ntype and nname.lower().rstrip("s") in self._WEB_TYPE_MAP:
                ntype = self._WEB_TYPE_MAP[nname.lower().rstrip("s")]
            if not ntype:
                ntype = gtype

            # ---- parent path fragment for children (cpu / lpc superio / gpu) --
            frag = ""
            for key, pat in self._WEB_IMG_PARENT:
                if key + ".png" in img:
                    frag = pat
                    break
            if not frag:
                htype = str(node.get("Type", "")).strip().lower()
                frag = {"superio": "lpc superio", "cpu": "cpu", "gpu": "gpu",
                        "motherboard": "motherboard", "memory": "memory",
                        "storage": "hdd", "nic": "nic"}.get(htype, "")
            if not frag:
                low = nname.lower()
                for key, pat in self._WEB_NAME_PARENT:
                    if key in low:
                        frag = pat
                        break
            if not frag:
                frag = nname.lower()
            child_parent = (parent + "/" + frag).strip("/")

            # ---- a node with a numeric Value is a sensor ----
            raw = node.get("Value")
            if raw not in (None, ""):
                val = self._web_num(raw)
                if val is not None:
                    stype = ntype or unit_type(raw)
                    if stype:
                        out.append((nname, parent, stype, val))

            for kid in (node.get("Children") or []):
                walk(kid, child_parent, ntype or gtype)

        walk(self._web_tree, "", "")
        return out

    def _refresh(self, max_age: float = 0.35, force: bool = False) -> None:
        now = time.monotonic()
        if self.channel is None:
            self._try_connect()
            if self.channel is None:
                return
        if not force and (now - self.last_poll) < max_age:
            return
        self.last_poll = now

        if self.channel == "wmi":
            out = []
            try:
                for s in self.conn.Sensor():
                    try:
                        v = float(s.Value)
                    except Exception:
                        continue
                    out.append((str(s.Name), str(s.Parent), str(s.SensorType), v))
            except Exception:
                self.channel = None        # lost it -> reconnect logic
                return
            self.sensors = out
        else:  # web
            if self._try_web():
                self.sensors = self._walk_web()
            else:
                self.channel = None        # server gone -> reconnect logic
                return

        if self.debug:
            log("---- all LibreHardwareMonitor sensors ----")
            for name, parent, s_type, v in sorted(self.sensors):
                log(f"  {s_type:<12} {parent:<32} {name:<32} {v}")
            if not self.sensors and self.channel == "web":
                log("  !! parser found 0 sensors - raw data.json nodes:")
                def raw(node, d=0):
                    if not isinstance(node, dict) or d > 3:
                        return
                    log(f"  {'  ' * d}Text={node.get('Text')!r} "
                        f"Type={node.get('Type')!r} "
                        f"Value={node.get('Value')!r} "
                        f"ImageURL={node.get('ImageURL')!r}")
                    for kid in (node.get("Children") or [])[:6]:
                        raw(kid, d + 1)
                raw(self._web_tree)
            log("------------------------------------------")

    @staticmethod
    def _kind(parent: str, *hints: str) -> bool:
        p = parent.lower()
        return any(h in p for h in hints)

    def _find(self, stype: str, parent_hints: tuple[str, ...],
              name_test, prefer_parents: tuple[str, ...] = ()) -> float | None:
        best = None
        for name, parent, s_type, val in self.sensors:
            if s_type != stype:
                continue
            if parent_hints and not self._kind(parent, *parent_hints):
                continue
            if not name_test(name.lower(), parent.lower()):
                continue
            if prefer_parents and self._kind(parent, *prefer_parents):
                return val
            if best is None:
                best = val
        return best

    # ------------------------- CPU --------------------------------------
    def cpu_temp(self) -> float:
        self._refresh()
        # preferred: CPU package / Tctl / Tdie style sensors
        v = self._find(
            "Temperature", ("cpu",),
            lambda n, p: ("package" in n or "tctl" in n or "tdie" in n
                          or n == "cpu" or "cpu overall" in n))
        if v is not None:
            return v
        # fallback: hottest individual core
        cores = [val for (n, p, t, val) in self.sensors
                 if t == "Temperature" and self._kind(p, "cpu")
                 and n.strip().lower().startswith("core")]
        if cores:
            return max(cores)
        # last resort: any temperature sensor with 'cpu' in its name
        v = self._find("Temperature", (), lambda n, p: "cpu" in n)
        return v if v is not None else self._acpi_temp()

    @staticmethod
    def _acpi_temp() -> float:
        """WMI thermal-zone fallback (works on some laptops)."""
        if wmi_mod is None:
            return UNKNOWN
        try:
            c = wmi_mod.WMI(namespace=r"root\wmi")
            for t in c.MSAcpi_ThermalZoneTemperature():
                v = float(t.CurrentTemperature) / 10.0 - 273.15
                if -20.0 < v < 150.0:
                    return v
        except Exception:
            pass
        return UNKNOWN

    def cpu_vcore(self) -> float:
        self._refresh()
        v = self._find("Voltage", ("cpu", "lpc", "motherboard", "superio"),
                       lambda n, p: ("vcore" in n or "core (svi2" in n
                                     or "cpu vdd" in n or "cpu vddio" in n
                                     or n in ("cpu", "vdd")
                                     or n.startswith("vin")),
                       prefer_parents=("lpc", "motherboard", "superio"))
        if v is not None:
            return v
        # last resort: any Vcore-range rail (0.2-2.2 V) that isn't a fixed rail
        skip = ("3v", "5v", "12v", "avcc", "vbat", "3vsb", "vtt", "3.3",
                "5.0", "12", "pchg", "dimm", "dram", "vddnb")
        for n, p, t, val in self.sensors:
            if (t == "Voltage" and 0.2 <= val <= 2.2
                    and not any(s in n.lower() for s in skip)):
                return val
        return UNKNOWN

    def cpu_fan(self) -> float:
        self._refresh()
        v = self._find("Fan", ("lpc", "motherboard", "superio"),
                       lambda n, p: ("cpu" in n or "processor" in n
                                     or "cooler" in n))
        if v is None:
            v = self._find("Fan", ("lpc", "motherboard", "superio"),
                           lambda n, p: True)
        if v is None:
            v = self._find("Fan", (), lambda n, p: "cpu" in n)
        if v is None:
            v = self._find("Fan", (),
                           lambda n, p: "gpu" not in p and "graphics" not in p)
        return v if v is not None else UNKNOWN

    # ------------------------- GPU --------------------------------------
    def gpu_temp(self) -> float:
        self._refresh()
        v = self._find("Temperature", ("gpu",),
                       lambda n, p: "gpu" in n or "core" in n or "hot" in n)
        return v if v is not None else UNKNOWN

    def gpu_load(self) -> float:
        self._refresh()
        v = self._find("Load", ("gpu",),
                       lambda n, p: n in ("gpu core", "gpu", "core",
                                          "graphics"))
        return v if v is not None else UNKNOWN

    def vram_load(self) -> float:
        self._refresh()
        v = self._find("Load", ("gpu",), lambda n, p: "memory" in n or "vram" in n)
        if v is not None:
            return v
        used = self._find("Data", ("gpu",),
                          lambda n, p: "dedicated" in n and "used" in n)
        total = self._find("Data", ("gpu",),
                           lambda n, p: "dedicated" in n and "total" in n)
        if used is not None and total:
            return max(0.0, min(100.0, 100.0 * used / total))
        return UNKNOWN

    def gpu_fan(self) -> float:
        """Fan RPM (most GPUs expose tach via LHM)."""
        self._refresh()
        v = self._find("Fan", ("gpu",), lambda n, p: True)
        return v if v is not None else UNKNOWN

    # ------------------------- misc -------------------------------------
    def dram_voltage(self) -> float:
        self._refresh()
        v = self._find("Voltage", (),
                       lambda n, p: ("dram" in n or "vddr" in n or "vdimm" in n
                                     or "memory voltage" in n))
        return v if v is not None else UNKNOWN


# ===========================================================================
#  RAM frequency / voltage  (Win32_PhysicalMemory)
# ===========================================================================
class MemoryInfo:
    def __init__(self) -> None:
        self.freq_mhz = UNKNOWN
        self.volt = UNKNOWN
        self._load()

    def _load(self) -> None:
        if wmi_mod is None:
            return
        try:
            c = wmi_mod.WMI()
            freqs, volts = [], []
            for m in c.Win32_PhysicalMemory():
                for attr in ("ConfiguredClockSpeed", "Speed"):
                    v = getattr(m, attr, None)
                    if v:
                        try:
                            freqs.append(float(v))
                        except Exception:
                            pass
                cv = getattr(m, "ConfiguredVoltage", None)   # millivolts
                if cv:
                    try:
                        volts.append(float(cv) / 1000.0)
                    except Exception:
                        pass
            if freqs:
                self.freq_mhz = max(freqs)
            if volts:
                self.volt = max(volts)
        except Exception:
            pass


# ===========================================================================
#  Network throughput (smoothed + glitch-guarded)
# ===========================================================================
class NetworkMeter:
    MAX_PLAUSIBLE = 2.0e6      # KB/s  (~16 Gbit/s) - above this it's a glitch

    def __init__(self) -> None:
        self.prev = self._counters()
        self.prev_t = time.monotonic()
        self.sm_down = 0.0
        self.sm_up = 0.0

    @staticmethod
    def _counters() -> tuple[int, int]:
        try:
            per = psutil.net_io_counters(pernic=True)
        except Exception:
            c = psutil.net_io_counters()
            return c.bytes_recv, c.bytes_sent
        rx = tx = 0
        for name, c in per.items():
            low = name.lower()
            if "loopback" in low or low.startswith("lo"):
                continue
            rx += c.bytes_recv
            tx += c.bytes_sent
        return rx, tx

    def rates(self) -> tuple[float, float]:
        """returns smoothed (download KB/s, upload KB/s)"""
        now = time.monotonic()
        rx, tx = self._counters()
        dt = max(0.05, now - self.prev_t)          # never divide by ~0
        down = (rx - self.prev[0]) / dt / 1024.0
        up = (tx - self.prev[1]) / dt / 1024.0
        self.prev = (rx, tx)
        self.prev_t = now

        # counter resets / virtual-NIC glitches produce impossible spikes
        if down < 0 or down > self.MAX_PLAUSIBLE:
            down = self.sm_down
        if up < 0 or up > self.MAX_PLAUSIBLE:
            up = self.sm_up

        a = 0.45                                   # EMA smoothing
        self.sm_down += a * (down - self.sm_down)
        self.sm_up += a * (up - self.sm_up)
        return self.sm_down, self.sm_up


# ===========================================================================
#  Game FPS  via Intel PresentMon
# ===========================================================================
class FPSMonitor:
    """
    Runs PresentMon in the background and counts present events per
    application per second  ->  frames per second of the most active app
    (your game). Works for ANY graphics API (DX9/11/12, Vulkan) and any GPU.
    """

    IGNORE_APPS = {
        "dwm.exe", "explorer.exe", "python.exe", "pythonw.exe",
        "monitor_agent.py", "obs64.exe", "obs32.exe",
    }

    def __init__(self, exe: str | None) -> None:
        self.ok = False
        self.proc: subprocess.Popen | None = None
        self.frames: dict[tuple[str, str], deque] = defaultdict(deque)
        self.lock = threading.Lock()
        self.exe = exe
        if not exe:
            log("[WARN] PresentMon not found -> in-game FPS will show 0.")
            log("       Download the FREE PresentMon (Intel) from")
            log("       https://github.com/GameTechDev/PresentMon/releases")
            log("       Put the downloaded exe (e.g. PresentMon-2.6.0-x64.exe)")
            log("       next to this script - ANY 'PresentMon*.exe' name works,")
            log("       no need to rename. Or pass --presentmon <path>.")
            return
        if self._spawn(exe):
            self.ok = True
            log(f"[ OK ] PresentMon running: {exe}  (FPS capture active)")

    @staticmethod
    def _candidate_cmds(exe: str) -> list[list[str]]:
        # PresentMon 2.x (double dash) first, then legacy 1.x (single dash)
        return [
            # 2.x: CSV on stdout, no console animation, NO junk CSV files on
            # disk, and take over the ETW session if another PresentMon
            # window is left open (all instances share one session name).
            [exe, "--output_stdout", "--no_csv", "--no_console_stats",
             "--stop_existing_session"],
            # older 2.x builds without --no_csv / --stop_existing_session
            [exe, "--output_stdout", "--no_console_stats"],
            # legacy 1.x
            [exe, "-output_stdout", "-captureall", "-no_top"],
            [exe, "-output_stdout", "-captureall"],
        ]

    def _spawn(self, exe: str) -> bool:
        for cmd in self._candidate_cmds(exe):
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
            except Exception:
                continue
            time.sleep(3.0)                    # must stay alive -> valid flags
            if proc.poll() is None:
                self.proc = proc
                threading.Thread(target=self._reader, daemon=True).start()
                return True
        log("[WARN] PresentMon exited immediately - is the terminal running AS "
            "ADMINISTRATOR? (ETW capture needs elevation). Also close any "
            "other PresentMon window - they share one capture session.")
        return False

    def _reader(self) -> None:
        swap_idx = -1
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith("application"):
                    header = [h.strip().lower() for h in line.split(",")]
                    swap_idx = (header.index("swapchainaddress")
                                if "swapchainaddress" in header else -1)
                    continue
                parts = line.split(",")
                if len(parts) < 5:
                    continue
                app = parts[0].strip().strip('"')
                if not app.lower().endswith(".exe"):
                    continue                       # console stats / junk line
                key = (app.lower(),
                       parts[swap_idx].strip() if 0 <= swap_idx < len(parts) else "")
                with self.lock:
                    self.frames[key].append(time.monotonic())
        except Exception:
            pass

    def get_fps(self) -> int:
        if not self.ok:
            return 0
        now = time.monotonic()
        best = 0
        with self.lock:
            for (app, _), dq in self.frames.items():
                while dq and now - dq[0] > 1.2:
                    dq.popleft()
                if app in self.IGNORE_APPS:
                    continue
                n = sum(1 for t in dq if now - t <= 1.0)   # presents in last 1 s
                if n > best:
                    best = n
        return int(best)


# ===========================================================================
#  helpers
# ===========================================================================
def find_presentmon(explicit: str | None) -> str | None:
    if explicit and explicit.lower() == "off":
        return None
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    names = ["PresentMon64.exe", "PresentMon.exe", "PresentMon64", "PresentMon"]
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, os.getcwd()):
        for n in names:
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                return p
        # any release-style name, e.g. PresentMon-2.6.0-x64.exe
        # (but never PresentMon-*.csv trace files!)
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            entries = []
        for n in entries:
            low = n.lower()
            if low.startswith("presentmon") and \
                    (low.endswith(".exe") or "." not in low):
                p = os.path.join(folder, n)
                if os.path.isfile(p):
                    return p
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def autodetect_port() -> str | None:
    ports = list(list_ports.comports())
    if not ports:
        return None
    preferred = ("cp210", "ch340", "ch910", "uart", "usb serial", "silicon labs",
                 "ftdi", "usb-serial")
    for p in ports:
        d = (p.description or "").lower() + " " + (p.manufacturer or "").lower()
        if any(k in d for k in preferred):
            return p.device
    return ports[0].device if len(ports) == 1 else None


def fmt(v: float, unit: str = "") -> str:
    return f"{v:.1f}{unit}" if v >= 0 else "--"


# ===========================================================================
#  main
# ===========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="PERFORMANCE MONITOR PC agent")
    ap.add_argument("--port", help="serial port, e.g. COM5 or /dev/ttyUSB0 "
                                   "(default: auto-detect)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--interval", type=float, default=0.5,
                    help="seconds between updates (default 0.5 = 2 Hz)")
    ap.add_argument("--presentmon", default=None,
                    help="path to the PresentMon exe (any PresentMon*.exe), "
                         "or 'off'")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the JSON lines instead of sending them")
    ap.add_argument("--debug-sensors", action="store_true",
                    help="list every sensor LibreHardwareMonitor exposes")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    log("=" * 68)
    log("  PERFORMANCE MONITOR  -  PC agent")
    log("=" * 68)
    if is_admin():
        log("[INFO] Running as Administrator: YES")
    else:
        log("[INFO] Running as Administrator: NO")
        log("       If LibreHardwareMonitor is elevated, run THIS terminal as")
        log("       Administrator too - the LHM WMI namespace is admin-only.")

    hw = HardwareReader(debug=args.debug_sensors)
    nv = NvidiaGPU()
    meminfo = MemoryInfo()
    net = NetworkMeter()
    fpsmon = FPSMonitor(find_presentmon(args.presentmon))

    if meminfo.freq_mhz >= 0:
        log(f"[ OK ] RAM: {meminfo.freq_mhz:.0f} MHz"
            + (f", {meminfo.volt:.2f} V (configured)"
               if meminfo.volt >= 0 else ""))

    # warm up psutil CPU meter
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=0.25)

    # ---------------- sensor health report ------------------------------
    def sample() -> dict:
        dram_v = hw.dram_voltage()
        if dram_v < 0 and meminfo.volt >= 0:
            dram_v = meminfo.volt

        ct = hw.cpu_temp()
        gt = hw.gpu_temp()
        if gt < 0:
            gt = nv.temp()
        gl = hw.gpu_load()
        if gl < 0:
            gl = nv.util()
        vr = hw.vram_load()
        if vr < 0:
            vr = nv.mem_pct()
        gf = hw.gpu_fan()
        gfu = 0
        if gf < 0:
            gf = nv.fan_pct()
            gfu = 1 if gf >= 0 else 0   # NVML reports percent, not RPM

        vm = psutil.virtual_memory()
        down, up = net.rates()
        now = time.localtime()
        h24, minute = now.tm_hour, now.tm_min
        return {
            "cpu":  round(psutil.cpu_percent(interval=None), 1),
            "ct":   round(ct, 1),
            "cv":   round(hw.cpu_vcore(), 2),
            "cf":   int(hw.cpu_fan()),
            "gpu":  round(gl, 1),
            "fps":  fpsmon.get_fps(),
            "gt":   round(gt, 1),
            "vram": round(vr, 1),
            "gf":   int(gf),
            "gfu":  gfu,
            "mem":  round(vm.percent, 1),
            "mf":   int(meminfo.freq_mhz),
            "mv":   round(dram_v, 2),
            "down": round(down, 1),
            "up":   round(up, 1),
            "h":    (h24 % 12) or 12,
            "m":    minute,
            "ap":   1 if h24 >= 12 else 0,
        }

    probe = sample()
    log("-" * 68)
    if hw.channel:
        log(f"[INFO] Sensor channel: LibreHardwareMonitor via "
            f"{'WMI' if hw.channel == 'wmi' else 'web server'}")
    log("[INFO] sensor check  ('--' = not found, fix listed below):")
    log(f"       CPU  temp {fmt(probe['ct'], ' C'):<8} vcore "
        f"{fmt(probe['cv'], ' V'):<8} fan {fmt(probe['cf'], ' RPM')}")
    log(f"       GPU  temp {fmt(probe['gt'], ' C'):<8} load "
        f"{fmt(probe['gpu'], ' %'):<8} vram {fmt(probe['vram'], ' %'):<8} "
        f"fan {fmt(probe['gf'])}")
    log(f"       MEM  freq {fmt(probe['mf'], ' MHz'):<8} volt "
        f"{fmt(probe['mv'], ' V')}")
    if probe["ct"] < 0 or probe["cv"] < 0 or probe["cf"] < 0:
        log("       -> CPU sensor gaps: run LHM AS ADMIN, and run this agent "
            "from an ADMIN terminal (or enable LHM's Options -> Remote Web "
            "Server -> Run).")
        if hw.sensors:
            def names(stype):
                got = [n for (n, p, t, v) in hw.sensors if t == stype][:8]
                return ", ".join(got) if got else "(none)"
            log(f"          LHM sees these Temperature sensors: {names('Temperature')}")
            log(f"          LHM sees these Voltage sensors:     {names('Voltage')}")
            log(f"          LHM sees these Fan sensors:         {names('Fan')}")
            if names("Voltage") == "(none)":
                log("          -> LHM exposes NO Voltage sensors at all: open "
                    "LHM's own window - if it shows no Voltages group, the "
                    "board's Super I/O chip is unsupported and Vcore cannot "
                    "be read.")
            if names("Fan") == "(none)":
                log("          -> LHM exposes NO Fan sensors at all: open "
                    "LHM's own window - if it shows no Fans group, fan RPM "
                    "cannot be read on this board.")
            log("          (if your CPU sensor is missing here, tell me these "
                "names and I'll match it)")
        else:
            log("          (no sensors parsed from LHM yet)")
    if probe["gt"] < 0 and not nv.ok:
        log("       -> GPU sensor gaps: NVIDIA users: pip install nvidia-ml-py ;"
            " others: run LibreHardwareMonitor AS ADMIN.")
    log("       (use --debug-sensors to see everything LibreHardwareMonitor "
        "can measure)")
    log("-" * 68)

    # ---------------- serial connection (with auto-reconnect) -------------
    ser = None
    port_name = args.port
    if not args.dry_run:
        while ser is None:
            if not port_name:
                port_name = autodetect_port()
            if not port_name:
                log("[WARN] No serial port found. Plug the ESP32 USB cable in "
                    "(install CP2102/CH340 driver if needed). Retrying...")
                time.sleep(4)
                continue
            try:
                ser = serial.Serial(port_name, args.baud, timeout=0.1,
                                    write_timeout=2)
                log(f"[ OK ] Serial open: {port_name} @ {args.baud}")
            except Exception as e:
                log(f"[WARN] Cannot open {port_name}: {e}")
                log("       Is the Arduino Serial Monitor still open? Close it. "
                    "Retrying...")
                port_name = None
                time.sleep(4)

    log("Streaming data. Start a game to see FPS appear in the GPU section.")
    log("Press Ctrl+C to stop.")
    log("-" * 68)

    next_t = time.monotonic()
    sent = 0
    while True:
        try:
            pkt = sample()
            line = json.dumps(pkt, separators=(",", ":"))

            # ---- send ----
            if args.dry_run:
                log(line)
            else:
                if ser is not None:
                    try:
                        ser.write((line + "\n").encode("ascii", errors="ignore"))
                        sent += 1
                    except Exception as e:
                        log(f"[WARN] Serial write failed ({e}) - reconnecting...")
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser = None
                        port_name = args.port
                        time.sleep(2)
                        continue
                if ser is None:
                    try:
                        if not port_name:
                            port_name = autodetect_port()
                        if port_name:
                            ser = serial.Serial(port_name, args.baud,
                                                timeout=0.1, write_timeout=2)
                            log(f"[ OK ] Serial reconnected: {port_name}")
                    except Exception:
                        port_name = None

            # periodic reminders for missing data sources
            if sent > 0 and sent % 200 == 0:
                if hw.conn is None:
                    log("[REMINDER] LibreHardwareMonitor still not detected - "
                        "CPU Vcore/fan show '--' until you run it (as admin).")
                if not fpsmon.ok:
                    log("[REMINDER] PresentMon not running - FPS stays 0. See "
                        "README section 4.1.")

            if not args.quiet and sent % 20 == 0 and sent > 0:
                log(f"  ... {sent} updates sent "
                    f"(cpu {pkt['cpu']:.0f}%  gpu {pkt['gpu']:.0f}%  "
                    f"fps {pkt['fps']}  mem {pkt['mem']:.0f}%  "
                    f"net {pkt['down']:.0f}/{pkt['up']:.0f} KB/s)")

            # ---- pace the loop ----
            next_t += args.interval
            sleep_t = next_t - time.monotonic()
            if sleep_t > 0:
                time.sleep(sleep_t)
            else:
                next_t = time.monotonic()

        except KeyboardInterrupt:
            log("\nStopped.")
            break
        except Exception as e:
            log(f"[ERROR] {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
