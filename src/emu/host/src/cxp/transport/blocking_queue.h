// Unbounded thread-safe FIFO with timed blocking pop.
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>

namespace cxp {

template <typename T>
class BlockingQueue {
public:
    void push(T v) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            q_.push_back(std::move(v));
        }
        cv_.notify_one();
    }

    void pushFront(T v) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            q_.push_front(std::move(v));
        }
        cv_.notify_one();
    }

    // Wait up to timeout_ms (< 0: forever).  Returns false on timeout.
    bool pop(T& out, int timeout_ms = -1) {
        std::unique_lock<std::mutex> lk(mu_);
        auto ready = [this] { return !q_.empty(); };
        if (timeout_ms < 0) {
            cv_.wait(lk, ready);
        } else if (!cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms), ready)) {
            return false;
        }
        out = std::move(q_.front());
        q_.pop_front();
        return true;
    }

    bool tryPop(T& out) { return pop(out, 0); }

    // Drop everything queued; returns how many items were discarded.
    size_t clear() {
        std::lock_guard<std::mutex> lk(mu_);
        size_t n = q_.size();
        q_.clear();
        return n;
    }

    size_t size() const {
        std::lock_guard<std::mutex> lk(mu_);
        return q_.size();
    }

private:
    mutable std::mutex mu_;
    std::condition_variable cv_;
    std::deque<T> q_;
};

}  // namespace cxp
