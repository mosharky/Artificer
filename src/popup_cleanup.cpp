// Deletes the message pop-ups (DefaultPopup) that EU4 keeps after they close.
//
// The game makes a controller for each message pop-up. When the pop-up closes,
// the controller marks itself finished and hides its window, and whatever showed
// it lets go of it, but nothing deletes it, not even when another save is loaded.
// The hidden windows stay in the GUI manager's widget registry and update list,
// so every frame walks every pop-up closed since the game started
// (docs/popup-leak.md). The message log keeps the controllers of its last entries
// so they can be opened again; those are not marked finished.
//
// The plugin notes each controller as it is constructed. Once one has been
// finished, off the session's list of open pop-ups and with its window hidden
// for a few checks in a row, it calls the controller's own deleting destructor:
// that releases the pop-up's 3D view, unregisters it from the game speed and
// destroys the window, which the GUI manager's sweep frees in the next frame. A
// controller left from an earlier session cannot run its destructor (it
// unregisters from that session, which is gone), so only its window is destroyed
// and the small controller stays.
#include "popup_cleanup.hpp"

#include <anvil/anvil.hpp>
#include <anvil/anvil_game.h>

#include "settings.gen.hpp"
#include "symbols.gen.hpp"

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace artificer {
namespace {

using Addr = std::uintptr_t;

constexpr std::uint64_t kFramesPerCheck = 30;  // about two checks a second
constexpr int kClosedChecks = 3;               // checks in a row closed before the delete
constexpr int kMaxOpenNodes = 10000;           // the open list holds a handful; bounds the walk
constexpr std::uint64_t kHeadBeforeTail = 8;   // the list is {head, tail, count}; not in symbols.toml

template <class T> T At(Addr address) { return *reinterpret_cast<const T*>(address); }

// Offsets from symbols.toml; the RVAs are made absolute once the plugin loads.
struct Layout {
    Addr root = 0;  // global: the game root, 0 without a game
    Addr popup_vtable = 0;
    std::uint64_t session = sym::session_of_root.session;                     // root: the session
    std::uint64_t open_head = sym::popup_list_append.tail - kHeadBeforeTail;  // session: open pop-ups
    std::uint64_t node_next = sym::popup_list_append.node_next;               // node: {popup, prev, next}
    std::uint64_t popup_session = sym::popup_base_destructor.session;         // controller: its session
    std::uint64_t popup_window = sym::popup_base_destructor.window;           // controller: its window's full object
    std::uint64_t popup_closed = sym::popup_close.closed;                     // controller: byte, finished
    std::uint64_t destroy_slot = sym::popup_base_destructor.destroy_slot;     // full object vtable: Destroy
    // window_destroy_thunk adds this signed byte to a widget to reach its full object
    std::ptrdiff_t widget_from_full = -static_cast<std::int8_t>(sym::window_destroy_thunk.full_object);
    std::uint64_t dead = sym::widget_dead.dead;
    std::uint64_t visible = sym::widget_visible.visible;
};

Layout g_layout;
anvil::HookOf<sym::popup_destructor> g_destructor;
decltype(sym::popup_delete)::Pointer g_delete = nullptr;

std::mutex g_mutex;
std::unordered_map<Addr, int> g_popups;  // live controller -> checks in a row it looked closed
std::uint64_t g_deleted = 0;             // controllers deleted with their windows
std::uint64_t g_windows_only = 0;        // windows destroyed of controllers from an earlier session

// The game (or the cleanup below) destroys a controller: forget it.
void OnPopupDestructor(void* popup) noexcept {
    {
        const std::lock_guard lock(g_mutex);
        g_popups.erase(reinterpret_cast<Addr>(popup));
    }
    g_destructor.next(popup);
}

void Track(Addr popup) {
    if (popup == 0 || At<Addr>(popup) != g_layout.popup_vtable) return;
    const std::lock_guard lock(g_mutex);
    g_popups.try_emplace(popup, 0);
}

// Main thread, after a frame.
void Check() {
    if (!cfg::clean_closed_popups.get()) return;
    const Layout& l = g_layout;
    const Addr root = At<Addr>(l.root);
    const Addr session = root ? At<Addr>(root + l.session) : 0;
    if (session == 0) return;  // no game running: nothing is deleted from the menu

    std::unordered_set<Addr> open;
    Addr node = At<Addr>(session + l.open_head);
    for (int i = 0; node != 0 && i < kMaxOpenNodes; ++i) {
        open.insert(At<Addr>(node));
        node = At<Addr>(node + l.node_next);
    }

    std::vector<Addr> current, earlier;
    {
        const std::lock_guard lock(g_mutex);
        for (auto& [popup, closed] : g_popups) {
            const Addr window = At<Addr>(popup + l.popup_window);
            const Addr widget = window + l.widget_from_full;
            if (At<std::uint8_t>(popup + l.popup_closed) == 0 || open.contains(popup) || window == 0 ||
                At<std::uint8_t>(widget + l.visible) != 0 || At<std::uint8_t>(widget + l.dead) != 0) {
                closed = 0;
                continue;
            }
            if (++closed < kClosedChecks) continue;
            (At<Addr>(popup + l.popup_session) == session ? current : earlier).push_back(popup);
        }
    }

    // Outside the lock: the destructor comes back through OnPopupDestructor.
    for (const Addr popup : current) g_delete(reinterpret_cast<void*>(popup), 1);
    for (const Addr popup : earlier) {
        const Addr window = At<Addr>(popup + l.popup_window);
        const auto destroy = reinterpret_cast<void (*)(Addr)>(At<Addr>(At<Addr>(window) + l.destroy_slot));
        destroy(window);
        const std::lock_guard lock(g_mutex);
        g_popups.erase(popup);  // the controller stays, as the game left it, without a live window
    }
    if (current.empty() && earlier.empty()) return;
    const std::lock_guard lock(g_mutex);
    const std::uint64_t before = g_deleted + g_windows_only;
    g_deleted += current.size();
    g_windows_only += earlier.size();
    // A line for the first deletes and then one per 100.
    if (before == 0 || before / 100 != (g_deleted + g_windows_only) / 100) {
        anvil::Host{}.info("deleted {} closed message pop-ups so far ({} of them from earlier sessions: window only)",
                           g_deleted + g_windows_only, g_windows_only);
    }
}

}  // namespace

void StartPopupCleanup(const anvil::Host& host) {
    const Addr base = host.game().image_base;
    g_layout.root = base + sym::root_and_date.root;
    g_layout.popup_vtable = base + sym::popup_destructor.vtable;
    g_delete = host.function(sym::popup_delete);

    host.hook(g_destructor, sym::popup_destructor, OnPopupDestructor);
    host.mid_hook(sym::popup_constructed, [](anvil::Cpu& cpu) { Track(cpu.rsi); }).release();
    host.subscribe(ANVIL_GAME_EVENT_FRAME, [](const void* payload) {
        const auto* frame = static_cast<const AnvilGameFrame*>(payload);
        if (frame == nullptr || frame->size < offsetof(AnvilGameFrame, number) + sizeof frame->number) return;
        if (frame->number % kFramesPerCheck == 0) Check();
    });
}

}  // namespace artificer
