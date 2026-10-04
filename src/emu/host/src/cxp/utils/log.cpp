#include "cxp/utils/log.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstdio>
#include <ctime>
#include <mutex>
#include <sys/time.h>
#include <utility>
#include <vector>

namespace cxp {

namespace {

struct LogState {
    std::mutex mu;
    std::vector<std::pair<int, LogSink>> sinks;
    int next_handle = 1;
    bool to_stderr = true;
};

LogState& state() {
    static LogState s;
    return s;
}

std::atomic<int> g_level{static_cast<int>(LogLevel::Info)};

}  // namespace

const char* logLevelName(LogLevel lvl) {
    switch (lvl) {
    case LogLevel::Debug:   return "DEBUG";
    case LogLevel::Info:    return "INFO";
    case LogLevel::Warning: return "WARNING";
    case LogLevel::Error:   return "ERROR";
    }
    return "INFO";
}

LogLevel parseLogLevel(const std::string& s) {
    std::string u(s);
    std::transform(u.begin(), u.end(), u.begin(),
                   [](unsigned char c) { return std::toupper(c); });
    if (u == "DEBUG") return LogLevel::Debug;
    if (u == "WARNING" || u == "WARN") return LogLevel::Warning;
    if (u == "ERROR") return LogLevel::Error;
    return LogLevel::Info;
}

namespace logging {

void setLevel(LogLevel lvl) { g_level = static_cast<int>(lvl); }
LogLevel level() { return static_cast<LogLevel>(g_level.load()); }

void setStderr(bool on) {
    std::lock_guard<std::mutex> lk(state().mu);
    state().to_stderr = on;
}

int addSink(LogSink sink) {
    std::lock_guard<std::mutex> lk(state().mu);
    int h = state().next_handle++;
    state().sinks.emplace_back(h, std::move(sink));
    return h;
}

void removeSink(int handle) {
    std::lock_guard<std::mutex> lk(state().mu);
    auto& v = state().sinks;
    v.erase(std::remove_if(v.begin(), v.end(),
                           [handle](const auto& p) { return p.first == handle; }),
            v.end());
}

}  // namespace logging

std::string vstrprintf(const char* fmt, va_list ap) {
    va_list ap2;
    va_copy(ap2, ap);
    char small[512];
    int n = std::vsnprintf(small, sizeof small, fmt, ap2);
    va_end(ap2);
    if (n < 0) return {};
    if (static_cast<size_t>(n) < sizeof small) return std::string(small, n);
    std::string out(static_cast<size_t>(n) + 1, '\0');
    std::vsnprintf(&out[0], out.size(), fmt, ap);
    out.resize(n);
    return out;
}

std::string strprintf(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    std::string s = vstrprintf(fmt, ap);
    va_end(ap);
    return s;
}

Logger::Logger(const std::string& name)
    : name_(name.empty() ? "cxp" : "cxp." + name) {}

bool Logger::enabled(LogLevel lvl) const {
    return static_cast<int>(lvl) >= g_level.load();
}

void Logger::log(LogLevel lvl, const std::string& msg) const {
    if (!enabled(lvl)) return;
    timeval tv{};
    gettimeofday(&tv, nullptr);
    std::tm tm{};
    localtime_r(&tv.tv_sec, &tm);
    char ts[16];
    std::strftime(ts, sizeof ts, "%H:%M:%S", &tm);
    std::string line = strprintf("%s %-7s %s: %s", ts, logLevelName(lvl),
                                 name_.c_str(), msg.c_str());
    std::lock_guard<std::mutex> lk(state().mu);
    if (state().to_stderr) {
        std::fprintf(stderr, "%s\n", line.c_str());
    }
    for (auto& s : state().sinks) {
        s.second(line, lvl);
    }
}

void Logger::vlog(LogLevel lvl, const char* fmt, va_list ap) const {
    if (!enabled(lvl)) return;
    log(lvl, vstrprintf(fmt, ap));
}

#define CXP_LOG_METHOD(meth, lvl)                    \
    void Logger::meth(const char* fmt, ...) const {  \
        va_list ap;                                  \
        va_start(ap, fmt);                           \
        vlog(lvl, fmt, ap);                          \
        va_end(ap);                                  \
    }
CXP_LOG_METHOD(debug, LogLevel::Debug)
CXP_LOG_METHOD(info, LogLevel::Info)
CXP_LOG_METHOD(warning, LogLevel::Warning)
CXP_LOG_METHOD(error, LogLevel::Error)
#undef CXP_LOG_METHOD

}  // namespace cxp
