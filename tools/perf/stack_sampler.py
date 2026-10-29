"""Samples the call stacks of EU4's busy threads, without admin rights.

Every 1/hz seconds each thread that used CPU recently is suspended just long enough
to copy its registers and stack, then resumed; dbghelp walks the copy. Samples are
taken on wall-clock time, so waits show up too (as the wait function and its
callers). Only time while the game runs (unpaused) counts, and the game dates at the
start and end give the cost per game day.

Frames in eu4.exe are named by the start of their function (from the exe's .pdata),
frames elsewhere by dbghelp (exports) or module+offset. The output JSON holds per
thread: samples, CPU seconds, and leaf ("self") and inclusive counts per function,
plus folded stacks for a flame graph.

    python stack_sampler.py --seconds 60 --out runs/prof-fresh
"""
import argparse
import bisect
import collections
import ctypes
import json
import pathlib
import struct
import sys
import threading
import time
from ctypes import wintypes

from session_log import DEFAULT_TONGS, parse_date

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)

PROCESS_ALL_READ = 0x0400 | 0x0010  # QUERY_INFORMATION | VM_READ
THREAD_ACCESS = 0x0002 | 0x0008 | 0x0040  # SUSPEND_RESUME | GET_CONTEXT | QUERY_INFORMATION
CONTEXT_FULL = 0x10000B
CONTEXT_SIZE = 0x4D0
MACHINE_AMD64 = 0x8664
TH32CS_SNAPTHREAD, TH32CS_SNAPMODULE = 0x4, 0x8


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]


class ADDRESS64(ctypes.Structure):
    _fields_ = [("Offset", ctypes.c_uint64), ("Segment", ctypes.c_uint16), ("Mode", ctypes.c_int)]


class STACKFRAME64(ctypes.Structure):
    _fields_ = [("AddrPC", ADDRESS64), ("AddrReturn", ADDRESS64), ("AddrFrame", ADDRESS64),
                ("AddrStack", ADDRESS64), ("AddrBStore", ADDRESS64), ("FuncTableEntry", ctypes.c_void_p),
                ("Params", ctypes.c_uint64 * 4), ("Far", wintypes.BOOL), ("Virtual", wintypes.BOOL),
                ("Reserved", ctypes.c_uint64 * 3), ("KdHelp", ctypes.c_byte * 512)]


class SYMBOL_INFO(ctypes.Structure):
    _fields_ = [("SizeOfStruct", wintypes.ULONG), ("TypeIndex", wintypes.ULONG), ("Reserved", ctypes.c_uint64 * 2),
                ("Index", wintypes.ULONG), ("Size", wintypes.ULONG), ("ModBase", ctypes.c_uint64),
                ("Flags", wintypes.ULONG), ("Value", ctypes.c_uint64), ("Address", ctypes.c_uint64),
                ("Register", wintypes.ULONG), ("Scope", wintypes.ULONG), ("Tag", wintypes.ULONG),
                ("NameLen", wintypes.ULONG), ("MaxNameLen", wintypes.ULONG), ("Name", ctypes.c_char * 256)]


class THREAD_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("ExitStatus", ctypes.c_long), ("TebBaseAddress", ctypes.c_void_p),
                ("UniqueProcess", ctypes.c_void_p), ("UniqueThread", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_size_t), ("Priority", ctypes.c_long), ("BasePriority", ctypes.c_long)]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("_pad1", wintypes.WORD),
                ("RegionSize", ctypes.c_uint64), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD), ("_pad2", wintypes.DWORD)]


READ_MEMORY = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, ctypes.c_uint64, ctypes.c_void_p,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
dbghelp.StackWalk64.argtypes = [wintypes.DWORD, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(STACKFRAME64),
                                ctypes.c_void_p, READ_MEMORY, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
dbghelp.SymInitializeW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.BOOL]
dbghelp.SymFromAddr.argtypes = [wintypes.HANDLE, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64),
                                ctypes.POINTER(SYMBOL_INFO)]
dbghelp.SymCleanup.argtypes = [wintypes.HANDLE]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenThread.restype = wintypes.HANDLE
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
k32.SuspendThread.argtypes = [wintypes.HANDLE]
k32.ResumeThread.argtypes = [wintypes.HANDLE]
k32.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_uint64)] * 4
k32.GetThreadDescription.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                               ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.CloseHandle.argtypes = [wintypes.HANDLE]
ntdll.NtQueryInformationThread.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                           ctypes.POINTER(wintypes.ULONG)]
FUNCTION_TABLE_ACCESS = ctypes.cast(dbghelp.SymFunctionTableAccess64, ctypes.c_void_p)
GET_MODULE_BASE = ctypes.cast(dbghelp.SymGetModuleBase64, ctypes.c_void_p)


