from prometheus_client import start_http_server, Gauge, Counter
import glob
import os
import re
import sys
import subprocess
import json
import logging
import threading
import time

# Engine key compatibility helper (MTL/Xe vs legacy /0 keys)
# Returns first present value from candidate engine keys and coerces to float.
def eng_val(data, names, field):
    e = data.get("engines", {})
    for n in names:
        v = e.get(n, {}).get(field)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0



igpu_device_id = Gauge(
    "igpu_device_id", "Intel GPU device id"
)

igpu_engines_blitter_0_busy = Gauge(
    "igpu_engines_blitter_0_busy", "Blitter 0 busy utilisation %"
)
igpu_engines_blitter_0_sema = Gauge(
    "igpu_engines_blitter_0_sema", "Blitter 0 sema utilisation %"
)
igpu_engines_blitter_0_wait = Gauge(
    "igpu_engines_blitter_0_wait", "Blitter 0 wait utilisation %"
)

igpu_engines_render_3d_0_busy = Gauge(
    "igpu_engines_render_3d_0_busy", "Render 3D 0 busy utilisation %"
)
igpu_engines_render_3d_0_sema = Gauge(
    "igpu_engines_render_3d_0_sema", "Render 3D 0 sema utilisation %"
)
igpu_engines_render_3d_0_wait = Gauge(
    "igpu_engines_render_3d_0_wait", "Render 3D 0 wait utilisation %"
)

igpu_engines_video_0_busy = Gauge(
    "igpu_engines_video_0_busy", "Video 0 busy utilisation %"
)
igpu_engines_video_0_sema = Gauge(
    "igpu_engines_video_0_sema", "Video 0 sema utilisation %"
)
igpu_engines_video_0_wait = Gauge(
    "igpu_engines_video_0_wait", "Video 0 wait utilisation %"
)

igpu_engines_video_enhance_0_busy = Gauge(
    "igpu_engines_video_enhance_0_busy", "Video Enhance 0 busy utilisation %"
)
igpu_engines_video_enhance_0_sema = Gauge(
    "igpu_engines_video_enhance_0_sema", "Video Enhance 0 sema utilisation %"
)
igpu_engines_video_enhance_0_wait = Gauge(
    "igpu_engines_video_enhance_0_wait", "Video Enhance 0 wait utilisation %"
)

igpu_frequency_actual = Gauge("igpu_frequency_actual", "Frequency actual MHz")
igpu_frequency_requested = Gauge("igpu_frequency_requested", "Frequency requested MHz")

igpu_imc_bandwidth_reads = Gauge("igpu_imc_bandwidth_reads", "IMC reads MiB/s")
igpu_imc_bandwidth_writes = Gauge("igpu_imc_bandwidth_writes", "IMC writes MiB/s")

igpu_interrupts = Gauge("igpu_interrupts", "Interrupts/s")

igpu_period = Gauge("igpu_period", "Period ms")

igpu_power_gpu = Gauge("igpu_power_gpu", "GPU power W")
igpu_power_package = Gauge("igpu_power_package", "Package power W")

igpu_rc6 = Gauge("igpu_rc6", "RC6 %")

igpu_engines_busy_max = Gauge(
    "igpu_engines_busy_max", "Maximum busy utilisation % across all engines"
)

# Intel NPU (VPU) metrics via /sys/class/accel sysfs (kernel 6.11+)
inpu_busy = Gauge("inpu_busy", "Intel NPU busy utilisation %")
inpu_busy_time_us_total = Counter(
    "inpu_busy_time_us_total",
    "Intel NPU cumulative busy time microseconds",
)
inpu_frequency_actual = Gauge(
    "inpu_frequency_actual",
    "Intel NPU current frequency MHz (peak sampled over poll interval)",
)
inpu_frequency_max = Gauge("inpu_frequency_max", "Intel NPU maximum frequency MHz")

# Intel SoC package power via RAPL (fallback / supplement to igpu_power_package
# which is unreliable on Core Ultra / Xe driver)
isoc_power_package_watts = Gauge(
    "isoc_power_package_watts", "Intel SoC package power from RAPL W"
)


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def find_npu_device():
    """Find first accel device exposing npu_busy_time_us. Returns device dir or None."""
    for accel in sorted(glob.glob("/sys/class/accel/accel*")):
        device_dir = os.path.join(accel, "device")
        if os.path.exists(os.path.join(device_dir, "npu_busy_time_us")):
            return device_dir
    return None


