// Command-line interface (cxp/cli.py).
//
//     cxp sim             [--fifo-dir /tmp/cxp]
//     cxp gui             [--fifo-dir /tmp/cxp]
//     cxp discover        [--spawn-sim] [--params]
//     cxp params          [--spawn-sim]
//     cxp read   ADDR [N]
//     cxp write  ADDR VALUE
//     cxp xml-parse FILE  [--tree] [--search RE]
//     cxp stream-capture  [--frames N] [--out dir]
//     cxp packet-dump     [--frames N] [--pcap f.pcap]
//     cxp image-extract   [--out img.pgm]
//     cxp compliance      [--inject-crc]
//     cxp trace           [--pcap f.pcap] [--seconds S]
//     cxp validate        [ID ...] [--all] [--runnable] [--uvm] [--list]
//                         [--json out.json] [--log-dir DIR] [run options]
//
// The validation run options (--soak, --timeout-scale, --host-spsm, ...)
// come from validation::optionSpecs(), as the GUI's do.
//
// Commands that talk to a device accept --spawn-sim to launch the reference
// virtual camera in-process.  Global options (--fifo-dir, --log, --timeout,
// --protocol-log) may appear anywhere on the command line.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <thread>
#include <unistd.h>
#include <sys/stat.h>
#include <vector>

#include "cxp/camera/client.h"
#include "cxp/camera/protocol_log.h"
#include "cxp/camera/session.h"
#include "cxp/compliance/checker.h"
#include "cxp/genicam/sfnc.h"
#include "cxp/parser/stream_parser.h"
#include "cxp/sim/virtual_camera.h"
#include "cxp/transport/fifo.h"
#include "cxp/validation/campaign.h"
#include "cxp/validation/runner.h"
#include "cxp/utils/log.h"

using namespace cxp;

