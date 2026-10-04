#include "cxp/validation/cases/gen/_helpers.h"

#include <zlib.h>

namespace cxp::validation::checks::gen {

std::vector<uint8_t> fetchXmlFile(Context& c, std::string* url_out) {
    c.wr32(Reg::XML_MANIFEST_SELECTOR, 0);
    const std::string url = c.readString(c.rd32(Reg::XML_URL_ADDRESS), 64);
    if (url_out) *url_out = url;
    auto loc = parseGenicamUrl(url);
    if (!loc) c.skip("the XML is not device-resident (URL '" + url + "')");
    const uint32_t len = uint32_t(loc->second);
    auto data = c.readBlock(uint32_t(loc->first), (len + 3) / 4 * 4, 256);
    data.resize(len);
    return data;
}

UnzippedXml unzipXml(const std::vector<uint8_t>& zip) {
    UnzippedXml r;
    const size_t n = zip.size();
    auto u16 = [&](size_t at) { return uint32_t(zip[at] | zip[at + 1] << 8); };
    auto u32 = [&](size_t at) { return u16(at) | u16(at + 2) << 16; };
    auto fail = [&](std::string why) {
        r.error = std::move(why);
        r.data.clear();
        return r;
    };
    // End of central directory: 22 bytes plus a comment of up to 64 KB.
    size_t eocd = n;
    for (size_t i = n < 22 ? 0 : n - 21; i-- > 0 && n - i <= 22 + 0xFFFF;)
        if (u32(i) == 0x06054b50) { eocd = i; break; }
    if (eocd == n) return fail("no end-of-central-directory record");
    const uint32_t entries = u16(eocd + 10);
    size_t at = u32(eocd + 16);
    for (uint32_t e = 0; e < entries; ++e) {
        if (at + 46 > n || u32(at) != 0x02014b50) return fail("central directory entry " + std::to_string(e) + " malformed");
        const uint32_t method = u16(at + 10), crc = u32(at + 16), csize = u32(at + 20), usize = u32(at + 24);
        const uint32_t name_len = u16(at + 28), extra_len = u16(at + 30), comment_len = u16(at + 32);
        const size_t local = u32(at + 42);
        if (at + 46 + name_len > n) return fail("central directory name past the end");
        std::string name(zip.begin() + long(at + 46), zip.begin() + long(at + 46 + name_len));
        at += 46 + name_len + extra_len + comment_len;
        std::string lower = name;
        for (auto& ch : lower) ch = char(std::tolower(uint8_t(ch)));
        if (lower.size() < 4 || lower.compare(lower.size() - 4, 4, ".xml") != 0) continue;
        r.member = name;
        if (local + 30 > n || u32(local) != 0x04034b50) return fail("local header of '" + name + "' malformed");
        if (u16(local + 8) != method) return fail("'" + name + "': local and central methods differ");
        if (u16(local + 6) & 1) return fail("'" + name + "' is encrypted");
        const size_t data = local + 30 + u16(local + 26) + u16(local + 28);
        if (data + csize > n) return fail("'" + name + "' data past the end");
        // Bit 3: the local CRC and sizes are in a data descriptor; the
        // central directory has them either way.
        if (!(u16(local + 6) & 8) && u32(local + 14) != crc) return fail("'" + name + "': local and central CRC-32 differ");
        if (method == 0) {
            if (csize != usize) return fail("'" + name + "': STORE with compressed size != size");
            r.data.assign(zip.begin() + long(data), zip.begin() + long(data + csize));
        } else if (method == 8) {
            r.data.resize(usize);
            z_stream zs{};
            if (inflateInit2(&zs, -15) != Z_OK) return fail("inflateInit2 failed");
            zs.next_in = const_cast<Bytef*>(zip.data() + data);
            zs.avail_in = uInt(csize);
            zs.next_out = r.data.data();
            zs.avail_out = uInt(usize);
            const int rc = inflate(&zs, Z_FINISH);
            const uLong out = zs.total_out;
            inflateEnd(&zs);
            if (rc != Z_STREAM_END) return fail("'" + name + "': inflate " + std::to_string(rc) + (zs.msg ? std::string(" ") + zs.msg : ""));
            if (out != usize) return fail("'" + name + "': inflated " + std::to_string(out) + " bytes, header says " + std::to_string(usize));
        } else {
            return fail("'" + name + "': compression method " + std::to_string(method) + " (only STORE 0 / DEFLATE 8)");
        }
        const uint32_t got = uint32_t(crc32(0, r.data.data(), uInt(r.data.size())));
        if (got != crc) {
            char buf[80];
            std::snprintf(buf, sizeof buf, "': CRC-32 0x%08X, header says 0x%08X", got, crc);
            return fail("'" + name + buf);
        }
        return r;
    }
    return fail("no .xml member among " + std::to_string(entries) + " entries");
}

}  // namespace cxp::validation::checks::gen
