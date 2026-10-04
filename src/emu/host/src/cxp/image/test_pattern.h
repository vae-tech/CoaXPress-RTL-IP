// Test-pattern generator golden model: the pixel bytes of one TestPattern
// frame, as the reference virtual camera streams them and the RTL
// cxp_app_tpg mirrors them.  The validation checks compare a
// device's stream against it.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "cxp/image/pixel_formats.h"

namespace cxp {

// TestPattern selector values — lock-step with RTL cxp_app_tpg.
inline constexpr uint32_t TPG_GRADIENT = 0;  // diagonal ramp, scrolls one step per frame
inline constexpr uint32_t TPG_BARS = 1;      // 8 equal-width vertical bars
inline constexpr uint32_t TPG_FLAT = 2;      // uniform field, level steps 16 per frame
inline constexpr uint32_t TPG_GREYBARS = 3;  // 8 equal-width graduated grey bands

// The selector of a TestPattern enumeration entry name of the reference XML
// ("Gradient", "Bars", "Flat", "GreyBars"); nullopt for any other name.
std::optional<uint32_t> testPatternId(const std::string& name);
// The entry name of a selector ("Bars" for TPG_BARS), "" for an unknown one.
const char* testPatternName(uint32_t pattern);
// Every frame of the pattern is the same (Bars, GreyBars), so one rendered
// frame is the golden image of all of them.
bool testPatternStatic(uint32_t pattern);

// Render one TestPattern frame's pixel bytes (exposed for tests).
std::vector<uint8_t> renderTestPattern(PixelFormat fmt, uint32_t w, uint32_t h,
                                       uint32_t pattern, uint32_t frame_id);

}  // namespace cxp
