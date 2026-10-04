#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

bool urlGrammarOk(const std::string& url) {
    static const char* schemes[] = {"Local:", "Web:", "File:", "local:", "web:", "file:"};
    for (const char* s : schemes) {
        if (url.rfind(s, 0) == 0) {
            if (url.rfind("Local:", 0) == 0 || url.rfind("local:", 0) == 0) return parseGenicamUrl(url).has_value();
            return url.size() > std::strlen(s);
        }
    }
    return false;
}

void checkString(Context& c, const char* name, uint32_t addr, uint32_t len, bool required) {
    auto a = c.readRaw(addr, len);
    if (!c.expect(is(a, Ack::READ_OK) && a->size_field == len, "%s: one %u-byte read answered %s", name, len,
                  ackStr(a).c_str())) {
        return;
    }
    auto b = a->bytes();
    b.resize(len);
    size_t nul = std::find(b.begin(), b.end(), uint8_t(0)) - b.begin();
    bool ascii = std::all_of(b.begin(), b.begin() + nul, [](uint8_t ch) { return ch >= 0x20 && ch <= 0x7E; });
    std::string s(b.begin(), b.begin() + nul);
    c.expect(ascii, "%s = '%s' is printable ASCII", name, s.c_str());
    if (nul == len) c.info("%s fills the register (no terminator, allowed)", name);
    if (required) c.expect(nul > 0, "%s is not empty", name);
    if (nul < len && std::any_of(b.begin() + nul, b.end(), [](uint8_t ch) { return ch != 0; })) {
        c.note("%s has non-zero bytes after the terminator", name);
    }
}

}  // namespace cxp::validation::checks::boot