def npu_poll_loop(device_dir, interval_sec):
    """Poll NPU sysfs entries and update gauges/counter until process exit.

    Frequency is peak-sampled many times per interval because the NPU aggressively
    clock-gates between inference bursts and a single-shot read usually returns 0.
    """
    busy_path = os.path.join(device_dir, "npu_busy_time_us")
    freq_path = os.path.join(device_dir, "npu_current_frequency_mhz")
    freq_max_path = os.path.join(device_dir, "npu_max_frequency_mhz")

    # Max frequency is static — read once
    fmax = _read_int(freq_max_path)
    if fmax is not None:
        inpu_frequency_max.set(fmax)

    samples_per_interval = 20
    sub_interval = interval_sec / samples_per_interval

    last_busy_us = None
    last_time_ns = None
    counter_last_us = None

    while True:
        # Peak-sample frequency across the interval
        max_freq = 0
        for _ in range(samples_per_interval):
            f = _read_int(freq_path)
            if f is not None and f > max_freq:
                max_freq = f
            time.sleep(sub_interval)
        inpu_frequency_actual.set(max_freq)

        # Read busy_time at the end of the interval for accurate delta
        try:
            now_ns = time.monotonic_ns()
            busy_us = _read_int(busy_path)

            if busy_us is not None:
                # Drive the Counter (monotonically increasing)
                if counter_last_us is None:
                    counter_last_us = busy_us
                elif busy_us >= counter_last_us:
                    inpu_busy_time_us_total.inc(busy_us - counter_last_us)
                    counter_last_us = busy_us
                else:
                    # Counter reset (driver reload / wraparound) — rebase
                    counter_last_us = busy_us

                # Drive the busy % Gauge
                if last_busy_us is not None and last_time_ns is not None:
                    delta_busy_us = busy_us - last_busy_us
                    delta_time_us = (now_ns - last_time_ns) / 1000.0
                    if delta_time_us > 0 and delta_busy_us >= 0:
                        pct = (delta_busy_us / delta_time_us) * 100.0
                        if pct < 0:
                            pct = 0.0
                        elif pct > 100:
                            pct = 100.0
                        inpu_busy.set(pct)
                last_busy_us = busy_us
                last_time_ns = now_ns
        except Exception as e:
            logging.warning("NPU poll error: %s", e)


def start_npu_monitor():
    """Start NPU monitoring thread if an Intel NPU is available."""
    device_dir = find_npu_device()
    if device_dir is None:
        logging.info("No Intel NPU detected, skipping NPU monitoring")
        return
    logging.info("Found Intel NPU at %s, starting monitor", device_dir)
    interval = float(os.getenv("NPU_POLL_INTERVAL_SEC", "1"))
    t = threading.Thread(
        target=npu_poll_loop, args=(device_dir, interval), daemon=True
    )
    t.start()


def find_package_rapl():
    """Find the package-0 RAPL zone under /sys/class/powercap."""
    for path in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")):
        name_path = os.path.join(path, "name")
        try:
            with open(name_path) as f:
                if f.read().strip() == "package-0":
                    return path
        except Exception:
            pass
    return None


def rapl_poll_loop(rapl_dir, interval_sec):
    """Compute SoC package power from RAPL energy counter deltas."""
    energy_path = os.path.join(rapl_dir, "energy_uj")
    max_energy_path = os.path.join(rapl_dir, "max_energy_range_uj")
    max_energy_uj = _read_int(max_energy_path)

    last_energy_uj = None
    last_time_ns = None

    while True:
        try:
            now_ns = time.monotonic_ns()
            energy_uj = _read_int(energy_path)
            if energy_uj is not None and last_energy_uj is not None:
                delta_uj = energy_uj - last_energy_uj
                if delta_uj < 0 and max_energy_uj:
                    # Counter wrap
                    delta_uj += max_energy_uj
                delta_sec = (now_ns - last_time_ns) / 1e9
                if delta_sec > 0 and delta_uj >= 0:
                    watts = (delta_uj / 1e6) / delta_sec
                    isoc_power_package_watts.set(watts)
            if energy_uj is not None:
                last_energy_uj = energy_uj
                last_time_ns = now_ns
        except Exception as e:
            logging.warning("RAPL poll error: %s", e)
        time.sleep(interval_sec)


def start_rapl_monitor():
    """Start RAPL package-power thread if the zone is available."""
    rapl_dir = find_package_rapl()
    if rapl_dir is None:
        logging.info("No RAPL package-0 zone found, skipping RAPL monitoring")
        return
    logging.info("Found RAPL package-0 at %s, starting monitor", rapl_dir)
    interval = float(os.getenv("RAPL_POLL_INTERVAL_SEC", "1"))
    t = threading.Thread(
        target=rapl_poll_loop, args=(rapl_dir, interval), daemon=True
    )
    t.start()



