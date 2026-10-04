// CXP-CAM-GEN-001.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen001(Context& c) {
    c.preserve(Reg::XML_MANIFEST_SELECTOR);
    std::string url;
    auto xml = fetchXmlFile(c, &url);
    c.expect(!xml.empty(), "XML read through '%s': %zu bytes", url.c_str(), xml.size());
    if (xml.size() >= 4 && xml[0] == 'P' && xml[1] == 'K') {
        size_t bad = 0;
        for (size_t i = 0; i + 30 < xml.size(); ++i) {
            if (xml[i] == 'P' && xml[i + 1] == 'K' && xml[i + 2] == 3 && xml[i + 3] == 4) {
                const int method = xml[i + 8] | xml[i + 9] << 8;
                bad += method != 0 && method != 8;
            }
        }
        c.expect(bad == 0, "zip members use only STORE (0) or DEFLATE (8)");
        auto unz = unzipXml(xml);
        if (!unz.error.empty()) {
            c.expect(false, "zipped XML unreadable: %s", unz.error.c_str());
            return;
        }
        c.info("zipped XML: member '%s', %zu bytes inflated", unz.member.c_str(), unz.data.size());
        xml = std::move(unz.data);
    }
    QXmlStreamReader rd(QByteArray(reinterpret_cast<const char*>(xml.data()), int(xml.size())));
    QString root;
    QXmlStreamAttributes attrs;
    while (!rd.atEnd()) {
        rd.readNext();
        if (rd.isStartElement() && root.isEmpty()) {
            root = rd.name().toString();
            attrs = rd.attributes();
        }
    }
    c.expect(!rd.hasError(), "XML well formed%s", rd.hasError() ? (": " + rd.errorString().toStdString()).c_str() : "");
    c.expect(root == "RegisterDescription", "root element <%s> (RegisterDescription)", root.toStdString().c_str());
    auto num = [&](const char* k) { return attrs.value(k).toUInt(); };
    const uint32_t schema = num("SchemaMajorVersion") << 16 | num("SchemaMinorVersion") << 8 | num("SchemaSubMinorVersion");
    const uint32_t file = num("MajorVersion") << 16 | num("MinorVersion") << 8 | num("SubMinorVersion");
    const uint32_t r_schema = c.rd32(Reg::XML_SCHEMA_VERSION), r_file = c.rd32(Reg::XML_VERSION);
    c.expect(r_schema == schema, "XmlSchemaVersion register %s = file schema %s", hex(r_schema).c_str(), hex(schema).c_str());
    c.expect(r_file == file, "XmlVersion register %s = file version %s", hex(r_file).c_str(), hex(file).c_str());
    c.note("no GenApi XSD validator in this host: well-formedness and root/version attributes only");
}
CXP_CHECK("CXP-CAM-GEN-001", gen001);

}  // namespace

}  // namespace cxp::validation::checks::gen
