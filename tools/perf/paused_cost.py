"""Measures what the game costs while paused: main-thread CPU per second and per frame,
and the number of hidden widgets that pile up (closed message popups and the like).

Frames are capped (vsync), so a costlier frame shows up as main-thread CPU, not fps.

    python paused_cost.py --seconds 10
"""
import argparse
import ctypes
import json
import sys
import time

from session_log import DEFAULT_TONGS, process_counters

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenThread.restype = ctypes.c_void_p
k32.GetThreadTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
COUNTED = ["DefaultPopup", "hints_test", "outliner_header_entry"]


def thread_cpu(handle):
    times = [ctypes.c_uint64() for _ in range(4)]
    k32.GetThreadTimes(handle, *[ctypes.byref(t) for t in times])
    return (times[2].value + times[3].value) / 1e7


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--label", default="")
    parser.add_argument("--tongs", default=str(DEFAULT_TONGS))
    args = parser.parse_args(argv)
    sys.path.insert(0, args.tongs)
    from tongs import Tongs  # noqa: E402

    with Tongs() as game:
        state = game.call("state")
        if not state.get("paused"):
            print("the game is running; pause it first", file=sys.stderr)
            return 1
        tid = game.call("window")["thread_id"]
        handle = k32.OpenThread(0x0040, False, tid)  # THREAD_QUERY_INFORMATION
        counts = {name: game.call(f"ui find {name} all limit=1").get("count") for name in COUNTED}
        game.call("frames")
        cpu0, t0 = thread_cpu(handle), time.monotonic()
        time.sleep(args.seconds)
        frames = game.call("frames")
        cpu, wall = thread_cpu(handle) - cpu0, time.monotonic() - t0
    fps = frames.get("outer_since", 0) / wall
    result = {"label": args.label, "date": state.get("date"), "pid": state.get("pid"),
              "main_cpu_pct": round(100 * cpu / wall, 1), "fps": round(fps, 1),
              "main_ms_per_frame": round(1000 * cpu / max(frames.get("outer_since", 1), 1), 2),
              **counts, "private_mb": process_counters(state["pid"])["private_mb"]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