def update(data):
    # Resolve engine metrics across old/new key formats
    blit_busy = eng_val(data, ["Blitter/0", "Blitter"], "busy")
    blit_sema = eng_val(data, ["Blitter/0", "Blitter"], "sema")
    blit_wait = eng_val(data, ["Blitter/0", "Blitter"], "wait")
    r3d_busy = eng_val(data, ["Render/3D/0", "Render/3D"], "busy")
    r3d_sema = eng_val(data, ["Render/3D/0", "Render/3D"], "sema")
    r3d_wait = eng_val(data, ["Render/3D/0", "Render/3D"], "wait")
    vid_busy = eng_val(data, ["Video/0", "Video"], "busy")
    vid_sema = eng_val(data, ["Video/0", "Video"], "sema")
    vid_wait = eng_val(data, ["Video/0", "Video"], "wait")
    ven_busy = eng_val(data, ["VideoEnhance/0", "VideoEnhance"], "busy")
    ven_sema = eng_val(data, ["VideoEnhance/0", "VideoEnhance"], "sema")
    ven_wait = eng_val(data, ["VideoEnhance/0", "VideoEnhance"], "wait")

    igpu_engines_blitter_0_busy.set(blit_busy)
    igpu_engines_blitter_0_sema.set(blit_sema)
    igpu_engines_blitter_0_wait.set(blit_wait)

    igpu_engines_render_3d_0_busy.set(r3d_busy)
    igpu_engines_render_3d_0_sema.set(r3d_sema)
    igpu_engines_render_3d_0_wait.set(r3d_wait)

    igpu_engines_video_0_busy.set(vid_busy)
    igpu_engines_video_0_sema.set(vid_sema)
    igpu_engines_video_0_wait.set(vid_wait)

    igpu_engines_video_enhance_0_busy.set(ven_busy)
    igpu_engines_video_enhance_0_sema.set(ven_sema)
    igpu_engines_video_enhance_0_wait.set(ven_wait)

    igpu_frequency_actual.set(data.get("frequency", {}).get("actual", 0))
    igpu_frequency_requested.set(data.get("frequency", {}).get("requested", 0))
    igpu_imc_bandwidth_reads.set(data.get("imc-bandwidth", {}).get("reads", 0))
    igpu_imc_bandwidth_writes.set(data.get("imc-bandwidth", {}).get("writes", 0))
    igpu_interrupts.set(data.get("interrupts", {}).get("count", 0))
    igpu_period.set(data.get("period", {}).get("duration", 0))
    igpu_power_gpu.set(data.get("power", {}).get("GPU", 0))
    igpu_power_package.set(data.get("power", {}).get("Package", 0))

    # RC6 percent (0-100)
    try:
        rc6_value = float(data.get("rc6", {}).get("value", 0) or 0)
    except Exception:
        rc6_value = 0.0
    igpu_rc6.set(rc6_value)

    # Optional fallback: derive non-idle percent from RC6 for targets with zero busy
    try:
        fb = os.getenv("FALLBACK_FROM_RC6", "0").lower() in ("1","true","yes","on")
        targets = [t.strip() for t in os.getenv("FALLBACK_TARGETS", "Video").split(',') if t.strip()]
    except Exception:
        fb = False
        targets = []
    if fb:
        active = max(0.0, 100.0 - rc6_value)
        if "Video" in targets and vid_busy <= 0:
            igpu_engines_video_0_busy.set(active)
        if ("Render/3D" in targets or "Render" in targets) and r3d_busy <= 0:
            igpu_engines_render_3d_0_busy.set(active)
        if "Blitter" in targets and blit_busy <= 0:
            igpu_engines_blitter_0_busy.set(active)
        if "VideoEnhance" in targets and ven_busy <= 0:
            igpu_engines_video_enhance_0_busy.set(active)

    # Calculate maximum busy utilization across all engines
    busy_values = [
        blit_busy,
        r3d_busy,
        vid_busy,
        ven_busy,
    ]
    igpu_engines_busy_max.set(max(busy_values))


if __name__ == "__main__":
    if os.getenv("DEBUG", False):
        debug = logging.DEBUG
    else:
        debug = logging.INFO
    logging.basicConfig(format="%(asctime)s - %(message)s", level=debug)

    start_http_server(int(os.getenv("LISTEN_PORT", 9080)))

    start_npu_monitor()
    start_rapl_monitor()

    period = os.getenv("REFRESH_PERIOD_MS", 1000)
    device = os.getenv("DEVICE")

    # Detect device id for reporting
    list_cmd = "intel_gpu_top -L"
    out, _ = subprocess.Popen(
        list_cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).communicate()
    out = out.decode()

    m = re.search(r"(?:device0|device)=(\w+)", out) or re.search(r"pci:vendor=\w+,device=(\w+)", out)
    device_id = int("0x" + m.group(1), 16) if m else 0

    igpu_device_id.set(device_id)

    if device is not None:
        cmd = "intel_gpu_top -J -s {} -d {}".format(int(period), device)
    else:
        cmd = "intel_gpu_top -J -s {}".format(int(period))

    process = subprocess.Popen(
        cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    logging.info("Started " + cmd)
    # Robust streaming JSON parse: bracket-depth framing
    buf = ''
    depth = 0
    started = False
    while True:
        chunk = process.stdout.read(4096)
        if not chunk:
            break
        for ch in chunk.decode('utf-8', 'ignore'):
            if not started:
                if ch == '{':
                    started = True
                    depth = 1
                    buf = '{'
            else:
                buf += ch
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(buf)
                            logging.debug(data)
                            update(data)
                        except Exception:
                            pass
                        buf = ''
                        started = False
    process.kill()

    if process.returncode != 0:
        logging.error("Error: " + process.stderr.read().decode("utf-8"))

    logging.info("Finished")
