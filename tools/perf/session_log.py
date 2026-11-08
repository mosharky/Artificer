"""Logs how EU4's speed and memory change over a long session, through Tongs.

Runs the loaded game forward in chunks of game time with `run_until`, and after
each chunk records:

- simulation speed: game days per wall-clock second over the chunk;
- render speed: frames per second while paused, over a few seconds;
- the process: private and working-set memory, handles, GDI and USER objects,
  threads and CPU time, read from Windows.

Each chunk appends one row to a CSV (for plotting) and one JSON line with the
raw Tongs answers. After each chunk it presses OK on the message popups, as a
player would. The run never saves the game, but the game's own autosaves
happen. With `--events first` (default) it picks the first option of every
event that opens, so do not play on from those saves if the choices matter.

Needs Tongs with `enabled = true` and `input = true`, a loaded game, and the
Tongs Python client (scripts/tongs.py of the Tongs repository).

    python session_log.py --step 6m --hours 3
    python session_log.py --step 1y --until 1600.1.1 --out runs/anbennar
"""
import argparse
import csv
import ctypes
import datetime
import json
import pathlib
import sys
import time
from ctypes import wintypes

DEFAULT_TONGS = pathlib.Path(__file__).resolve().parents[3] / "tongs" / "scripts"

CSV_FIELDS = [
    "wall_time", "session_min", "start_date", "end_date", "game_days", "wall_ms", "days_per_s",
    "paused_ms", "inputs", "event_choices", "game_calls", "clean", "popups_closed",
    "fps_paused", "longest_gap_ms", "private_mb", "working_set_mb", "peak_working_set_mb",
    "handles", "gdi_objects", "user_objects", "threads", "cpu_s", "cpu_pct",
]

# --- Process counters (Windows) -------------------------------------------------

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x2
GR_GDIOBJECTS, GR_USEROBJECTS = 0, 1

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
_user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]


def _filetime_s(ft):
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def _thread_count(pid):
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return entry.cntThreads
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        _kernel32.CloseHandle(snap)


