#include "cxp/utils/hexdump.h"

#include "cxp/utils/log.h"

namespace cxp {

std::string hexdump(const std::vector<uint8_t>& data, int width) {
    std::string out;
    for (size_t off = 0; off < data.size(); off += width) {
        std::string hexs, text;
        for (size_t i = off; i < data.size() && i < off + width; ++i) {
            if (!hexs.empty()) hexs += ' ';
            hexs += strprintf("%02x", data[i]);
            uint8_t b = data[i];
            text += (b >= 32 && b < 127) ? static_cast<char>(b) : '.';
        }
        if (!out.empty()) out += '\n';
        out += strprintf("%08zx  %-*s  %s", off, width * 3, hexs.c_str(),
                         text.c_str());
    }
    return out;
}

std::string wordsHex(const std::vector<uint32_t>& words, int per_line) {
    std::string out;
    for (size_t i = 0; i < words.size(); i += per_line) {
        std::string line = strprintf("[%4zu]", i);
        for (size_t j = i; j < words.size() && j < i + per_line; ++j) {
            line += strprintf(" %08x", words[j]);
        }
        if (!out.empty()) out += '\n';
        out += line;
    }
    return out;
}

}  // namespace cxp
