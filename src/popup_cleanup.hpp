#pragma once

#include <anvil/anvil.hpp>

namespace artificer {

// Hooks message pop-ups and deletes them a few seconds after they close
// (popup_cleanup.cpp). Call from Plugin::load; throws anvil::Error on failure.
void StartPopupCleanup(const anvil::Host& host);

}  // namespace artificer
