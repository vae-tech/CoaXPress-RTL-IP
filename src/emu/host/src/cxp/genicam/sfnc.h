// GenICam SFNC register-tree parser (cxp/genicam/sfnc.py).
//
// A pragmatic subset of GenApi sufficient for SFNC feature access over the
// CXP bootstrap/vendor register space.  Supported node types:
//
//     Category     grouping node (hierarchical browsing)
//     Integer      IntReg-backed or constant integer feature
//     Float        FloatReg-backed or constant float feature
//     Enumeration  EnumEntry list mapped onto an integer register
//     Boolean      1/0 integer register
//     String       fixed-length string register
//     Command      write-a-value-to-trigger register
//
// A RegisterAccessor abstracts the transport.  Reads are cached until
// NodeTree::invalidate() (a Command write or an explicit flush) so feature
// access does not re-hit the link.  The cache is mutex-protected; register
// I/O itself runs outside the lock.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace cxp {

class GenIcamError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

enum class FeatureKind { Category, Integer, Float, Enumeration, Boolean, String, Command };

const char* featureKindName(FeatureKind k);  // "Category", "Integer", ...
std::optional<FeatureKind> toFeatureKind(const std::string& tag);

class RegisterAccessor {
public:
    virtual ~RegisterAccessor() = default;
    virtual std::vector<uint8_t> read(uint64_t address, size_t length) = 0;
    virtual void write(uint64_t address, const std::vector<uint8_t>& data) = 0;
};

// A dynamically-typed feature value (Python int / float / bool / str).
struct Value {
    enum class Type { Int, Float, Bool, String };
    Type type = Type::Int;
    int64_t i = 0;
    double f = 0.0;
    bool b = false;
    std::string s;

    static Value ofInt(int64_t v) { Value x; x.type = Type::Int; x.i = v; return x; }
    static Value ofFloat(double v) { Value x; x.type = Type::Float; x.f = v; return x; }
    static Value ofBool(bool v) { Value x; x.type = Type::Bool; x.b = v; return x; }
    static Value ofString(std::string v) { Value x; x.type = Type::String; x.s = std::move(v); return x; }

    std::string str() const;   // Python str(): 1024, 1.0, True, ACME
    std::string repr() const;  // Python repr(): 1024, 1.0, True, 'ACME'
    bool truthy() const;
};

struct Reg {
    uint64_t address = 0;
    uint32_t length = 4;
    std::string access = "RW";  // RO / WO / RW
    bool is_signed = false;
};

class NodeTree;

// One node in the SFNC tree.
struct Feature {
    std::string name;
    FeatureKind kind = FeatureKind::Category;
    std::string description;
    std::string access = "RW";
    std::vector<Feature*> children;  // categories
    std::optional<Reg> reg;
    std::optional<Value> const_value;
    std::vector<std::pair<std::string, int64_t>> enum_entries;  // XML order
    int64_t command_value = 1;
    std::optional<double> min;
    std::optional<double> max;
    std::string p_available;  // dependency feature name; empty = none
    NodeTree* tree = nullptr;

    std::string effAccess() const { return reg ? reg->access : access; }
    bool isReadable() const;
    bool isWritable() const;
    bool isAvailable() const;

    Value getValue() const;
    void setValue(const Value& value) const;
    void execute() const;
};

class NodeTree {
public:
    explicit NodeTree(std::shared_ptr<RegisterAccessor> accessor = nullptr);

    static std::unique_ptr<NodeTree> fromFile(const std::string& path,
                                              std::shared_ptr<RegisterAccessor> accessor = nullptr);
    static std::unique_ptr<NodeTree> fromString(const std::vector<uint8_t>& xml,
                                                std::shared_ptr<RegisterAccessor> accessor = nullptr);

    std::string model_name;
    std::string vendor_name;
    std::string schema_version;

    void setAccessor(std::shared_ptr<RegisterAccessor> accessor);
    std::shared_ptr<RegisterAccessor> accessor() const;

    // Features in XML declaration order.
    const std::vector<Feature*>& features() const { return order_; }
    Feature* root() const { return root_; }
    Feature* find(const std::string& name) const;
    // Case-insensitive ECMAScript regex over names and descriptions.
    std::vector<Feature*> search(const std::string& pattern) const;
    std::string treeText() const;

    // -- register cache ----------------------------------------------------
    std::vector<uint8_t> cachedRead(const Reg& reg);
    void write(const Reg& reg, const std::vector<uint8_t>& data);
    void primeCache(uint64_t address, std::vector<uint8_t> data);
    void invalidate();  // drop the whole register cache (after Command / reset)

private:
    void parse(const std::vector<uint8_t>& xml);
    Feature* add(std::unique_ptr<Feature> feat);

    mutable std::mutex mu_;
    std::shared_ptr<RegisterAccessor> accessor_;
    std::vector<std::unique_ptr<Feature>> owned_;
    std::vector<Feature*> order_;
    std::map<std::string, Feature*> by_name_;
    Feature* root_ = nullptr;
    std::map<uint64_t, std::vector<uint8_t>> cache_;
};

}  // namespace cxp
