// CXP-CAM-CTRL-009.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl009(Context& c) {
    const std::string url = c.readString(c.rd32(Reg::XML_URL_ADDRESS), 64);
    auto loc = parseGenicamUrl(url);
    if (!loc) c.skip("the XML is not device-resident (URL '" + url + "')");
    const uint32_t base = uint32_t(loc->first), len = uint32_t(loc->second);
    std::vector<std::vector<uint8_t>> images;
    bool zipped = false;
    for (int64_t chunk64 : c.ilist("chunk_list")) {
        const uint32_t chunk = uint32_t(chunk64);
        std::vector<uint8_t> img;
        size_t refused = 0;
        while (img.size() < len) {
            c.checkpoint();
            uint32_t n = std::min<uint32_t>(chunk, len - uint32_t(img.size()));
            if (!zipped) n = (n + 3) / 4 * 4;  // plain XML: word-sized reads only
            auto a = c.readRaw(base + uint32_t(img.size()), n);
            if (!is(a, Ack::READ_OK)) {
                if (++refused == 1) c.expect(false, "chunk %u at +%zu: %s", chunk, img.size(), ackStr(a).c_str());
                break;
            }
            auto b = a->bytes();
            img.insert(img.end(), b.begin(), b.begin() + std::min<size_t>(n, len - img.size()));
            if (img.size() >= 2 && img[0] == 'P' && img[1] == 'K') zipped = true;
        }
        c.expect(img.size() == len && refused == 0, "XML read in %u-byte chunks: %zu of %u bytes", chunk, img.size(), len);
        images.push_back(std::move(img));
    }
    bool same = true;
    for (const auto& im : images) same &= im == images.front();
    c.expect(same, "all chunk sizes give a byte-identical XML image (%u bytes)", len);
    if (zipped && len % 4) {
        auto a = c.readRaw(base + (len & ~3u), len % 4);
        c.expect(is(a, Ack::READ_OK), "zipped XML: final %u-byte read: %s", len % 4, ackStr(a).c_str());
    }
}
CXP_CHECK("CXP-CAM-CTRL-009", ctrl009);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
