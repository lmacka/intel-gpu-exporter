# intel-gpu-exporter

Prometheus exporter for Intel integrated GPU, NPU, and SoC power on Linux.

> Fork of [onedr0p/intel-gpu-exporter](https://github.com/onedr0p/intel-gpu-exporter)
> (via [bjia56/intel-gpu-exporter](https://github.com/bjia56/intel-gpu-exporter)).
> Actively used in production on Intel NUC Meteor Lake (Core Ultra) nodes running
> Talos Linux / Kubernetes. Frigate is the primary workload exercising the NPU.

## What it exposes

- **iGPU** (via `intel_gpu_top -J`): per-engine busy/sema/wait %, frequency, IMC
  bandwidth, interrupts, power, RC6
- **NPU** (via `/sys/class/accel/accel*/device/*`): busy %, peak-sampled current
  frequency, max frequency, cumulative busy-time counter
- **Intel SoC package power** (via `/sys/class/powercap/intel-rapl:0`): whole-SoC
  power derived from RAPL energy counter — works even on Core Ultra where
  `intel_gpu_top` reports zero power

The NPU and RAPL paths are optional at runtime: if the sysfs entries are missing,
those collectors log a message and no-op, so the same image runs on nodes with
any subset of (iGPU, NPU, RAPL).

## Requirements

- Linux kernel 6.11+ for NPU sysfs entries (`npu_busy_time_us`,
  `npu_current_frequency_mhz`, `npu_max_frequency_mhz`)
- Intel CPU with `intel_vpu` driver for NPU support (Meteor Lake / Core Ultra and
  newer)
- `/dev/dri/*` for iGPU
- `/sys/class/powercap/intel-rapl:0` for SoC power
- Runs as a privileged container because `intel_gpu_top` needs PMU access

## Docker Compose

```yaml
services:
  intel-gpu-exporter:
    image: ghcr.io/lmacka/intel-gpu-exporter:v0.2.0-npu
    container_name: intel-gpu-exporter
    restart: unless-stopped
    privileged: true
    pid: host
    ports:
      - 9080:9080
    volumes:
      - /dev/dri:/dev/dri:ro
      - /sys/class/accel:/sys/class/accel:ro
      - /sys/class/powercap:/sys/class/powercap:ro
```

## Kubernetes DaemonSet

A privileged DaemonSet with host sysfs mounts. This is what's running in
production on Talos.

```yaml
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: intel-gpu-exporter
  namespace: kube-system
  labels:
    app: intel-gpu-exporter
spec:
  selector:
    matchLabels:
      app: intel-gpu-exporter
  template:
    metadata:
      labels:
        app: intel-gpu-exporter
    spec:
      hostPID: true
      containers:
        - name: intel-gpu-exporter
          image: ghcr.io/lmacka/intel-gpu-exporter:v0.2.0-npu
          imagePullPolicy: IfNotPresent
          securityContext:
            privileged: true
          ports:
            - name: http
              containerPort: 9080
              hostPort: 9080
              protocol: TCP
          volumeMounts:
            - name: devdri
              mountPath: /dev/dri
              readOnly: true
            - name: sysaccel
              mountPath: /sys/class/accel
              readOnly: true
            - name: syspowercap
              mountPath: /sys/class/powercap
              readOnly: true
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
      volumes:
        - name: devdri
          hostPath:
            path: /dev/dri
        - name: sysaccel
          hostPath:
            path: /sys/class/accel
        - name: syspowercap
          hostPath:
            path: /sys/class/powercap
---
apiVersion: v1
kind: Service
metadata:
  name: intel-gpu-exporter
  namespace: kube-system
  labels:
    app: intel-gpu-exporter
spec:
  selector:
    app: intel-gpu-exporter
  ports:
    - name: http
      port: 9080
      targetPort: 9080
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: intel-gpu-exporter
  namespace: kube-system
  labels:
    app: intel-gpu-exporter
spec:
  selector:
    matchLabels:
      app: intel-gpu-exporter
  endpoints:
    - port: http
      interval: 15s
```

Notes on the Talos/Kubernetes side:

- `privileged: true` is required — the container needs CAP_SYS_ADMIN and access
  to perf PMU, which `securityContext.capabilities.add` alone does not grant
  cleanly for `intel_gpu_top`.
- `hostPort` is used so Prometheus can scrape a stable endpoint per node; replace
  with a ServiceMonitor if you prefer pod-IP scraping.
- On Talos Linux, both `/sys/class/accel` and `/sys/class/powercap` are mounted
  on the host and can be hostPath'd in read-only.
- The exporter logs which collectors it starts on boot, e.g.:
  ```
  Found Intel NPU at /sys/class/accel/accel0/device, starting monitor
  Found RAPL package-0 at /sys/class/powercap/intel-rapl:0, starting monitor
  ```

## Metrics

### iGPU

| Metric | Type | Unit | Description |
|---|---|---|---|
| `igpu_device_id` | gauge | — | PCI device id |
| `igpu_engines_blitter_0_busy` | gauge | % | Blitter engine busy |
| `igpu_engines_blitter_0_sema` | gauge | % | Blitter engine sema wait |
| `igpu_engines_blitter_0_wait` | gauge | % | Blitter engine wait |
| `igpu_engines_render_3d_0_busy` | gauge | % | Render/3D engine busy |
| `igpu_engines_render_3d_0_sema` | gauge | % | Render/3D engine sema wait |
| `igpu_engines_render_3d_0_wait` | gauge | % | Render/3D engine wait |
| `igpu_engines_video_0_busy` | gauge | % | Video engine busy (decode/encode) |
| `igpu_engines_video_0_sema` | gauge | % | Video engine sema wait |
| `igpu_engines_video_0_wait` | gauge | % | Video engine wait |
| `igpu_engines_video_enhance_0_busy` | gauge | % | VideoEnhance engine busy |
| `igpu_engines_video_enhance_0_sema` | gauge | % | VideoEnhance engine sema wait |
| `igpu_engines_video_enhance_0_wait` | gauge | % | VideoEnhance engine wait |
| `igpu_engines_busy_max` | gauge | % | Max busy across all engines |
| `igpu_frequency_actual` | gauge | MHz | Current GPU frequency |
| `igpu_frequency_requested` | gauge | MHz | Requested GPU frequency |
| `igpu_imc_bandwidth_reads` | gauge | MiB/s | IMC reads |
| `igpu_imc_bandwidth_writes` | gauge | MiB/s | IMC writes |
| `igpu_interrupts` | gauge | /s | GPU interrupts |
| `igpu_period` | gauge | ms | Sampling period |
| `igpu_power_gpu` | gauge | W | GPU power (from `intel_gpu_top`) |
| `igpu_power_package` | gauge | W | Package power (from `intel_gpu_top`) |
| `igpu_rc6` | gauge | % | RC6 residency |

### NPU (Intel VPU)

| Metric | Type | Unit | Description |
|---|---|---|---|
| `inpu_busy` | gauge | % | NPU busy utilisation, derived from `npu_busy_time_us` delta |
| `inpu_busy_time_us_total` | counter | μs | Cumulative NPU busy time |
| `inpu_frequency_actual` | gauge | MHz | Peak-sampled NPU frequency over poll interval |
| `inpu_frequency_max` | gauge | MHz | Maximum NPU frequency |

`inpu_frequency_actual` is peak-sampled 20 times per polling interval (default
every 50 ms) because the NPU aggressively clock-gates between inference bursts —
a single-shot read will usually return 0 even under heavy load.

### Intel SoC (RAPL)

| Metric | Type | Unit | Description |
|---|---|---|---|
| `isoc_power_package_watts` | gauge | W | Package-0 power derived from RAPL energy counter |

`isoc_power_package_watts` is independent of `intel_gpu_top` and works on Core
Ultra / Xe driver where `igpu_power_package` returns 0.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `LISTEN_PORT` | `9080` | HTTP port the exporter binds to |
| `REFRESH_PERIOD_MS` | `1000` | `intel_gpu_top -s` sampling period |
| `DEVICE` | *(auto)* | `intel_gpu_top -d <device>` filter (for multi-GPU hosts) |
| `NPU_POLL_INTERVAL_SEC` | `1` | NPU sysfs polling interval |
| `RAPL_POLL_INTERVAL_SEC` | `1` | RAPL energy polling interval |
| `FALLBACK_FROM_RC6` | `0` | Derive non-idle % as `100 - rc6` when engine busy reports 0 |
| `FALLBACK_TARGETS` | `Video` | Comma-separated engines for the RC6 fallback: `Video,Render/3D,Blitter,VideoEnhance` |
| `DEBUG` | *(unset)* | Enable DEBUG level logging |

## Compatibility notes

- **Engine key compatibility**: supports both legacy (e.g. `Video/0`) and new
  (`Video`) `intel_gpu_top` JSON keys. Needed for Core Ultra / Xe driver.
- **Robust JSON framing**: stream parser tolerates `intel_gpu_top`'s irregular
  output (no trailing commas, brace-depth counter).
- **Graceful collector absence**: NPU and RAPL collectors no-op if sysfs entries
  are missing, so one image works on any node.

## Building

```bash
docker build -t ghcr.io/lmacka/intel-gpu-exporter:dev .
```

CI is set up to build and push to `ghcr.io/lmacka/intel-gpu-exporter:main` on
every push to main, using a self-hosted BuildKit runner on Kubernetes. See
`.github/workflows/build.yaml`.

## License

Inherits from upstream. See [onedr0p/intel-gpu-exporter](https://github.com/onedr0p/intel-gpu-exporter).
