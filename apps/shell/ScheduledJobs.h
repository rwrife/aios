#pragma once
#include <QObject>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocalSocket>
#include <QSet>
#include <QTimer>
#include <QUuid>
#include <QVariantMap>
#include <memory>
#include <functional>
#ifdef Q_OS_LINUX
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

// No model-supplied socket, owner, credentials, or generic service passthrough.
class ScheduledJobs : public QObject {
    Q_OBJECT
    Q_PROPERTY(quint64 scopeGeneration READ scopeGeneration NOTIFY invalidated)
    Q_PROPERTY(bool protectedWorkspace READ protectedWorkspace CONSTANT)
public:
    explicit ScheduledJobs(QObject *parent = nullptr) : QObject(parent) {}
    quint64 scopeGeneration() const { return generation; }
    bool protectedWorkspace() const { return !qEnvironmentVariableIsEmpty("AIOS_SESSION_SOCKET"); }
    void setProtectedContextProvider(std::function<QJsonObject()> provider) { protectedContext = std::move(provider); }
    Q_INVOKABLE void dispose() {
        if (disposing) return;
        disposing = true;
        invalidate();
        deleteLater();
    }
    Q_INVOKABLE QString newRequestId() const { return QUuid::createUuid().toString(QUuid::WithoutBraces); }
    Q_INVOKABLE QString health() { return call({{"action", "health"}}); }
    Q_INVOKABLE QString binding(const QString &prompt) { return call({{"action", "binding"}, {"prompt", prompt}}); }
    Q_INVOKABLE QString preview(const QVariantMap &schedule) {
        return call({{"action", "preview"}, {"schedule", QJsonObject::fromVariantMap(schedule)}});
    }
    Q_INVOKABLE QString create(const QVariantMap &config) {
        return call({{"action", "create"}, {"config", QJsonObject::fromVariantMap(config)}});
    }
    Q_INVOKABLE QString get(const QString &job) { return call({{"action", "get"}, {"job_id", job}}); }
    Q_INVOKABLE QString list(int limit = 50, const QString &after = {}) {
        QJsonObject value{{"action", "list"}, {"limit", limit}};
        if (!after.isEmpty()) value["after"] = after;
        return call(value);
    }
    Q_INVOKABLE QString update(const QString &job, qint64 revision, const QVariantMap &config) {
        auto value = mutation("update", job, revision);
        value["config"] = QJsonObject::fromVariantMap(config);
        return call(value);
    }
    Q_INVOKABLE QString pause(const QString &job, qint64 revision) { return call(mutation("pause", job, revision)); }
    Q_INVOKABLE QString resume(const QString &job, qint64 revision) { return call(mutation("resume", job, revision)); }
    Q_INVOKABLE QString remove(const QString &job, qint64 revision) { return call(mutation("delete", job, revision)); }
    Q_INVOKABLE QString runNow(const QString &job, qint64 revision, const QString &request) {
        auto value = mutation("run_now", job, revision);
        value["request_id"] = request;
        return call(value);
    }
    Q_INVOKABLE QString cancelRun(const QString &run) { return call({{"action", "cancel_run"}, {"run_id", run}}); }
    Q_INVOKABLE QString listRuns(const QString &job, int limit = 20, qint64 before = 0) {
        QJsonObject value{{"action", "list_runs"}, {"job_id", job}, {"limit", limit}};
        if (before) value["before"] = before;
        return call(value);
    }
    Q_INVOKABLE QString readResult(const QString &run) { return call({{"action", "read_result"}, {"run_id", run}}); }
    Q_INVOKABLE QString acknowledgeResult(const QString &run) { return call({{"action", "acknowledge_result"}, {"run_id", run}}); }
    Q_INVOKABLE QString unread(int limit = 50, qint64 after = 0) {
        return call({{"action", "unread"}, {"limit", limit}, {"after", after}});
    }
    void invalidate() {
        ++generation;
        protectedContext = {};
        const auto pending = sockets;
        sockets.clear();
        for (auto socket : pending) {
            socket->disconnect(this);
            socket->abort();
            socket->deleteLater();
        }
        emit invalidated();
    }
signals:
    void completed(const QString &requestId, const QString &action, const QVariantMap &response);
    void invalidated();
private:
    quint64 generation = 0;
    bool disposing = false;
    QSet<QLocalSocket *> sockets;
    std::function<QJsonObject()> protectedContext;
    static QJsonObject mutation(const QString &action, const QString &job, qint64 revision) {
        return {{"action", action}, {"job_id", job}, {"expected_revision", revision}};
    }
    static QVariantMap failure(const QString &message) {
        return {{"status", "unavailable"}, {"error", message}};
    }
    static bool privatePath(const QString &path, bool directory) {
#ifdef Q_OS_LINUX
        struct stat info{};
        if (::lstat(path.toLocal8Bit().constData(), &info) != 0) return false;
        return info.st_uid == ::getuid() && !(info.st_mode & 0077)
            && (directory ? S_ISDIR(info.st_mode) : S_ISSOCK(info.st_mode));
#else
        Q_UNUSED(path)
        Q_UNUSED(directory)
        return false;
#endif
    }
    QString call(const QJsonObject &value) {
        const auto id = newRequestId();
        const auto action = value.value("action").toString();
        const auto epoch = generation;
        // Deferred completion lets callers register the returned correlation ID.
        QTimer::singleShot(0, this, [this, id, action, value, epoch] {
            if (epoch != generation) return;
            auto raw = QJsonDocument(value).toJson(QJsonDocument::Compact) + '\n';
            const auto runtime = qEnvironmentVariable("XDG_RUNTIME_DIR");
            const auto directory = runtime + "/aios-scheduler";
            const bool protectedMode = protectedWorkspace();
            const auto path = protectedMode ? qEnvironmentVariable("AIOS_SESSION_SOCKET") : directory + "/service.sock";
            const auto context = protectedMode && protectedContext ? protectedContext() : QJsonObject{};
            if ((protectedMode && context.isEmpty()) || !qEnvironmentVariableIsEmpty("AIOS_SESSION_ID")
                    || !qEnvironmentVariableIsEmpty("AIOS_BACKGROUND_RUN")
                    || (!protectedMode && !qEnvironmentVariableIsEmpty("AIOS_PRINCIPAL"))) {
                emit completed(id, action, failure("Scheduled jobs require a current native workspace binding in this protected context."));
                return;
            }
            if (raw.size() > 128 * 1024 || sockets.size() >= 8) {
                emit completed(id, action, {{"status", "invalid"}, {"error", "Scheduling request exceeds the transport limit."}});
                return;
            }
            if (!protectedMode && (runtime.isEmpty() || !privatePath(runtime, true)
                    || !privatePath(directory, true) || !privatePath(path, false))) {
                emit completed(id, action, failure("Scheduled jobs service is unavailable."));
                return;
            }
            if (protectedMode) {
                QJsonObject wrapped{{"action", "scheduled_jobs"}, {"lease", context.value("lease")},
                                    {"scope", context.value("scope")}, {"request", value}};
                raw = QJsonDocument(wrapped).toJson(QJsonDocument::Compact) + '\n';
                if (raw.size() > 256 * 1024) {
                    emit completed(id, action, failure("Protected scheduling request exceeds the transport limit."));
                    return;
                }
            }
            auto socket = new QLocalSocket(this);
            socket->setReadBufferSize(2 * 1024 * 1024 + 1);
            auto buffer = std::make_shared<QByteArray>();
            sockets.insert(socket);
            const auto finish = [this, socket, id, action, epoch](const QVariantMap &reply) {
                if (!sockets.remove(socket)) return;
                socket->disconnect(this);
                socket->abort();
                socket->deleteLater();
                if (epoch == generation) emit completed(id, action, reply);
            };
            const auto fail = [this, socket, finish, protectedMode](const QString &message) {
                if (!sockets.contains(socket)) return;
                finish(failure(message));
                if (protectedMode) invalidate();
            };
            connect(socket, &QLocalSocket::connected, this, [this, socket, raw, fail, protectedMode, context] {
#ifdef Q_OS_LINUX
                struct ucred peer{};
                socklen_t length = sizeof(peer);
                if (::getsockopt(socket->socketDescriptor(), SOL_SOCKET, SO_PEERCRED, &peer, &length)
                        || peer.uid != (protectedMode ? 0 : ::getuid())) {
                    fail("Scheduled jobs service identity is unavailable.");
                    return;
                }
                if (protectedMode && (!protectedContext || protectedContext() != context)) {
                    fail("Private workspace authorization changed.");
                    return;
                }
                socket->write(raw);
#else
                Q_UNUSED(raw)
                fail("Scheduled jobs require the native Linux session.");
#endif
            });
            connect(socket, &QLocalSocket::readyRead, this, [this, socket, buffer, finish, fail, protectedMode, context] {
                buffer->append(socket->readAll());
                if (buffer->size() > 2 * 1024 * 1024) {
                    fail("Scheduled jobs response exceeds the transport limit.");
                    return;
                }
                const auto newline = buffer->indexOf('\n');
                if (newline < 0) return;
                QJsonParseError error;
                const auto document = QJsonDocument::fromJson(buffer->left(newline), &error);
                auto reply = document.object();
                if (protectedMode) {
                    if (!protectedContext || protectedContext() != context) {
                        fail("Private workspace authorization changed.");
                        return;
                    }
                    if (error.error != QJsonParseError::NoError || !reply.value("ok").toBool()
                            || !reply.value("result").isObject()) {
                        fail("Private workspace scheduling authorization is unavailable.");
                        return;
                    }
                    reply = reply.value("result").toObject();
                }
                const auto status = reply.value("status").toString();
                const QStringList statuses{"ok", "invalid", "unavailable", "conflict", "quota_exceeded", "needs_user_action"};
                if (error.error != QJsonParseError::NoError || !document.isObject()
                        || reply.size() != 2 || !statuses.contains(status)
                        || (status == "ok" ? !reply.contains("result") : !reply.value("error").isString())) {
                    fail("Scheduled jobs service returned an invalid response.");
                    return;
                }
                finish(reply.toVariantMap());
            });
            connect(socket, &QLocalSocket::errorOccurred, this, [fail](QLocalSocket::LocalSocketError) {
                fail("Scheduled jobs service is unavailable. Read current state before retrying.");
            });
            connect(socket, &QLocalSocket::disconnected, this, [fail] {
                fail("Scheduled jobs service response was incomplete.");
            });
            QTimer::singleShot(15000, socket, [fail] {
                fail("Scheduled jobs service timed out. Read current state before retrying.");
            });
            socket->connectToServer(path);
        });
        return id;
    }
};
