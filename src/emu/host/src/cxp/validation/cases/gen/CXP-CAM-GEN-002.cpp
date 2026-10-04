// CXP-CAM-GEN-002.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen002(Context& c) {
    auto xml = fetchXmlFile(c);
    std::unique_ptr<NodeTree> tree;
    try {
        tree = NodeTree::fromString(xml);
    } catch (const std::exception& e) {
        c.expect(false, "XML loads: %s", e.what());
        return;
    }
    c.expect(true, "XML loads into the host's node map (%zu features)", tree->features().size());
    const std::pair<const char*, FeatureKind> sfnc[] = {
        {"Width", FeatureKind::Integer}, {"Height", FeatureKind::Integer}, {"OffsetX", FeatureKind::Integer},
        {"OffsetY", FeatureKind::Integer}, {"PixelFormat", FeatureKind::Enumeration},
        {"AcquisitionMode", FeatureKind::Enumeration}, {"AcquisitionStart", FeatureKind::Command},
        {"AcquisitionStop", FeatureKind::Command}, {"DeviceVendorName", FeatureKind::String},
        {"DeviceModelName", FeatureKind::String}, {"DeviceTapGeometry", FeatureKind::Enumeration},
        {"Image1StreamID", FeatureKind::Integer}, {"TestPattern", FeatureKind::Enumeration},
        {"ExposureTime", FeatureKind::Float}, {"AcquisitionFrameRate", FeatureKind::Float},
        {"TriggerMode", FeatureKind::Enumeration}};
    for (const auto& [name, kind] : sfnc) {
        if (Feature* f = tree->find(name)) {
            c.expect(f->kind == kind, "SFNC %s is I%s (declared %s)", name, featureKindName(kind), featureKindName(f->kind));
        }
    }
    if (Feature* pf = tree->find("PixelFormat")) {
        for (const auto& e : pf->enum_entries) {
            c.expect(pfncTable25(e.first).has_value(), "PixelFormat entry '%s' is a PFNC name", e.first.c_str());
        }
    }
    for (const auto& [vendor, std_name] : {std::pair<const char*, const char*>{"TapGeometry", "DeviceTapGeometry"},
                                           {"StreamId", "Image1StreamID"}, {"StreamPacketSize", "StreamPacketSizeMax"}}) {
        if (tree->find(vendor) && !tree->find(std_name)) {
            c.warn("'%s' duplicates the semantics of SFNC/CXP '%s'", vendor, std_name);
        }
    }
    c.note("loaded with this host's GenApi subset, not the reference GenApi implementation");
}
CXP_CHECK("CXP-CAM-GEN-002", gen002);

}  // namespace

}  // namespace cxp::validation::checks::gen
