#include <QCoreApplication>
#include <cstdio>
#include <cstdlib>
#include "CameraProtocol.h"

static const QString request(32, '1'), consumer(32, '2');
static QJsonObject envelope(QString kind = "state", QJsonObject payload = {{"state", "capturing"}}) {
    return {{"version", 1}, {"event", kind}, {"request", request}, {"consumer", consumer},
            {"generation", 1}, {"sequence", 0}, {"captured_at", 0}, {"processed_at", 100.}, {"reason", "ok"}, {"payload", payload}};
}
static QJsonObject result() {
    auto event = envelope("result", {{"state", "ready"}, {"suggestion", QJsonObject{
        {"id", "12345678-1234-1234-1234-123456789012"}, {"name", "Example"}, {"photo", ""},
        {"confidence", "candidate"}, {"expires_at", 104.9}}}});
    event["sequence"] = 3; event["captured_at"] = 99.9;
    return event;
}
static CameraProtocol started() {
    CameraProtocol protocol;
    protocol.gate(true, false);
    protocol.track(request, consumer, "recognize");
    if (protocol.accept(envelope(), 100.) != CameraProtocol::Accept) std::abort();
    return protocol;
}
int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    int failures = 0;
    auto check = [&](bool ok, const char *message) { if (!ok) { std::fprintf(stderr, "%s\n", message); ++failures; } };
    auto protocol = started();
    check(protocol.accept(result(), 100.1) == CameraProtocol::Accept, "fresh correlated candidate rejected");
    check(protocol.accept(result(), 100.2) == CameraProtocol::Drop, "replayed candidate accepted");
    check(protocol.accept(envelope(), 100.2) == CameraProtocol::Drop, "replayed request accepted");
    auto malformed = [&](QJsonObject event, const char *message) {
        auto validator = started(); check(validator.accept(event, 100.1) == CameraProtocol::Invalid, message);
    };
    for (const auto &field : {"version", "generation", "sequence", "captured_at", "processed_at"}) {
        auto event = result(); event[field] = true; malformed(event, "boolean accepted as a number");
        event = result(); event.remove(field); malformed(event, "missing envelope field accepted");
    }
    for (double value : {-1., .5}) { auto event = result(); event["sequence"] = value; malformed(event, "invalid sequence accepted"); }
    auto event = result(); event["generation"] = 0; malformed(event, "old generation accepted");
    event = result(); event["captured_at"] = 96.; malformed(event, "stale capture accepted");
    event = result(); event["captured_at"] = 101.; malformed(event, "future capture accepted");
    event = result(); event["processed_at"] = 101.; malformed(event, "future processing time accepted");
    event = result(); event["consumer"] = QString(32, '3'); malformed(event, "wrong consumer accepted");
    event = result(); event["request"] = QString(32, '3'); malformed(event, "unsolicited result accepted");
    event = result(); event["embedding"] = QJsonArray{1, 2}; malformed(event, "private envelope field accepted");
    event = result(); event["reason"] = "private driver text"; malformed(event, "unbounded reason accepted");
    for (const auto &field : {"embedding", "landmarks", "candidates", "score", "live", "capability", "session", "owner"}) {
        event = result(); auto payload = event["payload"].toObject(); payload[field] = QJsonArray{1, 2}; event["payload"] = payload;
        malformed(event, "private result field accepted");
    }
    for (const auto &field : {"id", "name", "confidence", "expires_at"}) {
        event = result(); auto payload = event["payload"].toObject(); auto candidate = payload["suggestion"].toObject();
        candidate[field] = "invalid"; payload["suggestion"] = candidate; event["payload"] = payload;
        // A different plain display name is allowed.
        if (QString(field) != "name") malformed(event, "invalid candidate field accepted");
    }
    event = result(); auto payload = event["payload"].toObject(); auto candidate = payload["suggestion"].toObject();
    candidate["photo"] = "https://remote.example/private.png"; payload["suggestion"] = candidate; event["payload"] = payload;
    malformed(event, "remote portrait URL accepted");
    protocol = started(); protocol.gate(true, true);
    check(protocol.accept(result(), 100.1) == CameraProtocol::Drop, "candidate accepted during secure input");
    protocol = started(); protocol.release(consumer);
    check(protocol.accept(result(), 100.1) == CameraProtocol::Drop, "cancelled request accepted");
    protocol = CameraProtocol(); protocol.gate(true, false);
    event = envelope(); event["consumer"] = QString(32, '0');
    check(protocol.accept(event, 100.) == CameraProtocol::Accept, "background start rejected");
    event = result(); event["consumer"] = QString(32, '0');
    check(protocol.accept(event, 100.1) == CameraProtocol::Accept, "correlated background result rejected");
    protocol = CameraProtocol(); protocol.track(request, consumer, "enroll");
    check(protocol.accept(envelope("state", {{"state", "enrolling"}}), 100.) == CameraProtocol::Accept,
          "enrollment start rejected");
    event = envelope("preview", {{"image", "data:image/jpeg;base64,AA=="}});
    event["sequence"] = 1; event["captured_at"] = 99.9;
    check(protocol.accept(event, 100.1) == CameraProtocol::Accept, "enrollment preview rejected");
    event = envelope("progress", {{"samples", 10}, {"target", 10}, {"reason", "burst_capture"}});
    event["sequence"] = 2; event["captured_at"] = 99.9;
    check(protocol.accept(event, 100.1) == CameraProtocol::Accept, "fixed burst progress rejected");
    event["sequence"] = 3; payload = event["payload"].toObject(); payload["samples"] = 11; event["payload"] = payload;
    check(protocol.accept(event, 100.1) == CameraProtocol::Invalid, "excess burst samples accepted");
    check(!CameraProtocol::uniqueKeys("{\"a\":1,\"a\":2}"), "duplicate key accepted");
    check(!CameraProtocol::uniqueKeys("{\"x\":{\"a\":1,\"\\u0061\":2}}"), "escaped duplicate key accepted");
    check(CameraProtocol::uniqueKeys("{\"a\":{\"a\":1},\"b\":[{\"a\":2}]}"), "separate object keys rejected");
    QByteArray deep(17, '['); deep += QByteArray(17, ']');
    check(!CameraProtocol::uniqueKeys(deep), "excessive nesting accepted");
    return failures ? 1 : 0;
}