def process_counters(pid):
    """Memory, handle, GUI object, thread and CPU counters of the process."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError(f"OpenProcess({pid}) failed: error {ctypes.get_last_error()}")
    try:
        mem = PROCESS_MEMORY_COUNTERS_EX(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX))
        _psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb)
        handles = wintypes.DWORD(0)
        _kernel32.GetProcessHandleCount(handle, ctypes.byref(handles))
        times = [wintypes.FILETIME() for _ in range(4)]
        _kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times])
        mb = 1024 * 1024
        return {
            "private_mb": round(mem.PrivateUsage / mb, 1),
            "working_set_mb": round(mem.WorkingSetSize / mb, 1),
            "peak_working_set_mb": round(mem.PeakWorkingSetSize / mb, 1),
            "handles": handles.value,
            "gdi_objects": _user32.GetGuiResources(handle, GR_GDIOBJECTS),
            "user_objects": _user32.GetGuiResources(handle, GR_USEROBJECTS),
            "threads": _thread_count(pid),
            "cpu_s": round(_filetime_s(times[2]) + _filetime_s(times[3]), 2),  # kernel + user
        }
    finally:
        _kernel32.CloseHandle(handle)


# --- Game dates (EU4: 365-day years, no leap days) ----------------------------------

def parse_date(text):
    year, month, day = (int(part) for part in text.split(".")[:3])
    return year, month, day


def add_step(date, step):
    """The first day of the month `step` after `date`; `step` is like 6m, 1y or 30d."""
    year, month, day = date
    count, unit = int(step[:-1]), step[-1]
    if unit == "d":
        days_in = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        day += count
        while day > days_in[month - 1]:
            day -= days_in[month - 1]
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return year, month, day
    months = count * 12 if unit == "y" else count
    if unit not in "my":
        raise ValueError(f"bad step {step!r}: use Nd, Nm or Ny")
    total = year * 12 + (month - 1) + months
    return total // 12, total % 12 + 1, 1


def fmt_date(date):
    return "{}.{}.{}".format(*date)


# --- Measuring ------------------------------------------------------------------

def paused_fps(game, seconds):
    """Main-loop frames per second while paused, over `seconds`."""
    game.call("frames")  # resets the counters
    time.sleep(seconds)
    answer = game.call("frames")
    if not answer.get("ok"):
        return None, None, answer
    return round(answer["outer_since"] / seconds, 1), answer.get("longest_gap_ms"), answer


DISMISS_TEXTS = {"ok", "okay", "close", "confirm", "continue"}

MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]


def drag_window_up(game, path, title_offset=30, top=30):
    """Drags a moveable game window up by its title with the real mouse (Tongs has no drag)."""
    found = game.call(f"ui find {path}")
    window = game.call("window")
    if found.get("count") != 1 or not window.get("found"):
        return False
    rect = found["matches"][0]["rect"]
    x = window["screen_x"] + rect["x"] + rect["w"] // 2
    y = window["screen_y"] + rect["y"] + title_offset
    _user32.SetCursorPos(x, y)
    time.sleep(0.3)
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.3)
    while y > window["screen_y"] + top:
        y = max(y - 10, window["screen_y"] + top)
        _user32.SetCursorPos(x, y)
        time.sleep(0.03)
    time.sleep(0.3)
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.5)
    return True


# The OK buttons of the game's message popups (DefaultPopup, which pile up when nobody closes
# them) and of its generic confirmation box (DefaultConfirm, e.g. Anbennar's achievement notice).
# Searched with `ui find`: the interface tree is cut off long before the popups come in it.
POPUP_BUTTONS = ["DefaultPopup/AgreeButton", "DefaultPopup/CenterOK", "DefaultConfirm/AgreeButton"]


# Buttons that close the window covering a click (e.g. new_age_popup/close_button), tried in order.
CLOSE_BUTTONS = ["close_button", "CloseButton", "AgreeButton", "CenterOK"]


def close_covering(game, covered_by, depth=5):
    """Closes the window named in a click's `covered_by` (and whatever covers that); True if it did."""
    if not covered_by:
        return False  # Tongs did not say what covers the click
    window = covered_by.split("/")[0]
    if window.startswith("EventWindow"):
        return False  # events are chosen, not closed
    for name in CLOSE_BUTTONS:
        found = game.call(f"ui find {window}/{name}")
        match = next((m for m in found.get("matches", []) if m.get("visible") and m.get("enabled", True)), None)
        if match is None:
            continue
        answer = game.call(f"ui click {match['path']}")
        if answer.get("ok"):
            time.sleep(0.1)
            return True
        if answer.get("error") == "covered" and depth > 0 and answer.get("covered_by"):
            if close_covering(game, answer["covered_by"], depth - 1):
                return True
        # Its button under the menu bar at the screen's bottom edge: drag the window up first.
        if (not (answer.get("covered_by") or "").startswith("EventWindow")
                and drag_window_up(game, window) and game.call(f"ui click {match['path']}").get("ok")):
            time.sleep(0.1)
            return True
    return False


def close_popups(game, limit=2000):
    """Presses OK on every open message popup and confirmation box, as a player would, first
    closing whatever covers them. Returns how many popups it closed."""
    first = None
    for _ in range(limit):
        matches = [m for selector in POPUP_BUTTONS
                   for m in game.call(f"ui find {selector} limit=1000").get("matches", [])
                   if m.get("visible") and m.get("enabled", True)
                   and (selector == "DefaultConfirm/AgreeButton"
                        or (m.get("text") or "").strip().lower() in DISMISS_TEXTS)]  # not a decision
        first = len(matches) if first is None else first
        if not matches:
            break
        path = matches[-1]["path"]
        answer = game.call(f"ui click {path}")
        if answer.get("ok"):
            time.sleep(0.05)
            continue
        if answer.get("error") == "covered" and close_covering(game, (answer.get("covered_by") or "")):
            continue
        # A tall popup's OK at the screen's bottom edge, under the menu bar: drag it up first.
        if (answer.get("error") in ("covered", "not_visible")
                and not (answer.get("covered_by") or "").startswith("EventWindow")
                and drag_window_up(game, path.split("/")[0])
                and game.call(f"ui click {path}").get("ok")):
            continue
        last_refusal = answer
        break  # covered by an event, or by something that will not move
    else:
        matches = []
    if matches:
        game.last_refusal = last_refusal  # for a caller that wants to know what blocked it
    return (first or 0) - len(matches)


