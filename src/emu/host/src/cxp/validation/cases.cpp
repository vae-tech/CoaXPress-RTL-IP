#include "cxp/validation/cases.h"

#include <cstdlib>
#include <fstream>
#include <iterator>
#include <stdexcept>

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

namespace cxp::validation {

namespace {

std::string str(const QJsonObject& o, const char* key) {
    return o.value(key).toString().toStdString();
}

ParamValue parseParam(const QJsonValue& v) {
    ParamValue p;
    if (v.isDouble()) {
        p.num = v.toDouble();
    } else if (v.isString()) {
        p.kind = ParamValue::Kind::String;
        p.str = v.toString().toStdString();
    } else if (v.isArray()) {
        p.kind = ParamValue::Kind::List;
        for (const QJsonValue& x : v.toArray()) p.list.push_back(parseParam(x));
    } else if (v.isObject()) {
        p.kind = ParamValue::Kind::Group;
        const QJsonObject o = v.toObject();
        for (auto it = o.begin(); it != o.end(); ++it) p.group.emplace_back(it.key().toStdString(), parseParam(it.value()));
    } else {
        throw std::runtime_error("a parameter is neither a number, a string, a list nor a group");
    }
    return p;
}

std::string paramText(const ParamValue& p) {
    switch (p.kind) {
    case ParamValue::Kind::Number: {
        const QString s = QString::number(p.num, 'g', 12);
        return s.toStdString();
    }
    case ParamValue::Kind::String: return p.str;
    case ParamValue::Kind::List: {
        std::string s = "[";
        for (size_t i = 0; i < p.list.size(); ++i) s += (i ? ", " : "") + paramText(p.list[i]);
        return s + "]";
    }
    case ParamValue::Kind::Group: {
        std::string s = "{";
        for (size_t i = 0; i < p.group.size(); ++i) {
            s += (i ? ", " : "") + p.group[i].first + "=" + paramText(p.group[i].second);
        }
        return s + "}";
    }
    }
    return {};
}

std::vector<std::string> strList(const QJsonObject& o, const char* key) {
    std::vector<std::string> out;
    for (const QJsonValue& v : o.value(key).toArray()) out.push_back(v.toString().toStdString());
    return out;
}

CaseDef parseCase(const QJsonObject& o) {
    CaseDef c;
    c.id = str(o, "id");
    if (c.id.empty()) throw std::runtime_error("validation case without an id");
    c.title = str(o, "title");
    c.area = str(o, "area");
    c.section = str(o, "section");
    c.test_class = str(o, "class");
    c.automation = str(o, "automation");
    c.kind = str(o, "kind");
    c.hardware_dependent = o.value("hardware_dependent").toBool();
    for (const QJsonValue& v : o.value("requirements").toArray()) {
        const QJsonObject r = v.toObject();
        c.requirements.push_back({str(r, "id"), str(r, "level"), str(r, "clause"), str(r, "text")});
    }
    c.clauses = str(o, "clauses");
    c.objective = str(o, "objective");
    c.preconditions = str(o, "preconditions");
    c.equipment = strList(o, "equipment");
    c.procedure = strList(o, "procedure");
    c.stimulus = str(o, "stimulus");
    c.expected = str(o, "expected");
    c.pass_criteria = str(o, "pass_criteria");
    c.fail_criteria = str(o, "fail_criteria");
    c.evidence = str(o, "evidence");

    const QJsonObject e = o.value("emulator").toObject();
    if (e.isEmpty()) throw std::runtime_error(c.id + ": no emulator block");
    c.emulator.runnable = e.value("runnable").toBool();
    c.emulator.reason = str(e, "reason");
    c.emulator.scope = str(e, "scope");
    c.emulator.procedure = strList(e, "procedure");
    c.emulator.pass_criteria = str(e, "pass_criteria");
    c.emulator.fail_criteria = str(e, "fail_criteria");
    c.emulator.timeout_scale = e.value("timeout_scale").toDouble(1.0);
    if (c.emulator.timeout_scale <= 0) throw std::runtime_error(c.id + ": timeout_scale must be > 0");
    const QJsonObject params = e.value("params").toObject();
    for (auto it = params.begin(); it != params.end(); ++it) {
        try {
            c.emulator.params[it.key().toStdString()] = parseParam(it.value());
        } catch (const std::runtime_error& ex) {
            throw std::runtime_error(c.id + ": parameter " + it.key().toStdString() + ": " + ex.what());
        }
    }

    const QJsonObject u = o.value("uvm").toObject();
    c.uvm.test = str(u, "test");
    c.uvm.source = str(u, "source");
    c.uvm.plan = strList(u, "plan");
    for (const QJsonValue& v : u.value("expect_fail").toArray()) {
        const QJsonObject x = v.toObject();
        c.uvm.expect_fail.push_back({str(x, "scoreboard"), str(x, "kind"), str(x, "finding")});
    }
    c.uvm.tiers = strList(u, "tiers");
    c.uvm_tests = strList(o, "uvm_tests");
    return c;
}

}  // namespace

std::string describeParams(const Params& p) {
    std::string s;
    for (const auto& [k, v] : p) s += (s.empty() ? "" : " ") + k + "=" + paramText(v);
    return s;
}

const CaseDef* Catalogue::find(const std::string& id) const {
    for (const CaseDef& c : cases) {
        if (c.id == id) return &c;
    }
    return nullptr;
}

Catalogue parseCatalogue(const std::string& json_text) {
    QJsonParseError err{};
    const QJsonDocument doc =
        QJsonDocument::fromJson(QByteArray::fromStdString(json_text), &err);
    if (doc.isNull()) {
        throw std::runtime_error("validation catalogue: " + err.errorString().toStdString() +
                                 " at offset " + std::to_string(err.offset));
    }
    const QJsonObject root = doc.object();
    Catalogue cat;
    cat.schema = str(root, "schema");
    const QJsonObject src = root.value("source").toObject();
    cat.plan = str(src, "plan");
    cat.plan_version = str(src, "plan_version");
    cat.generated = str(src, "generated");
    for (const QJsonValue& v : root.value("cases").toArray()) cat.cases.push_back(parseCase(v.toObject()));
    if (cat.cases.empty()) throw std::runtime_error("validation catalogue holds no cases");
    return cat;
}

Catalogue loadCatalogue(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open validation catalogue " + path);
    return parseCatalogue({std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()});
}

std::string defaultCataloguePath() {
    if (const char* env = std::getenv("CXP_VALIDATION_JSON")) return env;
    return CXP_VALIDATION_JSON;
}

}  // namespace cxp::validation
