// Helpers the gen/ cases share (moved from checks/gen.cpp).
// Dissolved into fixtures / scoreboards in M2.
#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::gen {

// ===========================================================================
// 13. GenICam
// ===========================================================================
std::vector<uint8_t> fetchXmlFile(Context& c, std::string* url_out = nullptr);

// The XML member of a zipped GenICam file (§10.3.11: STORE or DEFLATE),
// found through the central directory, inflated and checked against its
// CRC-32 and size.  `error` is empty on success.
struct UnzippedXml {
    std::vector<uint8_t> data;
    std::string member;
    std::string error;
};
UnzippedXml unzipXml(const std::vector<uint8_t>& zip);

}  // namespace cxp::validation::checks::gen
