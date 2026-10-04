#include "cxp/genicam/sfnc.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <fstream>
#include <functional>
#include <iterator>
#include <regex>
#include <set>

#include <QByteArray>
#include <QXmlStreamReader>

#include "cxp/utils/log.h"

namespace cxp {

namespace {

// -- tiny DOM over QXmlStreamReader ---------------------------------------
struct XmlNode {
    std::string tag;  // local name (namespace stripped)
    std::map<std::string, std::string> attrs;
    std::string text;
    std::vector<std::unique_ptr<XmlNode>> kids;

    std::string attr(const std::string& k, const std::string& def = "") const {
        auto it = attrs.find(k);
        return it == attrs.end() ? def : it->second;
    }
};

std::string strip(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace(static_cast<unsigned char>(s[a]))) ++a;
    while (b > a && std::isspace(static_cast<unsigned char>(s[b - 1]))) --b;
    return s.substr(a, b - a);
}

std::unique_ptr<XmlNode> parseXml(const std::vector<uint8_t>& xml) {
    QXmlStreamReader rd(QByteArray(reinterpret_cast<const char*>(xml.data()),
                                   static_cast<int>(xml.size())));
    std::unique_ptr<XmlNode> root;
    std::vector<XmlNode*> stack;
    while (!rd.atEnd()) {
        auto tok = rd.readNext();
        if (tok == QXmlStreamReader::StartElement) {
            auto node = std::make_unique<XmlNode>();
            node->tag = rd.name().toString().toStdString();
            for (const auto& a : rd.attributes()) {
                node->attrs[a.name().toString().toStdString()] = a.value().toString().toStdString();
            }
            XmlNode* raw = node.get();
            if (stack.empty()) {
                if (root) throw GenIcamError("multiple XML root elements");
                root = std::move(node);
            } else {
                stack.back()->kids.push_back(std::move(node));
            }
            stack.push_back(raw);
        } else if (tok == QXmlStreamReader::EndElement) {
            if (!stack.empty()) stack.pop_back();
        } else if (tok == QXmlStreamReader::Characters && !stack.empty()) {
            // ElementTree .text = text before the first child element.
            if (stack.back()->kids.empty()) {
                stack.back()->text += rd.text().toString().toStdString();
            }
        }
    }
    if (rd.hasError()) {
        throw GenIcamError(strprintf("XML parse error at line %lld: %s",
                                     static_cast<long long>(rd.lineNumber()),
                                     rd.errorString().toStdString().c_str()));
    }
    if (!root) throw GenIcamError("empty XML document");
    return root;
}

// Stripped text of the first direct child named `child`.
std::optional<std::string> childText(const XmlNode& el, const std::string& child) {
    for (const auto& c : el.kids) {
        if (c->tag == child) return strip(c->text);
    }
    return std::nullopt;
}

int64_t toInt(const std::string& raw) {
    std::string s = strip(raw);
    try {
        size_t pos = 0;
        int64_t v;
        if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            v = static_cast<int64_t>(std::stoull(s.substr(2), &pos, 16));
            pos += 2;
        } else {
            v = std::stoll(s, &pos, 10);
        }
        if (pos != s.size()) throw std::invalid_argument(s);
        return v;
    } catch (const std::exception&) {
        throw GenIcamError("invalid integer literal '" + s + "'");
    }
}

int64_t toIntOr(const std::optional<std::string>& s, int64_t def) {
    return s ? toInt(*s) : def;
}

std::optional<double> optNum(const std::optional<std::string>& s) {
    if (!s) return std::nullopt;
    std::string lower(*s);
    std::transform(lower.begin(), lower.end(), lower.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    try {
        if (lower.find('.') == std::string::npos && lower.find('e') == std::string::npos) {
            return static_cast<double>(toInt(*s));
        }
        size_t pos = 0;
        double d = std::stod(*s, &pos);
        if (pos != s->size()) return std::nullopt;
        return d;
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

Value coerceConst(FeatureKind kind, const std::string& s) {
    switch (kind) {
    case FeatureKind::Float:
        try {
            return Value::ofFloat(std::stod(s));
        } catch (const std::exception&) {
            throw GenIcamError("invalid float literal '" + s + "'");
        }
    case FeatureKind::Boolean: {
        std::string t = strip(s);
        return Value::ofBool(t == "1" || t == "true" || t == "True");
    }
    case FeatureKind::Integer:
    case FeatureKind::Enumeration:
        return Value::ofInt(toInt(s));
    default:
        return Value::ofString(s);
    }
}

std::string upper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return std::toupper(c); });
    return s;
}

std::string pyFloat(double v) {
    if (std::isfinite(v) && v == std::floor(v) && std::fabs(v) < 1e16) {
        return strprintf("%.1f", v);
    }
    return strprintf("%.17g", v);
}

}  // namespace

