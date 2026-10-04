// CXP-CAM-BOOT-003.  See cases/_common.h.

#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

void boot003(Context& c) {
    c.preserve(Reg::XML_MANIFEST_SELECTOR);
    const uint32_t size = c.rd32(Reg::XML_MANIFEST_SIZE);
    c.expect(size >= 1, "XmlManifestSize = %u (>= 1)", size);
    const uint32_t max_manifests = uint32_t(c.iparam("max_manifests"));
    if (size > max_manifests) c.warn("XmlManifestSize = %u looks like a byte count, not a number of manifests", size);
    const uint32_t n = std::min<uint32_t>(size, max_manifests);
    for (uint32_t s = 0; s < n; ++s) {
        auto w = c.writeRaw(Reg::XML_MANIFEST_SELECTOR, {s});
        c.expect(is(w, Ack::WRITE_OK), "XmlManifestSelector = %u: %s", s, ackStr(w).c_str());
        const uint32_t ver = c.rd32(Reg::XML_VERSION), sch = c.rd32(Reg::XML_SCHEMA_VERSION);
        const uint32_t url_at = c.rd32(Reg::XML_URL_ADDRESS);
        c.expect((ver >> 24) == 0, "[%u] XmlVersion = %s, bits 31:24 zero", s, hex(ver).c_str());
        c.expect((sch >> 24) == 0, "[%u] XmlSchemaVersion = %s, bits 31:24 zero", s, hex(sch).c_str());
        c.expect(url_at >= Reg::MANUFACTURER_SPACE && url_at % 4 == 0, "[%u] XmlUrlAddress = %s (>= 0x6000, aligned)", s,
                 hex(url_at).c_str());
        std::string url;
        bool nul = false;
        const uint32_t max_url = uint32_t(c.iparam("max_url_bytes"));
        for (uint32_t off = 0; off < max_url && !nul; off += 4) {
            auto a = c.readRaw(url_at + off, 4);
            if (!is(a, Ack::READ_OK)) {
                c.expect(false, "[%u] URL read at +%u: %s", s, off, ackStr(a).c_str());
                break;
            }
            for (uint8_t b : a->bytes()) {
                if (b == 0) {
                    nul = true;
                    break;
                }
                url += char(b);
            }
        }
        c.expect(nul, "[%u] URL string is NUL-terminated within %u bytes", s, max_url);
        c.expect(urlGrammarOk(url), "[%u] URL '%s' parses (Local:name;addr;len | Web: | File:)", s, url.c_str());
    }
    const uint32_t sel_before = n ? n - 1 : 0;
    auto w = c.writeRaw(Reg::XML_MANIFEST_SELECTOR, {size});
    const uint32_t sel_after = c.rd32(Reg::XML_MANIFEST_SELECTOR);
    c.expect(w && w->code != Ack::WRITE_OK && sel_after == sel_before,
             "out-of-range XmlManifestSelector = %u: %s, selector now %u (must stay %u)", size,
             ackStr(w).c_str(), sel_after, sel_before);
}
CXP_CHECK("CXP-CAM-BOOT-003", boot003);

}  // namespace

}  // namespace cxp::validation::checks::boot