def choose_events(game):
    """Picks the first enabled option of every open event that nothing covers, topmost first,
    as run_until's events=first would. Returns the choices made."""
    chosen = []
    for event in game.call("event list").get("events") or []:
        option = next((o for o in event.get("options", []) if o.get("enabled")), None)
        if option is None:
            continue
        command = f"event choose {option['number']} event=title^=\"{event['title'][:30]}\""
        answer = game.call(command)
        if not answer.get("ok") and answer.get("error") == "not_visible":
            # A tall event (long Anbennar texts at 1080p) has its options below the screen;
            # the window is moveable, so drag it up by its title and try again.
            drag_window_up(game, event["path"])
            answer = game.call(command)
        point = answer.get("point")
        if (answer.get("error") == "covered" and point
                and (answer.get("covered_by") or "").startswith(event["path"] + "/")):
            # Tongs can take the event's own background for a cover; the option is still on
            # top at its point, so click it there.
            game.call(f"click {point['x']} {point['y']}")
            time.sleep(1)
            if not any(e.get("title") == event["title"] for e in game.call("event list").get("events") or []):
                answer = {"ok": True}
        if answer.get("ok"):
            chosen.append(f"event {event.get('id')} option {option['number']}")
            time.sleep(0.5)
    return chosen


def dismiss_popups(game, rounds=20):
    """Clears what holds the game: events and popups cover each other, so it takes turns
    between them until neither moves. Returns what it did."""
    clicked = []
    for _ in range(rounds):
        chosen = choose_events(game)
        closed = close_popups(game)
        clicked += chosen + ([f"{closed} popups"] if closed else [])
        if not chosen and not closed:
            break
    return clicked


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--step", default="6m", help="game time per chunk: Nd, Nm or Ny (default 6m)")
    parser.add_argument("--until", help="stop at this game date (e.g. 1600.1.1)")
    parser.add_argument("--hours", type=float, help="stop after this many wall-clock hours")
    parser.add_argument("--chunks", type=int, help="stop after this many chunks")
    parser.add_argument("--speed", type=int, default=5, help="game speed 1-5 (default 5)")
    parser.add_argument("--events", default="first", choices=["first", "stop"],
                        help="first: pick the first option of every event (default); stop: end the run")
    parser.add_argument("--fps-seconds", type=float, default=3.0, help="seconds to count paused frames (0: skip)")
    parser.add_argument("--max-pause", type=float, default=20.0,
                        help="treat the game as stuck once it stays paused this long, and press Ok on "
                             "popups (default 20)")
    parser.add_argument("--out", default="runs/session", help="output path without extension (default runs/session)")
    parser.add_argument("--tongs", default=str(DEFAULT_TONGS), help="folder with the Tongs client tongs.py")
    args = parser.parse_args(argv)

    sys.path.insert(0, args.tongs)
    from tongs import Tongs  # noqa: E402  (the path comes from --tongs)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    csv_path, jsonl_path = out.with_suffix(".csv"), out.with_suffix(".jsonl")
    new_csv = not csv_path.exists()
    started = time.monotonic()

    with Tongs() as game, open(csv_path, "a", newline="") as csv_file, open(jsonl_path, "a") as jsonl:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        if new_csv:
            writer.writeheader()
        state = game.call("state", check=True)
        if not state.get("in_game"):
            print("no game loaded: load a save first", file=sys.stderr)
            return 1
        pid, process_start_ms = state["pid"], state["process_start_ms"]
        date = parse_date(state["date"])
        print(f"eu4.exe pid {pid}, at {state['date']}; logging to {csv_path}", flush=True)
        previous = process_counters(pid)
        previous_wall = time.monotonic()
        chunk = 0
        while True:
            if args.chunks is not None and chunk >= args.chunks:
                break
            if args.hours is not None and time.monotonic() - started >= args.hours * 3600:
                break
            target = add_step(date, args.step)
            if args.until and target > parse_date(args.until):
                break
            for attempt in range(4):
                run = game.call(f"run_until {fmt_date(target)} speed={args.speed} events={args.events} "
                                f"max_pause={args.max_pause:g} timeout=14400")
                if run.get("ok") and run.get("reached"):
                    break
                if "already at or past" in run.get("error", ""):
                    break  # the previous attempt got there while its event was being answered
                # Usually a popup holding the game paused: press its Ok and run on from where it stopped.
                # A window that just opened refuses clicks for a moment, so try twice.
                clicked = dismiss_popups(game)
                if not clicked:
                    time.sleep(2)
                    clicked = dismiss_popups(game)
                jsonl.write(json.dumps({"stalled": run, "clicked": clicked}) + "\n")
                jsonl.flush()
                print(f"run_until {fmt_date(target)} stalled ({run.get('error', 'not reached')}); "
                      f"clicked {clicked or 'nothing'}", file=sys.stderr, flush=True)
                if not clicked:
                    shot = game.call(f"screenshot stall_{fmt_date(target).replace('.', '_')}")
                    print(f"nothing to dismiss; screenshot {shot.get('path')}", file=sys.stderr)
                    return 3
            else:
                print(f"run_until {fmt_date(target)} kept stalling", file=sys.stderr)
                return 3
            if "already at or past" in run.get("error", "") or (run.get("reached") and not run.get("game_days")):
                # The game got there while an event was answered: no timing for this chunk.
                date = parse_date(game.call("state")["date"])
                chunk += 1
                continue
            # A player closes the message popups as they come; left open they pile up by the hundred.
            popups_closed = close_popups(game)
            fps, gap, frames = paused_fps(game, args.fps_seconds) if args.fps_seconds > 0 else (None, None, None)
            counters = process_counters(pid)
            now = time.monotonic()
            event_choices = len(run.get("events") or [])
            row = {
                "wall_time": datetime.datetime.now().isoformat(timespec="seconds"),
                "session_min": round((time.time() * 1000 - process_start_ms) / 60000, 1),
                "start_date": run.get("start_date"),
                "end_date": run.get("reached_date"),
                "game_days": run.get("game_days"),
                "wall_ms": run.get("wall_ms"),
                "days_per_s": run.get("days_per_second"),
                "paused_ms": run.get("paused_ms"),
                "inputs": run.get("inputs"),
                "event_choices": event_choices,
                "game_calls": run.get("game_calls"),
                # Clean: nobody else touched the game, and no pause beyond event choices.
                "clean": (run.get("inputs", 0) <= event_choices and run.get("game_calls", 0) == 0),
                "popups_closed": popups_closed,
                "fps_paused": fps,
                "longest_gap_ms": gap,
                **counters,
                "cpu_pct": round(100 * (counters["cpu_s"] - previous["cpu_s"]) / (now - previous_wall), 1),
            }
            writer.writerow(row)
            csv_file.flush()
            jsonl.write(json.dumps({"row": row, "run": run, "frames": frames}) + "\n")
            jsonl.flush()
            print(f"{row['end_date']:>10}  {row['days_per_s']:>7} days/s  {fps} fps  "
                  f"{row['private_mb']} MB private  {row['handles']} handles  "
                  f"session {row['session_min']} min", flush=True)
            date = parse_date(run["reached_date"])
            previous, previous_wall = counters, now
            chunk += 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
