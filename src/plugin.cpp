// Artificer: fixes for EU4 performance in long play sessions.
#include <anvil/anvil.hpp>

#include "popup_cleanup.hpp"

namespace {

struct Artificer : anvil::Plugin {
    void load(anvil::Host& host) override {
        host.info("loaded on Anvil {}, game build {}", host.anvil_version(),
                  host.game().build_id ? host.game().build_id : "unknown");
        // A failing call throws: Anvil then rolls back whatever load() staged.
        artificer::StartPopupCleanup(host);
    }
};

}  // namespace

ANVIL_PLUGIN(Artificer)