MONTH_STARTS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def day_number(text):
    """Days since year 0 for an EU4 date (365-day years)."""
    year, month, day = parse_date(text)
    return year * 365 + MONTH_STARTS[month - 1] + day


def read(process, address, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(process, ctypes.c_void_p(address), buf, size, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


def pdata_starts(path):
    """Start RVAs of the functions in a PE file's exception directory, sorted."""
    data = pathlib.Path(path).read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    sections = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    rva, size = struct.unpack_from("<II", data, opt + 112 + 3 * 8)  # data directory 3: exceptions
    table = opt + opt_size
    for i in range(sections):
        _, vsize, vaddr, rawsize, rawptr = struct.unpack_from("<8sIIII", data, table + i * 40)
        if vaddr <= rva < vaddr + max(vsize, rawsize):
            off = rawptr + rva - vaddr
            return sorted({struct.unpack_from("<I", data, off + j)[0] for j in range(0, size, 12)})
    return []


class Modules:
    def __init__(self, pid):
        self.ranges = []  # (base, end, name, path)
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
        entry = MODULEENTRY32W(dwSize=ctypes.sizeof(MODULEENTRY32W))
        ok = k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            base = entry.modBaseAddr or 0
            self.ranges.append((base, base + entry.modBaseSize, entry.szModule.lower(), entry.szExePath))
            ok = k32.Module32NextW(snap, ctypes.byref(entry))
        k32.CloseHandle(snap)
        self.ranges.sort()
        self.bases = [r[0] for r in self.ranges]
        exe = next(r for r in self.ranges if r[2] == "eu4.exe")
        self.exe_base, self.exe_end = exe[0], exe[1]
        self.exe_funcs = pdata_starts(exe[3])

    def find(self, address):
        i = bisect.bisect_right(self.bases, address) - 1
        if i >= 0 and address < self.ranges[i][1]:
            return self.ranges[i]
        return None


class Namer:
    def __init__(self, process, modules):
        self.process, self.modules, self.cache = process, modules, {}
        self.symbol = SYMBOL_INFO(SizeOfStruct=88, MaxNameLen=255)  # sizeof(SYMBOL_INFO) with Name[1]

    def __call__(self, address):
        name = self.cache.get(address)
        if name is None:
            name = self.cache[address] = self._name(address)
        return name

    def _name(self, address):
        m = self.modules
        if m.exe_base <= address < m.exe_end:
            rva = address - m.exe_base
            i = bisect.bisect_right(m.exe_funcs, rva) - 1
            return f"eu4!{m.exe_funcs[i]:#x}" if i >= 0 else f"eu4+{rva:#x}"
        disp = ctypes.c_uint64(0)
        if dbghelp.SymFromAddr(self.process, address, ctypes.byref(disp), ctypes.byref(self.symbol)):
            module = m.find(address)
            prefix = module[2].rsplit(".", 1)[0] if module else "?"
            return f"{prefix}!{self.symbol.Name.decode(errors='replace')}"
        module = m.find(address)
        if module:
            return f"{module[2].rsplit('.', 1)[0]}+{(address - module[0]) & ~0xFFF:#x}"
        return "?"


class Thread:
    def __init__(self, tid, handle, stack_base):
        self.tid, self.handle, self.stack_base = tid, handle, stack_base
        self.name = ""
        desc = ctypes.c_wchar_p()
        if k32.GetThreadDescription(handle, ctypes.byref(desc)) >= 0 and desc.value:
            self.name = desc.value
            k32.LocalFree(desc)
        self.last_cpu = self.cpu()
        self.cpu_start = self.last_cpu
        self.active = False

    def cpu(self):
        times = [ctypes.c_uint64() for _ in range(4)]
        k32.GetThreadTimes(self.handle, *[ctypes.byref(t) for t in times])
        return (times[2].value + times[3].value) / 1e7


def list_threads(process, pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    entry = THREADENTRY32(dwSize=ctypes.sizeof(THREADENTRY32))
    tids = []
    ok = k32.Thread32First(snap, ctypes.byref(entry))
    while ok:
        if entry.th32OwnerProcessID == pid:
            tids.append(entry.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    threads = {}
    for tid in tids:
        handle = k32.OpenThread(THREAD_ACCESS, False, tid)
        if not handle:
            continue
        info = THREAD_BASIC_INFORMATION()
        ntdll.NtQueryInformationThread(handle, 0, ctypes.byref(info), ctypes.sizeof(info), None)
        teb = read(process, info.TebBaseAddress, 16)
        stack_base = struct.unpack_from("<Q", teb, 8)[0] if teb else 0
        threads[tid] = Thread(tid, handle, stack_base)
    return threads


class Sampler:
    def __init__(self, pid, max_depth=128):
        self.pid, self.max_depth = pid, max_depth
        self.process = k32.OpenProcess(PROCESS_ALL_READ, False, pid)
        if not self.process:
            raise OSError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        dbghelp.SymSetOptions(0x2 | 0x4 | 0x200 | 0x80000)  # UNDNAME, DEFERRED_LOADS, FAIL_CRITICAL, NO_PROMPTS
        if not dbghelp.SymInitializeW(self.process, str(pathlib.Path(__file__).parent), True):
            raise OSError(f"SymInitialize failed: {ctypes.get_last_error()}")
        self.modules = Modules(pid)
        self.name = Namer(self.process, self.modules)
        self.context = ctypes.create_string_buffer(CONTEXT_SIZE + 16)
        self.ctx_addr = (ctypes.addressof(self.context) + 15) & ~15
        self.stack = b""
        self.stack_lo = 0
        self.reader = READ_MEMORY(self._read)

    def _read(self, _process, address, buffer, size, done):
        off = address - self.stack_lo
        if 0 <= off and off + size <= len(self.stack):
            ctypes.memmove(buffer, self.stack[off:off + size], size)
            done[0] = size
            return True
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(self.process, ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
        done[0] = got.value
        return bool(ok)

    def sample(self, thread):
        """The thread's stack as a list of names, leaf first; None if it could not be read."""
        struct.pack_into("<I", (ctypes.c_char * CONTEXT_SIZE).from_address(self.ctx_addr), 0x30, CONTEXT_FULL)
        if k32.SuspendThread(thread.handle) == 0xFFFFFFFF:
            return None
        try:
            if not k32.GetThreadContext(thread.handle, self.ctx_addr):
                return None
            raw = ctypes.string_at(self.ctx_addr, CONTEXT_SIZE)
            rsp, rip = struct.unpack_from("<Q", raw, 0x98)[0], struct.unpack_from("<Q", raw, 0xF8)[0]
            self.last_rip = rip
            size = min(max(thread.stack_base - rsp, 0), 1 << 20)
            self.stack, self.stack_lo = (read(self.process, rsp, size) or b""), rsp
        finally:
            k32.ResumeThread(thread.handle)
        frame = STACKFRAME64()
        frame.AddrPC.Offset, frame.AddrPC.Mode = rip, 3
        frame.AddrStack.Offset, frame.AddrStack.Mode = rsp, 3
        frame.AddrFrame.Offset, frame.AddrFrame.Mode = struct.unpack_from("<Q", raw, 0xA0)[0], 3
        names = []
        while len(names) < self.max_depth:
            if not dbghelp.StackWalk64(MACHINE_AMD64, self.process, thread.handle, ctypes.byref(frame),
                                       self.ctx_addr, self.reader, FUNCTION_TABLE_ACCESS, GET_MODULE_BASE, None):
                break
            pc = frame.AddrPC.Offset
            if not pc:
                break
            # return addresses point after the call; step back so they name the calling function
            names.append(self.name(pc if not names else pc - 1))
        return names

    def memory(self):
        """Committed memory by type, and private allocations by size class."""
        mbi = MEMORY_BASIC_INFORMATION()
        address, by_type, allocs = 0, collections.Counter(), collections.Counter()
        while k32.VirtualQueryEx(self.process, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if mbi.State == 0x1000:  # MEM_COMMIT
                kind = {0x20000: "private", 0x40000: "mapped", 0x1000000: "image"}.get(mbi.Type, hex(mbi.Type))
                by_type[kind] += mbi.RegionSize
                if kind == "private":
                    allocs[mbi.AllocationBase] += mbi.RegionSize
            address = mbi.BaseAddress + mbi.RegionSize
            if address >= 1 << 47:
                break
        classes = collections.defaultdict(lambda: [0, 0])
        for size in allocs.values():
            cls = 1 << max(size - 1, 1).bit_length()
            classes[cls][0] += 1
            classes[cls][1] += size
        return {"committed_mb": {k: round(v / 2**20, 1) for k, v in by_type.items()},
                "private_allocations": len(allocs),
                "private_by_size": {f"<={k // 1024}KB": {"count": c, "mb": round(s / 2**20, 1)}
                                    for k, (c, s) in sorted(classes.items())}}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=float, default=60, help="unpaused seconds to sample (default 60)")
    parser.add_argument("--hz", type=float, default=50, help="samples per second of the main thread (default 50)")
    parser.add_argument("--worker-every", type=int, default=10,
                        help="sample the other busy threads every Nth round (default 10)")
    parser.add_argument("--busy", type=float, default=0.05, help="CPU share that makes a thread busy (default 0.05)")
    parser.add_argument("--wait", type=float, default=600, help="seconds to wait for the game to run (default 600)")
    parser.add_argument("--out", required=True, help="output path without extension")
    parser.add_argument("--tongs", default=str(DEFAULT_TONGS))
    args = parser.parse_args(argv)
    sys.path.insert(0, args.tongs)
    from tongs import Tongs  # noqa: E402

    game = Tongs()
    state = game.call("state")
    main_tid = game.call("window").get("thread_id")
    sampler = Sampler(state["pid"])
    threads = list_threads(sampler.process, state["pid"])
    memory_before = sampler.memory()

    # The game state is read on a thread of its own so a slow answer never delays sampling.
    live = {"paused": True, "date": state.get("date")}
    stop = threading.Event()

    def watch():
        with Tongs() as watcher:
            while not stop.is_set():
                s = watcher.call("state")
                live["paused"], live["date"] = s.get("paused", True), s.get("date")
                time.sleep(0.25)

    threading.Thread(target=watch, daemon=True).start()
    deadline = time.monotonic() + args.wait
    while live["paused"]:
        if time.monotonic() > deadline:
            print("the game never ran", file=sys.stderr)
            return 1
        time.sleep(0.05)

    stats = collections.defaultdict(lambda: {"samples": 0, "self": collections.Counter(), "rip": collections.Counter(),
                                              "incl": collections.Counter(), "folded": collections.Counter()})
    start_date, running, period = live["date"], 0.0, 1 / args.hz
    walk_s, rounds = 0.0, 0
    last_busy_check = time.monotonic()
    for t in threads.values():
        t.active = True  # the first second samples every thread, then only the busy ones
    tick = previous = time.monotonic()
    while running < args.seconds:
        now = time.monotonic()
        if not live["paused"]:
            running += now - previous
        previous = now
        if now - last_busy_check >= 1.0:
            for t in threads.values():
                cpu = t.cpu()
                t.active = (cpu - t.last_cpu) / (now - last_busy_check) >= args.busy or t.tid == main_tid
                t.last_cpu = cpu
            last_busy_check = now
        if not live["paused"]:
            rounds += 1
            begin = time.perf_counter()
            for t in threads.values():
                if not t.active or (t.tid != main_tid and rounds % args.worker_every):
                    continue
                names = sampler.sample(t)
                if not names:
                    continue
                key = "main" if t.tid == main_tid else (t.name or f"tid{t.tid}")
                s = stats[key]
                s["samples"] += 1
                s["self"][names[0]] += 1
                if sampler.modules.exe_base <= sampler.last_rip < sampler.modules.exe_end:
                    s["rip"][f"{sampler.last_rip - sampler.modules.exe_base:#x}"] += 1
                for n in set(names):
                    s["incl"][n] += 1
                s["folded"][";".join(reversed(names))] += 1
            walk_s += time.perf_counter() - begin
        tick += period
        delay = tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            tick = time.monotonic()
    end_date = live["date"]
    stop.set()
    days = day_number(end_date) - day_number(start_date)
    result = {
        "pid": state["pid"], "start_date": start_date, "end_date": end_date, "game_days": days,
        "running_s": round(running, 2), "ms_per_day": round(running * 1000 / days, 2) if days else None,
        "hz": args.hz, "worker_every": args.worker_every, "rounds": rounds, "sampler_busy_s": round(walk_s, 2),
        "thread_cpu_s": {("main" if t.tid == main_tid else (t.name or f"tid{t.tid}")): round(t.cpu() - t.cpu_start, 2)
                         for t in threads.values() if t.cpu() - t.cpu_start > 0.1},
        "memory_before": memory_before, "memory_after": sampler.memory(),
        "threads": {k: {"samples": v["samples"], "self": v["self"].most_common(300), "rip": v["rip"].most_common(300),
                        "incl": v["incl"].most_common(500)} for k, v in stats.items()},
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(result, indent=1))
    with open(out.with_suffix(".folded"), "w") as f:
        for key, v in stats.items():
            for stack, n in v["folded"].items():
                f.write(f"{key};{stack} {n}\n")
    print(f"{start_date} -> {end_date}: {days} days in {running:.1f} s = {result['ms_per_day']} ms/day; "
          f"{rounds} rounds, sampler busy {walk_s:.1f} s; wrote {out.with_suffix('.json')}")
    for key, v in sorted(stats.items(), key=lambda kv: -kv[1]["samples"])[:6]:
        top = ", ".join(f"{n} {c * 100 // v['samples']}%" for n, c in v["self"].most_common(4))
        print(f"  {key}: {v['samples']} samples; self {top}")
    dbghelp.SymCleanup(sampler.process)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