namespace {

std::atomic<bool> g_interrupted{false};

void onSignal(int) { g_interrupted = true; }

struct UsageError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// -- argument parsing -------------------------------------------------------------
struct Args {
    std::string cmd;
    std::string fifo_dir = "/tmp/cxp";
    std::string log = "INFO";
    double timeout = 5.0;
    bool spawn_sim = false;
    std::string pcap;
    bool params = false;
    bool tree = false;
    std::string search;
    bool search_set = false;
    int frames = 2;
    std::string out;
    bool inject_crc = false;
    double seconds = 2.0;
    bool all = false;
    bool runnable = false;  // validate: only cases the emulator runs
    bool uvm = false;       // validate: only the UVM tests' cases
    bool list = false;
    std::string json;
    std::string protocol_log;
    std::string log_dir;
    validation::Options vopt;  // validate: run options
    std::vector<std::string> positional;
};

const char* const kUsage =
    "usage: cxp [--fifo-dir DIR] [--log LEVEL] [--timeout SEC] <command> [options]\n"
    "\n"
    "CoaXPress 1.1.1 host application (C++/Qt port of emu/cxp).\n"
    "\n"
    "commands:\n"
    "  sim                              serve the virtual camera on the FIFOs\n"
    "  gui                              launch the cxp-gui device explorer\n"
    "  discover [--params]              discover the device (+ list parameters)\n"
    "  params                           list every parameter defined by the XML\n"
    "  read ADDR [N]                    read N register words\n"
    "  write ADDR VALUE                 write one register word\n"
    "  xml-parse FILE [--tree] [--search RE]\n"
    "  stream-capture [--frames N] [--out DIR]\n"
    "  packet-dump [--frames N]\n"
    "  image-extract [--out FILE]\n"
    "  compliance [--inject-crc] [--frames N]\n"
    "  trace [--seconds S]\n"
    "  validate [ID ...] [--all] [--runnable] [--uvm] [--list] [--json FILE]\n"
    "           [--log-dir DIR] [run options]\n"
    "                                   run validation-plan cases and the UVM\n"
    "                                   tests' emulator cases (IDs may be\n"
    "                                   suffixes, e.g. BOOT-002 or\n"
    "                                   test_crc_error); --runnable and --uvm\n"
    "                                   narrow the choice, or pick all such\n"
    "                                   cases when no ID is given; each case's\n"
    "                                   protocol log goes to DIR/<ID>.log\n"
    "                                   (default validation_logs/<date_time>)\n"
    "\n"
    "device options: --spawn-sim  --pcap FILE\n"
    "global options:\n"
    "  --fifo-dir DIR   FIFO directory (default /tmp/cxp)\n"
    "  --log LEVEL      DEBUG / INFO / WARNING / ERROR (default INFO)\n"
    "  --timeout SEC    device control-ack wait timeout, also the stream capture\n"
    "                   deadline, in seconds (default 5.0)\n"
    "  --protocol-log FILE  log every frame sent and received, decoded\n";

// kUsage plus the validation run options, one line each from optionSpecs().
std::string usage() {
    std::string u = kUsage;
    u += "validate run options:\n";
    const validation::Options dflt;
    for (const auto& s : validation::optionSpecs()) {
        std::string flag = std::string(s.flag) + (s.decimals ? " X" : " N");
        if (flag.size() < 20) flag.resize(20, ' ');
        u += strprintf("  %s %s (default %.*f%s%s)\n", flag.c_str(), s.help, s.decimals, s.get(dflt),
                       *s.unit && *s.unit != 'x' ? " " : "", *s.unit != 'x' ? s.unit : "");
    }
    return u;
}

uint64_t parseInt(const std::string& s) {
    size_t pos = 0;
    uint64_t v;
    try {
        v = std::stoull(s, &pos, 0);
    } catch (const std::exception&) {
        throw UsageError("invalid integer: '" + s + "'");
    }
    if (pos != s.size()) throw UsageError("invalid integer: '" + s + "'");
    return v;
}

double parseDouble(const std::string& s) {
    try {
        size_t pos = 0;
        double v = std::stod(s, &pos);
        if (pos == s.size()) return v;
    } catch (const std::exception&) {
    }
    throw UsageError("invalid number: '" + s + "'");
}

Args parseArgs(int argc, char** argv) {
    static const std::set<std::string> commands = {
        "sim", "gui", "discover", "params", "read", "write", "xml-parse",
        "stream-capture", "packet-dump", "image-extract", "compliance", "trace", "validate"};
    Args a;
    std::vector<std::string> av(argv + 1, argv + argc);
    auto value = [&](size_t& i, const std::string& opt) -> std::string {
        std::string arg = av[i];
        size_t eq = arg.find('=');
        if (eq != std::string::npos) return arg.substr(eq + 1);
        if (i + 1 >= av.size()) throw UsageError(opt + " expects a value");
        return av[++i];
    };
    for (size_t i = 0; i < av.size(); ++i) {
        const std::string& arg = av[i];
        std::string opt = arg.substr(0, arg.find('='));
        const validation::OptionSpec* spec = nullptr;
        for (const auto& s : validation::optionSpecs()) {
            if (opt == s.flag) spec = &s;
        }
        if (arg == "-h" || arg == "--help") {
            std::fputs(usage().c_str(), stdout);
            std::exit(0);
        } else if (spec) {
            try {
                validation::setOption(a.vopt, *spec, parseDouble(value(i, opt)));
            } catch (const std::invalid_argument& e) {
                throw UsageError(e.what());
            }
        } else if (opt == "--fifo-dir") {
            a.fifo_dir = value(i, opt);
        } else if (opt == "--log") {
            a.log = value(i, opt);
        } else if (opt == "--timeout") {
            a.timeout = parseDouble(value(i, opt));
        } else if (opt == "--pcap") {
            a.pcap = value(i, opt);
        } else if (opt == "--search") {
            a.search = value(i, opt);
            a.search_set = true;
        } else if (opt == "--frames") {
            a.frames = static_cast<int>(parseInt(value(i, opt)));
        } else if (opt == "--out") {
            a.out = value(i, opt);
        } else if (opt == "--seconds") {
            a.seconds = parseDouble(value(i, opt));
        } else if (opt == "--json") {
            a.json = value(i, opt);
        } else if (opt == "--protocol-log") {
            a.protocol_log = value(i, opt);
        } else if (opt == "--log-dir") {
            a.log_dir = value(i, opt);
        } else if (arg == "--all") {
            a.all = true;
        } else if (arg == "--runnable") {
            a.runnable = true;
        } else if (arg == "--uvm") {
            a.uvm = true;
        } else if (arg == "--list") {
            a.list = true;
        } else if (arg == "--spawn-sim") {
            a.spawn_sim = true;
        } else if (arg == "--params") {
            a.params = true;
        } else if (arg == "--tree") {
            a.tree = true;
        } else if (arg == "--inject-crc") {
            a.inject_crc = true;
        } else if (arg.size() > 1 && arg[0] == '-' && !std::isdigit(static_cast<unsigned char>(arg[1]))) {
            throw UsageError("unrecognized argument: " + arg);
        } else if (a.cmd.empty()) {
            if (!commands.count(arg)) throw UsageError("invalid command: '" + arg + "'");
            a.cmd = arg;
        } else {
            a.positional.push_back(arg);
        }
    }
    if (a.cmd.empty()) throw UsageError("a command is required");
    return a;
}

// -- helpers ----------------------------------------------------------------------
std::string defaultXmlPath() {
    if (const char* env = std::getenv("CXP_XML")) return env;
    return CXP_DEFAULT_XML;
}

bool fileExists(const std::string& path) {
    struct stat st{};
    return ::stat(path.c_str(), &st) == 0;
}

std::unique_ptr<HostSession> makeHost(const Args& args) {
    SessionConfig cfg;
    cfg.fifo_dir = args.fifo_dir;
    cfg.spawn_vcam = args.spawn_sim;
    if (args.inject_crc) cfg.vcam.inject_crc_every = 7;
    // The hw RTL env runs forever; the host bounds a run by timing out the
    // wait for a device control ack.
    cfg.ack_timeout_ms = static_cast<int>(args.timeout * 1000);
    cfg.retries = 3;
    cfg.pcap = args.pcap;
    cfg.protocol_log = args.protocol_log;
    return std::make_unique<HostSession>(cfg);
}

void printParams(const NodeTree& tree, const std::map<std::string, ParamResult>& values) {
    std::printf("%s %s (GenICam schema %s) -- %zu parameters\n\n", tree.vendor_name.c_str(),
                tree.model_name.c_str(), tree.schema_version.c_str(), values.size());
    for (const auto& [name, v] : values) {
        Feature* feat = tree.find(name);
        const char* kind = feat ? featureKindName(feat->kind) : "?";
        if (v.ok()) {
            std::printf("  %-28s <%-11s> = %s\n", name.c_str(), kind, v.value->repr().c_str());
        } else {
            std::printf("  %-28s <%-11s> !! %s\n", name.c_str(), kind, v.error.c_str());
        }
    }
}

// -- commands -----------------------------------------------------------------------
int cmdSim(const Args& args) {
    auto [h2c, c2h] = makeFifoPair(args.fifo_dir);
    VirtualCameraConfig cfg;
    cfg.xml = defaultCameraXml();
    VirtualCamera vcam(std::make_shared<FifoEndpoint>(c2h, h2c, true, "vcam"), cfg);
    Logger("cli").info("virtual camera serving on %s", args.fifo_dir.c_str());
    vcam.runUntil(g_interrupted);
    return 0;
}

int cmdGui(const Args& args, const char* argv0) {
    std::string self(argv0);
    char buf[4096];
    ssize_t n = ::readlink("/proc/self/exe", buf, sizeof buf - 1);
    if (n > 0) self.assign(buf, static_cast<size_t>(n));
    std::string dir = self.substr(0, self.find_last_of('/') + 1);
    std::string gui = dir + "cxp-gui";
    std::string fifo_arg = "--fifo-dir=" + args.fifo_dir;
    ::execl(gui.c_str(), gui.c_str(), fifo_arg.c_str(), static_cast<char*>(nullptr));
    ::execlp("cxp-gui", "cxp-gui", fifo_arg.c_str(), static_cast<char*>(nullptr));
    std::fprintf(stderr, "cannot launch cxp-gui (looked in %s and $PATH)\n", dir.c_str());
    return 1;
}

int cmdDiscover(const Args& args) {
    auto s = makeHost(args);
    DeviceInfo info = s->host()->connect();
    std::printf("Vendor : %s\n", info.vendor_name.c_str());
    std::printf("Model  : %s\n", info.model_name.c_str());
    std::printf("Revision: 0x%08X\n", info.revision);
    std::printf("XML URL : %s\n", info.xml_url.c_str());
    std::printf("LinkID  : %u\n", info.device_link_id);
    if (args.params) {
        auto tree = s->host()->xmlTree();
        if (!tree) {
            std::fprintf(stderr, "\nno GenICam XML available on the device\n");
        } else {
            std::printf("\n");
            printParams(*tree, s->host()->parameters());
        }
    }
    return 0;
}

int cmdParams(const Args& args) {
    auto s = makeHost(args);
    s->host()->connect();  // discovery reads the XML + params
    auto tree = s->host()->xmlTree();
    if (!tree) {
        std::fprintf(stderr, "no GenICam XML available on the device\n");
        return 1;
    }
    printParams(*tree, s->host()->parameters());
    return 0;
}

int cmdRead(const Args& args) {
    if (args.positional.empty() || args.positional.size() > 2) {
        throw UsageError("read expects ADDR [N]");
    }
    uint32_t addr = static_cast<uint32_t>(parseInt(args.positional[0]));
    uint32_t n = args.positional.size() > 1 ? static_cast<uint32_t>(parseInt(args.positional[1])) : 1;
    auto s = makeHost(args);
    s->host()->connect();
    auto vals = s->host()->readReg(addr, n);
    for (size_t i = 0; i < vals.size(); ++i) {
        std::printf("0x%08X: 0x%08X (%u)\n", static_cast<uint32_t>(addr + 4 * i), vals[i], vals[i]);
    }
    return 0;
}

int cmdWrite(const Args& args) {
    if (args.positional.size() != 2) throw UsageError("write expects ADDR VALUE");
    uint32_t addr = static_cast<uint32_t>(parseInt(args.positional[0]));
    uint32_t value = static_cast<uint32_t>(parseInt(args.positional[1]));
    auto s = makeHost(args);
    s->host()->connect();
    s->host()->writeReg(addr, {value});
    std::printf("wrote 0x%08X -> 0x%08X\n", value, addr);
    return 0;
}

int cmdXmlParse(const Args& args) {
    if (args.positional.size() != 1) throw UsageError("xml-parse expects FILE");
    auto tree = NodeTree::fromFile(args.positional[0]);
    std::printf("Model=%s Vendor=%s schema=%s features=%zu\n", tree->model_name.c_str(),
                tree->vendor_name.c_str(), tree->schema_version.c_str(),
                tree->features().size());
    if (args.search_set) {
        for (Feature* f : tree->search(args.search)) {
            std::printf("  %-24s <%s> %s\n", f->name.c_str(), featureKindName(f->kind),
                        f->description.c_str());
        }
    }
    if (args.tree || !args.search_set) std::printf("%s\n", tree->treeText().c_str());
    return 0;
}

struct StreamResult {
    std::unique_ptr<HostSession> session;
    StreamParser parser;
    std::vector<FramePtr> images;
};

std::unique_ptr<StreamResult> runStream(const Args& args, int want_frames) {
    auto r = std::make_unique<StreamResult>();
    r->session = makeHost(args);
    auto& host = *r->session->host();
    host.connect();
    host.writeReg(R_TPG_RUN, {0});    // a FrameCount burst, not Continuous
    host.writeReg(R_FRAMECNT, {static_cast<uint32_t>(want_frames)});
    host.writeReg(R_ACQ_START, {1});
    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(static_cast<int64_t>(args.timeout * 1000));
    while (static_cast<int>(r->images.size()) < want_frames &&
           std::chrono::steady_clock::now() < deadline && !g_interrupted) {
        Words frame;
        if (!host.streamQueue().pop(frame, 1000)) break;
        for (auto& img : r->parser.feedFrame(frame)) r->images.push_back(img);
    }
    return r;
}

int cmdStreamCapture(const Args& args) {
    auto r = runStream(args, args.frames);
    std::string out = args.out.empty() ? "captured" : args.out;
    ::mkdir(out.c_str(), 0777);
    for (const auto& img : r->images) {
        std::string p = out + strprintf("/frame_%04u.pgm", img->header.frame_id);
        std::printf("saved %s\n", img->save(p).c_str());
    }
    const auto& s = r->parser.stats;
    std::printf("frames=%llu stream_pkts=%llu crc_err=%llu missing_pkts=%llu\n",
                (unsigned long long)s.images_completed, (unsigned long long)s.stream_pkts,
                (unsigned long long)s.crc_errors, (unsigned long long)s.missing_packets);
    return 0;
}

int cmdImageExtract(const Args& args) {
    auto r = runStream(args, 1);
    if (r->images.empty()) {
        std::fprintf(stderr, "no image received\n");
        return 1;
    }
    std::printf("saved %s\n", r->images[0]->save(args.out.empty() ? "frame.pgm" : args.out).c_str());
    return 0;
}

int cmdPacketDump(const Args& args) {
    auto r = runStream(args, args.frames);
    std::printf("%s\n", r->parser.stats.str().c_str());
    return 0;
}

int cmdCompliance(const Args& args) {
    auto r = runStream(args, 2);
    DeviceInfo info = r->session->host()->discover();
    std::unique_ptr<NodeTree> tree;
    const std::string xml = defaultXmlPath();
    if (fileExists(xml)) tree = NodeTree::fromFile(xml);
    ComplianceChecker chk;
    chk.checkDevice(info);
    chk.checkParserStats(r->parser.stats);
    chk.checkSfnc(tree.get());
    chk.checkErrorHandling(args.inject_crc
                               ? std::optional<bool>(r->parser.stats.crc_errors > 0)
                               : std::nullopt);
    const auto& rep = chk.finalize();
    std::printf("%s\n", rep.render().c_str());
    return rep.passed() ? 0 : 2;
}

int cmdTrace(Args args) {
    if (args.pcap.empty()) args.pcap = "cxp_trace.pcap";
    auto s = makeHost(args);
    s->host()->connect();
    s->host()->startHeartbeat();
    auto end = std::chrono::steady_clock::now() +
               std::chrono::milliseconds(static_cast<int64_t>(args.seconds * 1000));
    while (std::chrono::steady_clock::now() < end && !g_interrupted) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    std::printf("trace written to %s\n", args.pcap.c_str());
    return 0;
}

// Validation plan: list the catalogue, or run cases against the device and
// print each check line.  The run itself is a validation::Campaign: every
// frame of a case, with its check lines and verdict, goes to
// <log dir>/<ID>.log and the results to results.json there; the connect-time
// discovery goes to connect.log.  Exit 0 when nothing FAILs or ERRORs.
int cmdValidate(Args args) {
    namespace v = cxp::validation;
    const v::Catalogue cat = v::loadCatalogue(v::defaultCataloguePath());
    std::vector<const v::CaseDef*> pick;
    const bool every = args.all || args.list || (args.positional.empty() && (args.runnable || args.uvm));
    for (const auto& c : cat.cases) {
        bool want = every;
        for (const auto& id : args.positional) {
            want |= c.id == id || (c.id.size() > id.size() && c.id.compare(c.id.size() - id.size(), id.size(), id) == 0);
        }
        if (args.runnable && !c.emulator.runnable) want = false;
        if (args.uvm && !c.isUvm()) want = false;
        if (want) pick.push_back(&c);
    }
    if (args.list) {
        std::printf("run options: %s\n", v::describeOptions(args.vopt).c_str());
        int w = 18;
        for (const auto* c : pick) w = std::max(w, int(c->id.size()));
        for (const auto* c : pick) {
            const std::string scale =
                c->emulator.runnable && c->emulator.timeout_scale != 1.0 ? strprintf("x%g", c->emulator.timeout_scale) : "";
            std::printf("%-*s %-4s %-5s %s\n", w, c->id.c_str(), c->emulator.runnable ? "run" : "-", scale.c_str(),
                        c->title.c_str());
            if (!c->emulator.params.empty()) {
                std::printf("%-*s %s\n", w + 11, "", v::describeParams(c->emulator.params).c_str());
            }
        }
        return 0;
    }
    if (pick.empty()) throw UsageError("validate expects case IDs, --all, --runnable, --uvm or --list");

    v::CampaignConfig cfg;
    cfg.log_dir = v::makeRunLogDir(args.log_dir, "validation_logs", args.vopt.keep_logs);
    cfg.results_path = args.json;
    cfg.device = "cxp host session";
    cfg.options = args.vopt;
    if (args.protocol_log.empty()) args.protocol_log = cfg.log_dir + "/connect.log";
    std::printf("protocol logs: %s/\n", cfg.log_dir.c_str());

    auto s = makeHost(args);
    s->host()->protocolLog()->event("connect: discovery and XML read");
    s->connect();
    // No GUI drain thread here: keep the stream queue from growing.
    s->startDrain();

    struct Console : v::CampaignObserver {
        void caseStarted(size_t i, const v::CaseDef& c) override {
            if (i) std::printf("\n");
            std::printf("== %s  %s\n", c.id.c_str(), c.title.c_str());
            std::fflush(stdout);
        }
        void caseLine(size_t, const v::CaseDef&, const v::LogLine& l) override {
            std::printf("   %s%s\n", v::lineTag(l.kind), l.text.c_str());
            std::fflush(stdout);
        }
        void caseDone(size_t, const v::CaseDef&, const v::CaseResult& r) override {
            std::printf("   -> %s: %s (%.0f ms)\n", v::verdictName(r.verdict), r.summary.c_str(), r.duration_ms);
            std::fflush(stdout);
        }
        void deviceLost(size_t, const v::CaseDef&) override {
            std::printf("   !! the device no longer answers a read of Standard\n");
        }
        void finished(const v::CampaignSummary& sum) override {
            std::printf("\nsummary:");
            for (const auto& [k, n] : sum.counts) std::printf("  %s %d", k.c_str(), n);
            std::printf("\n");
            if (!sum.dead_after.empty()) std::printf("the device stopped answering after %s\n", sum.dead_after.c_str());
            std::printf("results written to %s\n", sum.results_path.c_str());
        }
    } console;
    v::Campaign campaign(cat, pick, cfg);
    const v::CampaignSummary sum = campaign.run(s->host(), g_interrupted, &console);
    s->stopDrain();
    return sum.failures() ? 2 : 0;
}

}  // namespace

