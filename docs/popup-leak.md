# Closed message pop-ups slow EU4 down

EU4 1.37.5 gets slower the longer the process runs, and only a restart helps. The
largest cause found so far is that closed message pop-ups are never deleted.
Artificer's `clean_closed_popups` setting deletes them (`src/popup_cleanup.cpp`).
Addresses are RVAs in the Steam `eu4.exe` for 1.37.5. The ones the plugin uses are in
`symbols.toml`, where Anvil checks them against the game before loading the plugin.

## What happens

- Each message pop-up (the `DefaultPopup` window of `interface/eu3dialog.gui`) is a
  controller of 0x5B0 bytes (vtable `0x1CC1DF8`). The factory `0x8C1430` builds it
  through the constructor `0x8C1F80`. The controller holds:
  - `+0x08`: its window's full object;
  - `+0x10`: the session;
  - `+0x114`: a "finished" byte, set by slot 5 (`0x10A08C0`) when the pop-up closes.
- When the pop-up closes, whatever showed it lets go of it, and the window is hidden.
  That is the session's open list (`session+0x78`), or the path through `0x123F800`.
  Nothing calls the controller's deleting destructor (slot 0, `0x8C22A0`). In memory,
  a closed controller is then referenced only from inside itself.
- The hidden windows and all their child widgets stay in the GUI manager's registry
  (`mgr+0x110`) and update list (`mgr+0x170`). The per-frame passes (`0x14E9A70` and
  `0x14E9FA0`) walk all of them. Loading another save keeps them; only a restart
  frees them.
- The message log keeps the controllers of its last entries (about 20) so they can
  be opened again. Those controllers are not marked finished, and the log's entries
  point to them.

## Measured before the fix

Same save (1819.1.1), same span, Tongs playing, 6-month chunks, no Bellows:

| | After 2.5 h and an in-process reload | Fresh restart |
| --- | --- | --- |
| Hidden `DefaultPopup` windows | 4 637 | 0 |
| Main thread per frame, paused | 4.95 ms | 1.98 ms |
| Cost per game day (sampled) | 177 ms | 140 ms |
| Private memory | 6.7 GB | 5.1 GB |

The tools that produced these figures are in `tools/perf`: `session_log.py`,
`stack_sampler.py`, `compare_profiles.py`, `paused_cost.py` and `gui_inspect.py`.

## The fix

1. A mid hook in the constructor (`popup_constructed`) notes each controller.
2. A hook on its destructor (`popup_destructor`) forgets any controller the game
   deletes itself.
3. About twice a second, after a frame on the main thread, a controller is deleted
   with its own deleting destructor once, for three checks in a row, it is:
   - finished,
   - not on the session's open list,
   - and its window is hidden.

   The destructor releases the pop-up's 3D view, takes the controller off the
   session's game-speed object (`0x1AA090`, which tolerates a controller that is not
   there) and destroys the window. The GUI manager then frees the window's widgets in
   its next sweep.
4. A controller left over from an earlier session cannot run its destructor, because
   that session is gone. Only its window is destroyed, through the window's full-object
   slot `0x130`, the same call the base destructor `0x10A05F0` makes.

In a 5-year test from 1654, the fix deleted 200 closed pop-ups. The 19 hidden pop-ups
left were exactly the 19 the message log keeps, none of them finished.

## Measured with the fix

The same run as above, repeated with the fix and with Bellows 0.14.0 loaded on both
sides: 2.3 hours of play from 1654 to 1819, then an in-process reload of the 1819 save,
against a fresh restart on it.

| | After 2.3 h and an in-process reload | Fresh restart |
| --- | --- | --- |
| Hidden `DefaultPopup` windows | 4 | 0 |
| Main thread per frame, paused | 1.48 ms | 2.37 ms |
| Cost per game day (sampled) | 137 ms | 132 ms |
| Private memory | 6.6 GB | 5.7 GB |

- During the session, the hidden pop-ups stayed between 4 and 12, and the paused cost
  between 1.8 and 3.5 ms per frame, with no upward trend. Without the fix, those
  figures were 4 637 pop-ups and 4.95 ms.
- The gap per game day fell from 26 % to 4 %. On the main thread, the job wait
  `0x179E90` grew by 4.7 ms per day after the long session, against 20 ms without
  the fix. The GUI passes (`0x14E9FA0`, `0x14E9E46`, `0x15931AC`) no longer grow at all.
- Private memory still grows by about 1 GB over the session, mostly in 16 MB heap
  segments (171 against 112). This growth does not show up as a cost per day.
