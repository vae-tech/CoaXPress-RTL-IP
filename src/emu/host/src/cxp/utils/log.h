// Logging: a tiny thread-safe named-logger facility.
//
// Mirrors cxp/utils/logging.py: every logger is a child of "cxp", lines go
// to stderr as "HH:MM:SS LEVEL   cxp.name: message", and extra sinks (the
// GUI log pane) can subscribe to receive a copy of each formatted record.
#pragma once

#include <cstdarg>
#include <functional>
#include <string>

namespace cxp {

enum class LogLevel { Debug = 10, Info = 20, Warning = 30, Error = 40 };

const char* logLevelName(LogLevel lvl);

// Parse "DEBUG"/"INFO"/"WARNING"/"ERROR" (case-insensitive); INFO on miss.
LogLevel parseLogLevel(const std::string& s);

using LogSink = std::function<void(const std::string& line, LogLevel lvl)>;

namespace logging {
void setLevel(LogLevel lvl);
LogLevel level();
void setStderr(bool on);
int addSink(LogSink sink);       // returns a handle for removeSink
void removeSink(int handle);
}  // namespace logging

std::string strprintf(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
std::string vstrprintf(const char* fmt, va_list ap);

class Logger {
public:
    explicit Logger(const std::string& name);

    void debug(const char* fmt, ...) const __attribute__((format(printf, 2, 3)));
    void info(const char* fmt, ...) const __attribute__((format(printf, 2, 3)));
    void warning(const char* fmt, ...) const __attribute__((format(printf, 2, 3)));
    void error(const char* fmt, ...) const __attribute__((format(printf, 2, 3)));

    void log(LogLevel lvl, const std::string& msg) const;
    bool enabled(LogLevel lvl) const;

private:
    void vlog(LogLevel lvl, const char* fmt, va_list ap) const;
    std::string name_;
};

}  // namespace cxp