// -- enums / Value ------------------------------------------------------------
const char* featureKindName(FeatureKind k) {
    switch (k) {
    case FeatureKind::Category:    return "Category";
    case FeatureKind::Integer:     return "Integer";
    case FeatureKind::Float:       return "Float";
    case FeatureKind::Enumeration: return "Enumeration";
    case FeatureKind::Boolean:     return "Boolean";
    case FeatureKind::String:      return "String";
    case FeatureKind::Command:     return "Command";
    }
    return "?";
}

std::optional<FeatureKind> toFeatureKind(const std::string& tag) {
    static const std::pair<const char*, FeatureKind> kinds[] = {
        {"Category", FeatureKind::Category},
        {"Integer", FeatureKind::Integer},
        {"Float", FeatureKind::Float},
        {"Enumeration", FeatureKind::Enumeration},
        {"Boolean", FeatureKind::Boolean},
        {"String", FeatureKind::String},
        {"Command", FeatureKind::Command},
    };
    for (const auto& kv : kinds) {
        if (tag == kv.first) return kv.second;
    }
    return std::nullopt;
}

std::string Value::str() const {
    switch (type) {
    case Type::Int:    return std::to_string(i);
    case Type::Float:  return pyFloat(f);
    case Type::Bool:   return b ? "True" : "False";
    case Type::String: return s;
    }
    return {};
}

std::string Value::repr() const {
    if (type != Type::String) return str();
    std::string out = "'";
    for (char c : s) {
        if (c == '\'' || c == '\\') out += '\\';
        out += c;
    }
    return out + "'";
}

bool Value::truthy() const {
    switch (type) {
    case Type::Int:    return i != 0;
    case Type::Float:  return f != 0.0;
    case Type::Bool:   return b;
    case Type::String: return !s.empty();
    }
    return false;
}

// -- Feature ------------------------------------------------------------------
bool Feature::isReadable() const {
    return kind != FeatureKind::Command && effAccess().find('R') != std::string::npos;
}

bool Feature::isWritable() const { return effAccess().find('W') != std::string::npos; }

bool Feature::isAvailable() const {
    if (p_available.empty() || tree == nullptr) return true;
    Feature* dep = tree->find(p_available);
    if (dep == nullptr) return false;
    try {
        return dep->getValue().truthy();
    } catch (const GenIcamError&) {
        return true;
    }
}

Value Feature::getValue() const {
    if (const_value) return *const_value;
    if (kind == FeatureKind::Category) throw GenIcamError(name + " is a category");
    if (kind == FeatureKind::Command) throw GenIcamError("commands are not readable");
    if (tree == nullptr || !reg) throw GenIcamError(name + " has no backing register");
    std::vector<uint8_t> raw = tree->cachedRead(*reg);
    if (kind == FeatureKind::String) {
        std::string s;
        for (uint8_t c : raw) {
            if (c == 0) break;
            s += (c < 0x80) ? static_cast<char>(c) : '?';
        }
        return Value::ofString(s);
    }
    uint64_t u = 0;
    for (uint8_t c : raw) u = (u << 8) | c;
    int64_t ival = static_cast<int64_t>(u);
    if (reg->is_signed && !raw.empty() && raw.size() < 8 && (raw[0] & 0x80)) {
        ival -= int64_t(1) << (8 * raw.size());
    }
    if (kind == FeatureKind::Boolean) return Value::ofBool(ival != 0);
    if (kind == FeatureKind::Enumeration) {
        for (const auto& kv : enum_entries) {
            if (kv.second == ival) return Value::ofString(kv.first);
        }
        return Value::ofInt(ival);
    }
    if (kind == FeatureKind::Float) return Value::ofFloat(static_cast<double>(ival));
    return Value::ofInt(ival);
}

void Feature::setValue(const Value& value) const {
    if (!isWritable()) throw GenIcamError(name + " is not writable");
    if (tree == nullptr || !reg) throw GenIcamError(name + " has no backing register");
    int64_t ival = 0;
    if (kind == FeatureKind::Enumeration) {
        std::string key = value.str();
        auto it = std::find_if(enum_entries.begin(), enum_entries.end(),
                               [&](const auto& kv) { return kv.first == key; });
        if (it == enum_entries.end()) {
            std::string names;
            for (const auto& kv : enum_entries) {
                names += (names.empty() ? "'" : ", '") + kv.first + "'";
            }
            throw GenIcamError(value.repr() + " not in [" + names + "]");
        }
        ival = it->second;
    } else if (kind == FeatureKind::Boolean) {
        ival = value.truthy() ? 1 : 0;
    } else if (kind == FeatureKind::String) {
        std::vector<uint8_t> data(reg->length, 0);
        std::string s = value.str();
        std::copy_n(s.begin(), std::min<size_t>(s.size(), reg->length), data.begin());
        tree->write(*reg, data);
        return;
    } else {
        switch (value.type) {
        case Value::Type::Int:   ival = value.i; break;
        case Value::Type::Float: ival = static_cast<int64_t>(value.f); break;
        case Value::Type::Bool:  ival = value.b ? 1 : 0; break;
        case Value::Type::String: ival = toInt(value.s); break;
        }
        if (min && ival < *min) throw GenIcamError(value.str() + " < min " + pyFloat(*min));
        if (max && ival > *max) throw GenIcamError(value.str() + " > max " + pyFloat(*max));
    }
    std::vector<uint8_t> data(reg->length, 0);
    for (uint32_t k = 0; k < reg->length && k < 8; ++k) {
        data[reg->length - 1 - k] = static_cast<uint8_t>(static_cast<uint64_t>(ival) >> (8 * k));
    }
    tree->write(*reg, data);
}

