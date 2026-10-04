#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::data {

std::vector<uint32_t> tagsOfRun(Context& c, size_t n_images);

// A small test-pattern stream: the bench's generator selected (USE_TPG 1,
// TPG_RUN 0; the acquisition decides which images go) when the bench has a
// pixel port, Width x Height and StreamPacketSizeMax = spsm, all restored
// on exit.  The acquisition is not started.
void smallTpg(Context& c, uint32_t width, uint32_t height, uint32_t spsm);

// Host time of the first control acknowledgment in cap that arrived at or
// after t_ms, or -1.  A case that sends one command at a time while
// recording finds its command's acknowledgment this way: the stream packets
// after it went on the wire after the acknowledgment.
double ackTime(const std::vector<Captured>& cap, double t_ms);

}  // namespace cxp::validation::checks::data
