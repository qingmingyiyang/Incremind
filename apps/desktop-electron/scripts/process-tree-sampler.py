from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psutil


def main() -> int:
    root_pid = int(sys.argv[1])
    stop_path = Path(sys.argv[2])
    output_path = Path(sys.argv[3])
    interval_seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 0.1
    ready_path = Path(sys.argv[5])
    if not 0.1 <= interval_seconds <= 60:
        raise ValueError("process_tree_sampler_interval_out_of_range")
    peak_rss = 0
    peak_processes = 0
    peak_private_bytes = 0
    peak_handles = 0
    peak_threads = 0
    cpu_seconds = 0.0
    read_bytes = 0
    write_bytes = 0
    samples = 0
    metrics: list[dict[str, float | int]] = []
    access_denied_count = 0
    root = psutil.Process(root_pid)
    root_create_time = root.create_time()
    started = time.monotonic()
    next_sample_at = started
    ready_path.write_text("ready\n", encoding="utf-8")
    while not stop_path.exists():
        try:
            root = psutil.Process(root_pid)
            if root.create_time() != root_create_time:
                raise RuntimeError("process_tree_sampler_root_identity_changed")
            processes = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess as error:
            raise RuntimeError("process_tree_sampler_root_disappeared") from error
        except psutil.AccessDenied as error:
            raise RuntimeError("process_tree_sampler_root_access_denied") from error
        rss = 0
        private_bytes = 0
        handles = 0
        threads = 0
        current_cpu = 0.0
        current_read = 0
        current_write = 0
        for process in processes:
            try:
                rss += process.memory_info().rss
                memory = process.memory_info()
                private_bytes += int(getattr(memory, "private", 0))
                handles += int(process.num_handles()) if hasattr(process, "num_handles") else 0
                threads += int(process.num_threads())
                times = process.cpu_times()
                current_cpu += times.user + times.system
                io = process.io_counters()
                current_read += io.read_bytes
                current_write += io.write_bytes
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                access_denied_count += 1
                continue
        peak_rss = max(peak_rss, rss)
        peak_processes = max(peak_processes, len(processes))
        peak_private_bytes = max(peak_private_bytes, private_bytes)
        peak_handles = max(peak_handles, handles)
        peak_threads = max(peak_threads, threads)
        cpu_seconds = max(cpu_seconds, current_cpu)
        read_bytes = max(read_bytes, current_read)
        write_bytes = max(write_bytes, current_write)
        samples += 1
        metrics.append(
            {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "rss_bytes": rss,
                "private_bytes": private_bytes,
                "handles": handles,
                "threads": threads,
                "processes": len(processes),
                "cpu_seconds": round(current_cpu, 3),
                "read_bytes": current_read,
                "write_bytes": current_write,
            }
        )
        next_sample_at += interval_seconds
        sleep_seconds = next_sample_at - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    output_path.write_text(
        json.dumps(
            {
                "root_pid": root_pid,
                "root_create_time": root_create_time,
                "samples": samples,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "peak_rss_bytes": peak_rss,
                "peak_processes": peak_processes,
                "peak_private_bytes": peak_private_bytes,
                "peak_handles": peak_handles,
                "peak_threads": peak_threads,
                "cpu_seconds": round(cpu_seconds, 3),
                "read_bytes": read_bytes,
                "write_bytes": write_bytes,
                "access_denied_count": access_denied_count,
                "metrics": metrics,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