void Feature::execute() const {
    if (kind != FeatureKind::Command) throw GenIcamError(name + " is not a command");
    if (tree == nullptr || !reg) throw GenIcamError(name + " has no backing register");
    std::vector<uint8_t> data(reg->length, 0);
    for (uint32_t k = 0; k < reg->length && k < 8; ++k) {
        data[reg->length - 1 - k] =
            static_cast<uint8_t>(static_cast<uint64_t>(command_value) >> (8 * k));
    }
    tree->write(*reg, data);
    tree->invalidate();
}

// -- NodeTree -----------------------------------------------------------------
NodeTree::NodeTree(std::shared_ptr<RegisterAccessor> accessor)
    : accessor_(std::move(accessor)) {}

std::unique_ptr<NodeTree> NodeTree::fromFile(const std::string& path,
                                             std::shared_ptr<RegisterAccessor> accessor) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw GenIcamError("cannot open " + path);
    std::vector<uint8_t> xml((std::istreambuf_iterator<char>(f)),
                             std::istreambuf_iterator<char>());
    return fromString(xml, std::move(accessor));
}

std::unique_ptr<NodeTree> NodeTree::fromString(const std::vector<uint8_t>& xml,
                                               std::shared_ptr<RegisterAccessor> accessor) {
    auto tree = std::make_unique<NodeTree>(std::move(accessor));
    tree->parse(xml);
    return tree;
}

void NodeTree::setAccessor(std::shared_ptr<RegisterAccessor> accessor) {
    std::lock_guard<std::mutex> lk(mu_);
    accessor_ = std::move(accessor);
}

std::shared_ptr<RegisterAccessor> NodeTree::accessor() const {
    std::lock_guard<std::mutex> lk(mu_);
    return accessor_;
}

Feature* NodeTree::add(std::unique_ptr<Feature> feat) {
    feat->tree = this;
    Feature* raw = feat.get();
    auto it = by_name_.find(raw->name);
    if (it != by_name_.end()) {
        std::replace(order_.begin(), order_.end(), it->second, raw);
    } else {
        order_.push_back(raw);
    }
    by_name_[raw->name] = raw;
    owned_.push_back(std::move(feat));
    return raw;
}

