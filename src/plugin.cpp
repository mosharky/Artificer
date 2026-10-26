// A first Anvil plugin: a function hook, a mid hook and a setting, written with
// the C++ wrapper (anvil.hpp) and the headers `anvil gen` writes from
// symbols.toml and settings.toml.
#include <anvil/anvil.hpp>

#include "settings.gen.hpp"
#include "symbols.gen.hpp"

#include <atomic>

namespace {

anvil::HookOf<sym::speed_object> g_speed;  // the hook and the rest of its chain
std::atomic<bool> g_first{true};

// Must match the signature in symbols.toml, or the plugin does not compile.
void* on_speed_object(void* session) noexcept {
    void* speed = g_speed.next(session);
    if (g_first.exchange(false)) anvil::Host{}.info("first game-speed object: {}", speed);
    return speed;
}

struct MyPlugin : anvil::Plugin {
    anvil::Change menu_return;
    int returns = 0;

    void load(anvil::Host& host) override {
        host.info("loaded on Anvil {}, game build {}", host.anvil_version(),
                  host.game().build_id ? host.game().build_id : "unknown");
        // A failing call throws: Anvil then rolls back whatever load() staged.
        host.hook(g_speed, sym::speed_object, on_speed_object);
        // Any callable; the Host is a value, so capturing it is safe.
        menu_return = host.mid_hook(sym::idle_exit, [host, this](anvil::Cpu& cpu) {
            ++returns;
            // restart = false: read it each time, so a change applies at once.
            if (cfg::log_menu_returns.get()) host.info("returning to the menu (rcx={:#x}, {} so far)", cpu.rcx, returns);
        });
    }
};

}  // namespace

ANVIL_PLUGIN(MyPlugin)