int main(int argc, char** argv) {
    Args args;
    try {
        args = parseArgs(argc, argv);
    } catch (const UsageError& e) {
        std::fprintf(stderr, "%scxp: error: %s\n", usage().c_str(), e.what());
        return 2;
    }
    logging::setLevel(parseLogLevel(args.log));
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);
    try {
        const std::string& c = args.cmd;
        if (c == "sim") return cmdSim(args);
        if (c == "gui") return cmdGui(args, argv[0]);
        if (c == "discover") return cmdDiscover(args);
        if (c == "params") return cmdParams(args);
        if (c == "read") return cmdRead(args);
        if (c == "write") return cmdWrite(args);
        if (c == "xml-parse") return cmdXmlParse(args);
        if (c == "stream-capture") return cmdStreamCapture(args);
        if (c == "packet-dump") return cmdPacketDump(args);
        if (c == "image-extract") return cmdImageExtract(args);
        if (c == "compliance") return cmdCompliance(args);
        if (c == "trace") return cmdTrace(args);
        if (c == "validate") return cmdValidate(args);
    } catch (const UsageError& e) {
        std::fprintf(stderr, "%scxp: error: %s\n", usage().c_str(), e.what());
        return 2;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "cxp: %s\n", e.what());
        return 1;
    }
    return 2;
}
