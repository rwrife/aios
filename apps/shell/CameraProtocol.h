#pragma once
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMap>
#include <QSet>
#include <QRegularExpression>
#include <QStringList>
#include <cmath>
#include <time.h>

// Pure protocol state; no UI, broker, authentication or filesystem operations.
class CameraProtocol {
public:
    enum Decision { Accept, Drop, Invalid };
    static double monotonic() {
        timespec value{};
        clock_gettime(CLOCK_MONOTONIC, &value);
        return double(value.tv_sec) + double(value.tv_nsec) / 1e9;
    }
    static bool keys(const QJsonObject &value, const QStringList &expected) {
        auto actual = value.keys(); auto sorted = expected; sorted.sort();
        return actual == sorted;
    }
    static bool integer(const QJsonValue &value, double minimum = 0, double maximum = 9007199254740991.) {
        const double number = value.toDouble(-1);
        return value.isDouble() && std::isfinite(number) && number >= minimum && number <= maximum && std::floor(number) == number;
    }
    static bool number(const QJsonValue &value) {
        return value.isDouble() && std::isfinite(value.toDouble()) && value.toDouble() >= 0;
    }
    static bool id(const QString &value) {
        static const QRegularExpression pattern("^[a-f0-9]{32}$");
        return pattern.match(value).hasMatch();
    }
    static bool uuid(const QString &value) {
        static const QRegularExpression pattern("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$");
        return pattern.match(value).hasMatch();
    }
    static bool uniqueKeys(const QByteArray &raw) {
        // QJsonDocument validates grammar; this pass rejects duplicate decoded
        // keys (including escaped spellings) before QJson can collapse them.
        QList<QSet<QString>> objects;
        for (qsizetype i = 0; i < raw.size(); ++i) {
            if (raw[i] == '{' || raw[i] == '[') {
                objects.append(QSet<QString>());
                if (objects.size() > 16) return false;
            } else if (raw[i] == '}' || raw[i] == ']') {
                if (objects.isEmpty()) return false;
                objects.removeLast();
            } else if (raw[i] == '"') {
                const auto start = i++;
                while (i < raw.size() && raw[i] != '"') { if (raw[i] == '\\') ++i; ++i; }
                if (i >= raw.size()) return false;
                auto next = i + 1;
                while (next < raw.size() && (raw[next] == ' ' || raw[next] == '\t' || raw[next] == '\r' || raw[next] == '\n')) ++next;
                if (next < raw.size() && raw[next] == ':') {
                    if (objects.isEmpty()) return false;
                    const auto key = QJsonDocument::fromJson("[" + raw.mid(start, i - start + 1) + "]").array().at(0).toString();
                    if (objects.last().contains(key)) return false;
                    objects.last().insert(key);
                }
            }
        }
        return objects.isEmpty();
    }
    static bool base64(const QString &value, int maximum, QByteArray *decoded = nullptr) {
        if (value.size() > maximum) return false;
        const auto bytes = value.toLatin1();
        const auto data = QByteArray::fromBase64(bytes);
        if (data.toBase64() != bytes) return false;
        if (decoded) *decoded = data;
        return true;
    }
    static bool portrait(const QJsonValue &value) {
        if (!value.isString()) return false;
        const auto text = value.toString();
        if (text.isEmpty()) return true;
        const QString prefix = "data:image/png;base64,";
        QByteArray bytes;
        if (!text.startsWith(prefix) || !base64(text.mid(prefix.size()), 20000, &bytes) || bytes.size() < 24) return false;
        return bytes.left(8) == QByteArray::fromHex("89504e470d0a1a0a") && bytes.mid(12, 12) == QByteArray::fromHex("494844520000004000000040");
    }
    void track(const QString &request, const QString &consumer, const QString &mode) {
        if (jobs.size() >= 8) { jobs.clear(); }
        jobs.insert(request, Job{consumer, mode, 0, 0, false});
    }
    void release(const QString &consumer) {
        for (auto it = jobs.begin(); it != jobs.end();) {
            if (it->consumer == consumer) { remember(it.key()); it = jobs.erase(it); }
            else ++it;
        }
    }
    void gate(bool active, bool secure) { eligible = active && !secure; }
    double generation() const { return currentGeneration; }
    Decision accept(const QJsonObject &event, double now) {
        if (!keys(event, {"version", "event", "request", "consumer", "generation", "sequence", "captured_at", "processed_at", "reason", "payload"}) ||
                !integer(event["version"], 1, 1) || !integer(event["generation"], 1) || !integer(event["sequence"], 0, 1000000) ||
                !number(event["captured_at"]) || !number(event["processed_at"]) || !event["payload"].isObject()) return Invalid;
        for (const auto &field : {"event", "request", "consumer", "reason"}) if (!event[field].isString()) return Invalid;
        const auto kind = event["event"].toString(), request = event["request"].toString(), consumer = event["consumer"].toString();
        const auto reason = event["reason"].toString();
        static const QStringList reasons{"ok", "completed", "cancelled", "device_changed", "gate_changed", "configuration_changed", "shutdown", "preempted",
            "inactive", "consent_required", "busy", "cooldown", "unavailable", "timeout", "worker_exit", "invalid_result", "oversized"};
        if (!reasons.contains(reason) || !QStringList{"state", "error", "cancelled", "preview", "photo", "progress", "result"}.contains(kind)) return Invalid;
        const auto generation = event["generation"].toDouble(), sequence = event["sequence"].toDouble();
        const auto captured = event["captured_at"].toDouble(), processed = event["processed_at"].toDouble();
        if (generation < currentGeneration || processed > now || now - processed > 3 ||
                (captured && (captured > processed || processed - captured > 3))) return Invalid;
        const auto payload = event["payload"].toObject();
        if (request.isEmpty() && consumer.isEmpty()) {
            if (kind != "state" || sequence != 0 || captured != 0 || !state(payload)) return Invalid;
            currentGeneration = generation;
            return Accept;
        }
        if (!id(request) || !id(consumer)) return Invalid;
        if (!jobs.contains(request)) {
            if (retired.contains(request)) return Drop;
            if (consumer != QString(32, '0') || kind != "state" || payload["state"] != "capturing") return Invalid;
            if (!eligible) { remember(request); return Drop; }
            track(request, consumer, "recognize");
        }
        auto &job = jobs[request];
        if (job.consumer != consumer) return Invalid;
        if (kind == "state") {
            if (job.started || !state(payload) || sequence != 0 || captured != 0 ||
                payload["state"] != (job.mode == "enroll" ? "enrolling" : "capturing")) return Invalid;
            job.started = true; job.generation = generation;
        } else if (kind == "error" || kind == "cancelled") {
            if (!payload.isEmpty() || sequence != 0 || captured != 0) return Invalid;
            remember(request); jobs.remove(request);
        } else {
            if (!job.started || generation != job.generation || sequence < job.sequence ||
                    (kind != "result" && sequence == job.sequence)) return Invalid;
            if (!payloadValid(kind, job.mode, payload, captured, now, sequence)) return Invalid;
            job.sequence = sequence;
            const bool suppressed = job.mode == "recognize" && !eligible;
            if (kind == "result" || kind == "photo") { remember(request); jobs.remove(request); }
            currentGeneration = generation;
            return suppressed ? Drop : Accept;
        }
        currentGeneration = generation;
        return Accept;
    }
private:
    struct Job { QString consumer, mode; double sequence, generation; bool started; };
    QMap<QString, Job> jobs;
    QStringList retired;
    double currentGeneration = 0;
    bool eligible = false;
    void remember(const QString &request) { retired.append(request); if (retired.size() > 128) retired.removeFirst(); }
    static bool state(const QJsonObject &payload) {
        return keys(payload, {"state"}) && payload["state"].isString() &&
            QStringList{"disabled", "ready", "capturing", "enrolling", "manual-only", "unavailable"}.contains(payload["state"].toString());
    }
    static bool payloadValid(const QString &kind, const QString &mode, const QJsonObject &payload, double captured, double now, double sequence) {
        if (kind == "preview") {
            const auto value = payload["image"].toString();
            return mode == "preview" && captured > 0 && sequence > 0 && keys(payload, {"image"}) &&
                value.startsWith("data:image/jpeg;base64,") && base64(value.mid(23), 240000);
        }
        if (kind == "photo") {
            QByteArray rgb;
            return mode == "photo" && captured > 0 && sequence > 0 && keys(payload, {"image", "rgb"}) &&
                portrait(payload["image"]) && !payload["image"].toString().isEmpty() && payload["rgb"].isString() &&
                base64(payload["rgb"].toString(), 16384, &rgb) && rgb.size() == 12288;
        }
        if (kind == "progress") return mode == "enroll" && captured > 0 && keys(payload, {"samples", "target", "reason"}) &&
            integer(payload["samples"], 0, 3) && integer(payload["target"], 3, 3) && payload["reason"].isString() &&
            QStringList{"look_straight", "turn_slightly", "turn_other_way", "improve_light_or_hold_still", "one_person_only", "face_camera", "sample_accepted"}.contains(payload["reason"].toString());
        if (kind != "result") return false;
        if (mode == "enroll") return keys(payload, {"enrolled"}) && payload["enrolled"].isString() && uuid(payload["enrolled"].toString()) && captured > 0 && sequence >= 3;
        if (mode == "purge" || mode == "preview") return keys(payload, {"state"}) && payload["state"] == (mode == "purge" ? "purged" : "manual-only");
        if (mode != "recognize" || !payload["state"].isString() ||
            !QStringList{"disabled", "ready", "manual-only", "unavailable"}.contains(payload["state"].toString())) return false;
        for (const auto &key : payload.keys()) if (!QStringList{"state", "reason", "suggestion"}.contains(key)) return false;
        if (payload.contains("reason") && (!payload["reason"].isString() ||
            !QStringList{"no-enrollment", "unknown-or-ambiguous", "deleted", "configuration-changed"}.contains(payload["reason"].toString()))) return false;
        if (!payload.contains("suggestion") || payload["suggestion"].isNull()) return true;
        if (!payload["suggestion"].isObject() || payload["state"] != "ready" || captured <= 0 || sequence < 3) return false;
        const auto candidate = payload["suggestion"].toObject();
        static const QRegularExpression name("^[^\\x00-\\x1f]{1,80}$");
        return keys(candidate, {"id", "name", "photo", "confidence", "expires_at"}) && candidate["id"].isString() && uuid(candidate["id"].toString()) &&
            candidate["name"].isString() && name.match(candidate["name"].toString()).hasMatch() && portrait(candidate["photo"]) &&
            candidate["confidence"] == "candidate" && number(candidate["expires_at"]) &&
            candidate["expires_at"].toDouble() == captured + 5 && candidate["expires_at"].toDouble() > now;
    }
};
