// Helpers the boot/ cases share (moved from checks/boot.cpp).
// Dissolved into fixtures / scoreboards in M2.
#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::boot {

bool urlGrammarOk(const std::string& url);

void checkString(Context& c, const char* name, uint32_t addr, uint32_t len, bool required);

struct UseCase { const char* reg; uint32_t addr; const char* sfnc; const char* alias; bool readable; };

const UseCase kUseCases[] = {
    {"WidthAddress", Reg::WIDTH_ADDRESS, "Width", "Width", true},
    {"HeightAddress", Reg::HEIGHT_ADDRESS, "Height", "Height", true},
    {"AcquisitionModeAddress", Reg::ACQ_MODE_ADDRESS, "AcquisitionMode", "AcquisitionMode", true},
    {"AcquisitionStartAddress", Reg::ACQ_START_ADDRESS, "AcquisitionStart", "AcquisitionStart", false},
    {"AcquisitionStopAddress", Reg::ACQ_STOP_ADDRESS, "AcquisitionStop", "AcquisitionStop", false},
    {"PixelFormatAddress", Reg::PIXEL_FORMAT_ADDRESS, "PixelFormat", "PixelFormat", true},
    {"DeviceTapGeometryAddress", Reg::TAP_GEOMETRY_ADDRESS, "DeviceTapGeometry", "TapGeometry", true},
    {"Image1StreamIDAddress", Reg::IMAGE1_STREAM_ID_ADDRESS, "Image1StreamID", "StreamId", true},
};

}  // namespace cxp::validation::checks::boot
