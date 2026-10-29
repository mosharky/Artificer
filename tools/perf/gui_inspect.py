"""Reads EU4's interface objects from outside the process (no admin rights, no
Tongs): the GUI manager's widget registry by class and window name, the session's
pop-up list, and, with --refs, every place in memory that points at a widget.

Addresses are EU4 1.37.5 (Steam); see the Tongs design notes ("Widget tree",
"Events") and the Artificer findings.

    python gui_inspect.py                     # registry and pop-up list summary
    python gui_inspect.py --name DefaultPopup --refs 3
"""
import argparse
import collections
import ctypes
import struct
import sys

import numpy as np

from stack_sampler import MEMORY_BASIC_INFORMATION, Modules, k32, read

GUI_MANAGER = 0x23494F0   # global pointer to the GUI manager
ROOT = 0x233FE78          # global pointer to the game root; session at root+0x1E00
SESSION = 0x1E00
POPUPS = 0x78             # session: {head, tail, count} list of open pop-ups
WINDOW_VTABLES = {0x1D929D0: "windowType", 0x1D57510: "eu3dialogtype", 0x1DA4090: "containerWindowType"}
WINDOW_NAME = 0xA28       # std::string, the window's own name (else the definition's)
FULL_OBJECT = 0x30        # a window's full object starts this far before the widget pointer


class Game:
    def __init__(self, pid):
        self.process = k32.OpenProcess(0x0410, False, pid)
        if not self.process:
            raise OSError(f"OpenProcess({pid}) failed")
        self.base = Modules(pid).exe_base

    def bytes(self, address, size):
        return read(self.process, address, size) or b""

    def q(self, address):
        raw = self.bytes(address, 8)
        return struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0

    def u8(self, address):
        raw = self.bytes(address, 1)
        return raw[0] if raw else None

    def string(self, address):
        """An MSVC std::string: 16-byte buffer or pointer, size +0x10, capacity +0x18."""
        raw = self.bytes(address, 32)
        if len(raw) < 32:
            return None
        size, cap = struct.unpack_from("<QQ", raw, 16)
        if size > 256:
            return None
        text = raw[:size] if cap < 16 else self.bytes(struct.unpack_from("<Q", raw, 0)[0], size)
        return text.decode("cp1252", "replace")

    def rva(self, address):
        return address - self.base

    def registry(self):
        mgr = self.q(self.base + GUI_MANAGER)
        begin, end = self.q(mgr + 0x110), self.q(mgr + 0x118)
        raw = self.bytes(begin, end - begin)
        return mgr, list(struct.unpack(f"<{len(raw) // 8}Q", raw))

    def popups(self):
        root = self.q(self.base + ROOT)
        session = self.q(root + SESSION) if root else 0
        if not session:
            return session, [], 0
        head, tail, count = self.q(session + POPUPS), self.q(session + POPUPS + 8), self.q(session + POPUPS + 16)
        nodes, node = [], head
        while node and len(nodes) < 100000:
            nodes.append((node, self.q(node), self.u8(node + 0x18)))
            node = self.q(node + 0x10)
        return session, nodes, count

    def scan(self, values):
        """Addresses of every 8-aligned qword in committed private memory equal to one of values."""
        wanted = np.array(sorted(values), dtype=np.uint64)
        mbi = MEMORY_BASIC_INFORMATION()
        address, found = 0, collections.defaultdict(list)
        while k32.VirtualQueryEx(self.process, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if mbi.State == 0x1000 and mbi.Type in (0x20000, 0x1000000) and not mbi.Protect & 0x101:
                start, size = mbi.BaseAddress, mbi.RegionSize
                for off in range(0, size, 64 << 20):
                    chunk = self.bytes(start + off, min(64 << 20, size - off))
                    arr = np.frombuffer(chunk[:len(chunk) // 8 * 8], dtype=np.uint64)
                    for i in np.nonzero(np.isin(arr, wanted))[0]:
                        found[int(arr[i])].append(start + off + int(i) * 8)
            address = mbi.BaseAddress + mbi.RegionSize
            if address >= 1 << 47:
                break
        return found

    def owner_guess(self, address, window=0x800):
        """The nearest qword below address that looks like an eu4.exe vtable (in .rdata)."""
        raw = self.bytes(address - window, window + 8)
        for i in range(len(raw) // 8 - 1, -1, -1):
            value = struct.unpack_from("<Q", raw, i * 8)[0]
            if 0x1B66000 <= value - self.base < 0x1FB0000:
                return address - window + i * 8, self.rva(value)
        return None, None


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pid", type=int, help="eu4.exe process id (default: the running one)")
    parser.add_argument("--name", help="window name to look at in detail, e.g. DefaultPopup")
    parser.add_argument("--refs", type=int, default=0, help="scan memory for pointers to this many of them")
    args = parser.parse_args(argv)
    pid = args.pid
    if pid is None:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq eu4.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        pid = int(out.split(",")[1].strip('"'))
    game = Game(pid)
    mgr, widgets = game.registry()
    by_class, windows, selected = collections.Counter(), collections.Counter(), []
    for w in widgets:
        vt = game.rva(game.q(w))
        dead, visible = game.u8(w + 0xC4), game.u8(w + 0xC5)
        by_class[vt] += 1
        if vt in WINDOW_VTABLES:
            name = game.string(w + WINDOW_NAME) or game.string(game.q(w + 0xB0) + 0x10) or "?"
            windows[(name, WINDOW_VTABLES[vt], visible, dead)] += 1
            if name == args.name:
                selected.append(w)
    print(f"GUI manager {mgr:#x}: {len(widgets)} widgets in the registry")
    print("windows by name (name, class, visible, dead): count")
    for key, n in windows.most_common(15):
        print(f"  {n:6}  {key}")
    session, nodes, count = game.popups()
    print(f"session {session:#x}: pop-up list count {count}, walked {len(nodes)} nodes; "
          f"vtables {collections.Counter(hex(game.rva(game.q(p))) for _, p, _ in nodes).most_common(5)}")
    if args.name:
        print(f"{len(selected)} {args.name} windows; first: {[hex(w) for w in selected[:5]]}")
        full_vts = collections.Counter(hex(game.rva(game.q(w - FULL_OBJECT))) for w in selected)
        print(f"  full-object vtables {full_vts.most_common(3)}")
        targets = selected[:args.refs]
        if targets:
            values = set(targets) | {w - FULL_OBJECT for w in targets}
            found = game.scan(values)
            popup_ptrs = {p for _, p, _ in nodes}
            for w in targets:
                print(f"\nwidget {w:#x} (full object {w - FULL_OBJECT:#x}):")
                for value, label in ((w, "widget"), (w - FULL_OBJECT, "full")):
                    for at in found.get(value, []):
                        obj, vt = game.owner_guess(at)
                        where = "registry" if mgr and game.q(mgr + 0x110) <= at < game.q(mgr + 0x118) else ""
                        print(f"  {label} pointer at {at:#x} {where}; nearest vtable {vt and hex(vt)} at -{at - obj:#x}"
                              if obj else f"  {label} pointer at {at:#x} {where}")
            print(f"\n(pop-up list holds {len(popup_ptrs)} objects)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