void NodeTree::parse(const std::vector<uint8_t>& xml) {
    auto root_el = parseXml(xml);
    model_name = root_el->attr("ModelName");
    vendor_name = root_el->attr("VendorName");
    schema_version = root_el->attr("SchemaMajorVersion", "1") + "." +
                     root_el->attr("SchemaMinorVersion", "1");

    std::map<std::string, Reg> regs;
    std::vector<std::pair<Feature*, std::string>> pending;  // (feature, pValue)
    std::vector<std::pair<std::string, std::vector<std::string>>> cat_links;

    static const std::set<std::string> reg_tags = {"IntReg", "FloatReg", "StringReg",
                                                   "MaskedIntReg", "Register"};
    for (const auto& elp : root_el->kids) {
        const XmlNode& el = *elp;
        std::string name = el.attr("Name");
        if (name.empty()) continue;
        if (reg_tags.count(el.tag)) {
            Reg r;
            r.address = static_cast<uint64_t>(toIntOr(childText(el, "Address"), 0));
            r.length = static_cast<uint32_t>(toIntOr(childText(el, "Length"), 4));
            auto acc = childText(el, "AccessMode");
            r.access = upper(acc && !acc->empty() ? *acc : "RW");
            r.is_signed = childText(el, "Sign").value_or("Unsigned") == "Signed";
            regs[name] = r;
            continue;
        }
        if (el.tag == "Category") {
            auto feat = std::make_unique<Feature>();
            feat->name = name;
            feat->kind = FeatureKind::Category;
            feat->description = childText(el, "Description").value_or("");
            std::vector<std::string> kids;
            for (const auto& c : el.kids) {
                if (c->tag == "pFeature" && !c->text.empty()) kids.push_back(strip(c->text));
            }
            cat_links.emplace_back(name, std::move(kids));
            add(std::move(feat));
            continue;
        }
        auto kind = toFeatureKind(el.tag);
        if (!kind || *kind == FeatureKind::Category) continue;
        auto feat = std::make_unique<Feature>();
        feat->name = name;
        feat->kind = *kind;
        feat->description = childText(el, "Description").value_or("");
        auto acc = childText(el, "AccessMode");
        feat->access = upper(acc && !acc->empty() ? *acc : "RW");
        feat->p_available = childText(el, "pIsAvailable").value_or("");
        feat->min = optNum(childText(el, "Min"));
        feat->max = optNum(childText(el, "Max"));
        if (*kind == FeatureKind::Command) {
            feat->command_value = toIntOr(childText(el, "CommandValue"), 1);
        }
        if (*kind == FeatureKind::Enumeration) {
            for (const auto& ee : el.kids) {
                if (ee->tag == "EnumEntry") {
                    feat->enum_entries.emplace_back(ee->attr("Name"),
                                                    toIntOr(childText(*ee, "Value"), 0));
                }
            }
        }
        auto cval = childText(el, "Value");
        auto pval = childText(el, "pValue");
        Feature* raw = add(std::move(feat));
        if (pval && !pval->empty()) {
            pending.emplace_back(raw, *pval);
        } else if (cval) {
            raw->const_value = coerceConst(*kind, *cval);
        }
    }

    for (auto& [feat, pval] : pending) {
        auto r = regs.find(pval);
        if (r != regs.end()) {
            feat->reg = r->second;
        } else if (Feature* other = find(pval)) {  // pValue -> another feature's reg
            feat->reg = other->reg;
        }
    }

    for (const auto& [cname, kids] : cat_links) {
        Feature* cat = find(cname);
        if (cat == nullptr) continue;
        for (const auto& k : kids) {
            if (Feature* child = find(k)) cat->children.push_back(child);
        }
    }

    root_ = find("Root");
    if (root_ == nullptr) {
        auto synth = std::make_unique<Feature>();
        synth->name = "Root";
        synth->kind = FeatureKind::Category;
        synth->tree = this;
        for (Feature* f : order_) {
            if (f->kind == FeatureKind::Category) synth->children.push_back(f);
        }
        root_ = synth.get();
        owned_.push_back(std::move(synth));  // owned, but not listed in features()
    }
}

std::vector<uint8_t> NodeTree::cachedRead(const Reg& reg) {
    std::shared_ptr<RegisterAccessor> acc;
    {
        std::lock_guard<std::mutex> lk(mu_);
        auto it = cache_.find(reg.address);
        if (it != cache_.end()) return it->second;
        acc = accessor_;
    }
    if (!acc) throw GenIcamError("no register accessor bound");
    std::vector<uint8_t> data = acc->read(reg.address, reg.length);
    std::lock_guard<std::mutex> lk(mu_);
    cache_[reg.address] = data;
    return data;
}

void NodeTree::write(const Reg& reg, const std::vector<uint8_t>& data) {
    std::shared_ptr<RegisterAccessor> acc = accessor();
    if (!acc) throw GenIcamError("no register accessor bound");
    acc->write(reg.address, data);
    std::lock_guard<std::mutex> lk(mu_);
    cache_.erase(reg.address);
}

void NodeTree::primeCache(uint64_t address, std::vector<uint8_t> data) {
    std::lock_guard<std::mutex> lk(mu_);
    cache_[address] = std::move(data);
}

void NodeTree::invalidate() {
    std::lock_guard<std::mutex> lk(mu_);
    cache_.clear();
}

Feature* NodeTree::find(const std::string& name) const {
    auto it = by_name_.find(name);
    return it == by_name_.end() ? nullptr : it->second;
}

std::vector<Feature*> NodeTree::search(const std::string& pattern) const {
    std::regex rx(pattern, std::regex::ECMAScript | std::regex::icase);
    std::vector<Feature*> out;
    for (Feature* f : order_) {
        if (std::regex_search(f->name, rx) || std::regex_search(f->description, rx)) {
            out.push_back(f);
        }
    }
    return out;
}

std::string NodeTree::treeText() const {
    std::set<std::string> seen;
    std::string out;
    std::function<void(const Feature*, int)> walk = [&](const Feature* node, int indent) {
        if (node == nullptr || seen.count(node->name)) return;
        seen.insert(node->name);
        if (!out.empty()) out += '\n';
        out += std::string(indent * 2, ' ') + node->name + " <" +
               featureKindName(node->kind) + ">";
        if (node->kind == FeatureKind::Category) {
            for (const Feature* c : node->children) walk(c, indent + 1);
        }
    };
    walk(root_, 0);
    return out;
}

}  // namespace cxp
