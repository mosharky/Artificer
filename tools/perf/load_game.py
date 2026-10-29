"""Starts EU4 (unless it runs) and continues the last game, through Tongs.

Waits for the main menu, clicks Continue Game, presses Ok on confirmation boxes
(such as Anbennar's achievement notice) and returns once the game is loaded
and paused. With --save and a game already loaded, it loads that save through the
in-game menu instead, in the same process.

    python load_game.py
    python load_game.py --save artificer_perf_1819
"""
import argparse
import pathlib
import subprocess
import sys
import time

from session_log import DEFAULT_TONGS, dismiss_popups

EU4_DIR = pathlib.Path(r"C:\Steam\steamapps\common\Europa Universalis IV")
CONTINUE = "country_selection_panel/mainmenu/mainmenu_panel_bottom/continue_container/continue_game_button"
ENTRY = "menu_load/games/ingamesavegameentry"


def menu_open(game):
    found = game.call("ui find menu/menu_panel/menu_close_button").get("matches")  # none while a save loads
    return bool(found and found[0].get("visible"))


def load_in_game(game, title, timeout_s):
    """Loads the save named `title` through the in-game menu (Esc, Load Game)."""
    for _ in range(3):  # the first Escape may only close something else
        if menu_open(game):
            break
        game.call("key escape")
        time.sleep(1)
    game.call("ui click menu/menu_panel/menu_load_button", check=True)
    time.sleep(1.5)
    titles = game.call(f"ui find {ENTRY}/save_game_title limit=200")["matches"]
    entry = next((m["path"].rsplit("/", 1)[0] for m in titles if m.get("text") == title), None)
    if entry is None:
        raise RuntimeError(f"no save named {title} in the load menu")
    game.call(f"ui click {entry}/save_game_title")
    time.sleep(0.5)
    game.call("ui click menu_load/load", check=True)
    time.sleep(1.5)
    confirm = game.call("ui find DefaultConfirm/Description")["matches"]
    if not confirm or title not in confirm[0].get("text", ""):
        raise RuntimeError(f"unexpected confirmation: {confirm}")
    date = game.call("state").get("date")
    game.call("ui click DefaultConfirm/AgreeButton", check=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(2)
        state = game.call("state")
        # the date jumps to the save's date once the load is done
        if state.get("in_game") and state.get("date") != date:
            break
    # The menu stays open after a load (and can open again a moment later).
    for _ in range(5):
        time.sleep(2)
        if menu_open(game):
            game.call("ui click menu/menu_panel/menu_close_button")


def connect(Tongs, timeout_s):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return Tongs()
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eu4-dir", default=str(EU4_DIR))
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for each stage (default 900)")
    parser.add_argument("--tongs", default=str(DEFAULT_TONGS), help="folder with the Tongs client tongs.py")
    parser.add_argument("--save", help="with a game loaded: load this save through the in-game menu")
    args = parser.parse_args(argv)
    sys.path.insert(0, args.tongs)
    from tongs import Tongs  # noqa: E402  (the path comes from --tongs)

    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq eu4.exe"], capture_output=True, text=True)
    if "eu4.exe" not in running.stdout:
        eu4 = pathlib.Path(args.eu4_dir)
        subprocess.Popen([str(eu4 / "eu4.exe")], cwd=eu4)
        print("started eu4.exe", flush=True)

    with connect(Tongs, args.timeout) as game:
        if args.save and game.call("state").get("in_game"):
            load_in_game(game, args.save, args.timeout)
        deadline = time.monotonic() + args.timeout
        while not game.call("state").get("in_game"):
            if time.monotonic() >= deadline:
                print("the game did not load in time", file=sys.stderr)
                return 1
            dismiss_popups(game)
            if game.call(f"ui find {CONTINUE}").get("count") == 1:
                game.call(f"ui click {CONTINUE}")
                print("clicked Continue Game", flush=True)
            time.sleep(5)
        time.sleep(5)  # the load's own popups come a moment after the game is in
        clicked = dismiss_popups(game)
        state = game.call("state")
        print(f"loaded at {state.get('date')}, paused {state.get('paused')}; dismissed {clicked or 'nothing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
