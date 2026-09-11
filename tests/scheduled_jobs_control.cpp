#include "ScheduledJobs.h"
#include <QCoreApplication>
#include <QDir>
#include <QEventLoop>
#include <QLocalServer>
#include <QTemporaryDir>
#include <cstdio>

static void wait(int milliseconds = 50) {
    QEventLoop loop;
    QTimer::singleShot(milliseconds, &loop, &QEventLoop::quit);
    loop.exec();
}

static int liveService() {
    ScheduledJobs bridge;
    QVariantMap response;
    QString expected;
    QEventLoop loop;
    QObject::connect(&bridge, &ScheduledJobs::completed, &loop,
                     [&](const QString &id, const QString &, const QVariantMap &value) {
        if (id == expected) { response = value; loop.quit(); }
    });
    const auto call = [&](const QString &id) {
        expected = id; response.clear();
        QTimer timeout;
        timeout.setSingleShot(true);
        QObject::connect(&timeout, &QTimer::timeout, &loop, &QEventLoop::quit);
        timeout.start(16000);
        loop.exec();
        if (response.value("status") != "ok") {
            std::fprintf(stderr, "Native service request failed: %s\n",
                         QJsonDocument::fromVariant(response).toJson(QJsonDocument::Compact).constData());
            return false;
        }
        return true;
    };
    if (!call(bridge.health())) return 21;
    if (!call(bridge.binding("Summarize the task"))) return 22;
    const auto binding = response.value("result").toMap();
    QVariantMap execution{{"provider", binding.value("provider")}, {"profile", binding.value("profile")},
                          {"model", binding.value("model")}, {"capabilities", QVariantList{}}};
    QVariantMap schedule{{"kind", "cron"}, {"value", "0 9 * * 1-5"}, {"zone", "UTC"}};
    if (!call(bridge.preview(schedule)) || response.value("result").toList().size() != 3) return 23;
    QVariantMap config{{"title", "Native bridge task"}, {"prompt", "Summarize the task"},
                       {"schedule", schedule}, {"execution", execution}};
    if (!call(bridge.create(config))) return 24;
    auto job = response.value("result").toMap();
    const auto id = job.value("id").toString();
    if (id.isEmpty() || job.value("next_occurrences").toList().size() != 3) return 25;
    if (!call(bridge.get(id))) return 26;
    if (!call(bridge.pause(id, job.value("revision").toLongLong()))) return 27;
    job = response.value("result").toMap();
    if (job.value("enabled").toBool()) return 28;
    config["title"] = "Native edited task";
    if (!call(bridge.update(id, job.value("revision").toLongLong(), config))) return 29;
    job = response.value("result").toMap();
    if (!call(bridge.resume(id, job.value("revision").toLongLong()))) return 30;
    job = response.value("result").toMap();
    const auto revision = job.value("revision").toLongLong();
    const auto receipt = bridge.newRequestId();
    if (!call(bridge.runNow(id, revision, receipt))) return 31;
    const auto run = response.value("result").toMap().value("id").toString();
    if (!call(bridge.runNow(id, revision, receipt))
            || response.value("result").toMap().value("id").toString() != run) return 32;
    bool succeeded = false;
    for (int attempt = 0; attempt < 100; ++attempt) {
        if (!call(bridge.readResult(run))) return 33;
        const auto value = response.value("result").toMap();
        if (value.value("state") == "succeeded") {
            succeeded = value.value("result") == "Saved background answer";
            break;
        }
        wait(100);
    }
    if (!succeeded) return 34;
    if (!call(bridge.unread()) || response.value("result").toList().isEmpty()) return 35;
    if (!call(bridge.acknowledgeResult(run))) return 36;
    if (!call(bridge.listRuns(id)) || response.value("result").toList().size() != 1) return 37;
    if (!call(bridge.remove(id, revision))) return 38;
    if (!call(bridge.list()) || !response.value("result").toList().isEmpty()) return 39;
    std::puts("Native bridge completed real-service binding, preview, CRUD, pause/resume, run-now dedup, worker result, unread/ack and history.");
    return 0;
}

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    if (app.arguments().contains("--live-service")) return liveService();
    QTemporaryDir runtime("/tmp/aios-jobs-test-XXXXXX");
    if (!runtime.isValid()) return 1;
    qputenv("XDG_RUNTIME_DIR", runtime.path().toUtf8());
    qunsetenv("AIOS_SESSION_SOCKET");
    qunsetenv("AIOS_SESSION_ID");
    qunsetenv("AIOS_BACKGROUND_RUN");
    qunsetenv("AIOS_PRINCIPAL");
    const auto directory = runtime.path() + "/aios-scheduler";
    if (!QDir().mkdir(directory)) return 2;
    QFile::setPermissions(directory, QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner);
    QLocalServer server;
    server.setSocketOptions(QLocalServer::UserAccessOption);
    if (!server.listen(directory + "/service.sock")) return 3;
    QJsonObject observed;
    QList<QLocalSocket *> peers;
    bool respond = true;
    QByteArray response = "{\"status\":\"ok\",\"result\":{\"available\":true}}\n";
    QObject::connect(&server, &QLocalServer::newConnection, &app, [&] {
        auto peer = server.nextPendingConnection();
        peers.append(peer);
        QObject::connect(peer, &QLocalSocket::readyRead, peer, [&, peer] {
            observed = QJsonDocument::fromJson(peer->readAll().trimmed()).object();
            if (respond) { peer->write(response); peer->flush(); }
        });
    });
    ScheduledJobs bridge;
    QVariantMap received;
    QString receivedId, receivedAction;
    int completions = 0;
    if (bridge.metaObject()->indexOfProperty("lease") >= 0
            || bridge.metaObject()->indexOfProperty("scope") >= 0
            || bridge.metaObject()->indexOfMethod("setProtectedContextProvider()") >= 0) return 17;
    QObject::connect(&bridge, &ScheduledJobs::completed, &app,
                     [&](const QString &id, const QString &action, const QVariantMap &value) {
        received = value; receivedId = id; receivedAction = action; ++completions;
    });
    auto id = bridge.health();
    wait();
    if (completions != 1 || receivedId != id || receivedAction != "health"
            || received.value("status") != "ok" || observed.value("action") != "health") return 4;
    const auto receipt = bridge.newRequestId();
    bridge.runNow("job-id", 7, receipt);
    wait();
    if (observed.value("request_id") != receipt || observed.value("expected_revision").toInt() != 7
            || observed.contains("owner")) return 5;
    bridge.unread(20, 32);
    wait();
    if (observed.value("limit").toInt() != 20 || observed.value("after").toInt() != 32) return 6;
    response = "{\"status\":\"ok\",\"unexpected\":true}\n";
    bridge.health();
    wait();
    if (received.value("status") != "unavailable") return 7;
    const auto before = completions;
    bridge.health();
    bridge.invalidate();
    wait();
    if (completions != before || bridge.scopeGeneration() != 1) return 8;
    respond = false;
    bridge.readResult("private-run");
    wait();
    const auto beforePrivacy = completions;
    bridge.invalidate();
    for (auto peer : peers)
        if (peer->state() == QLocalSocket::ConnectedState)
            peer->write("{\"status\":\"ok\",\"result\":{\"result\":\"private\"}}\n");
    wait();
    if (completions != beforePrivacy || bridge.scopeGeneration() != 2) return 9;
    qputenv("AIOS_SESSION_SOCKET", "/private/broker.sock");
    observed = {};
    bridge.get("private-job");
    wait();
    if (received.value("status") != "unavailable" || !observed.isEmpty()) return 10;
    qunsetenv("AIOS_SESSION_SOCKET");
    QFile::setPermissions(directory, QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner | QFile::ReadOther);
    bridge.health();
    wait();
    if (received.value("status") != "unavailable") return 11;
    QFile::setPermissions(directory, QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner);
    qputenv("AIOS_SESSION_SOCKET", (directory + "/service.sock").toUtf8());
    QJsonObject context{{"lease", "native-only-lease"}, {"scope", "native-only-scope"}};
    bridge.setProtectedContextProvider([&] { return context; });
    respond = true;
    response = "{\"ok\":true,\"result\":{\"status\":\"ok\",\"result\":{\"available\":true}}}\n";
    bridge.health();
    wait();
    if (::getuid() == 0) {
        if (received.value("status") != "ok" || observed.value("action") != "scheduled_jobs"
                || observed.value("lease") != "native-only-lease"
                || observed.value("request").toObject().value("action") != "health"
                || received.contains("lease") || received.contains("scope")) return 12;
        respond = false;
        bridge.readResult("private-run");
        wait();
        context["scope"] = "replacement-scope";
        for (auto peer : peers)
            if (peer->state() == QLocalSocket::ConnectedState) peer->write(response);
        wait();
        if (received.value("status") != "unavailable" || received.contains("result")) return 13;
        ScheduledJobs first, second;
        first.setProtectedContextProvider([] { return QJsonObject{{"lease", "lease-a"}, {"scope", "work-a"}}; });
        second.setProtectedContextProvider([] { return QJsonObject{{"lease", "lease-b"}, {"scope", "work-b"}}; });
        respond = true;
        first.health(); wait();
        if (observed.value("scope") != "work-a" || observed.value("lease") != "lease-a") return 18;
        second.health(); wait();
        if (observed.value("scope") != "work-b" || observed.value("lease") != "lease-b") return 19;
        first.invalidate(); observed = {};
        first.health(); wait();
        if (!observed.isEmpty()) return 20;
        second.health(); wait();
        if (observed.value("scope") != "work-b") return 40;
    } else if (received.value("status") != "unavailable") {
        return 14;
    }
    qunsetenv("AIOS_SESSION_SOCKET");
    bridge.create({{"prompt", QString(128 * 1024, QChar('x'))}});
    wait();
    if (received.value("status") != "invalid") return 15;
    observed = {};
    qputenv("AIOS_PRINCIPAL", "descriptor-is-not-authorization");
    bridge.health();
    wait();
    if (received.value("status") != "unavailable" || !observed.isEmpty()) return 16;
    std::puts("ScheduledJobs native protocol, bounds, correlation and privacy generation checks passed.");
    return 0;
}
