#pragma once
#include <QObject>
#include <QCoreApplication>
#include <QProcess>
#include <QProcessEnvironment>
#include <QLocalSocket>
#include <QTemporaryDir>
#include <QTimer>
#include <QPointer>
#include <QJsonDocument>
#include <QJsonObject>
#include <QCryptographicHash>
#include <QUuid>
#include <QFile>
#include <cmath>
#include "CameraProtocol.h"
#ifdef Q_OS_LINUX
#include <sys/socket.h>
#include <unistd.h>
#endif

// Native UI-only client. There is no worker/model tool registration for this
// channel. Both endpoints validate Unix PID/UID; raw recognition data stays in
// the service worker. Exactly one client/service is shared by all chat windows.
class CameraClient : public QObject {
    Q_OBJECT
public:
    static CameraClient *instance() {
        static QPointer<CameraClient> value;
        if (!value) value = new CameraClient(qApp);
        return value;
    }
    static QString identifier() { return QUuid::createUuid().toString(QUuid::Id128); }
    static QString deviceKey(const QString &path) {
        return path.isEmpty() ? QString() : QString::fromLatin1(
            QCryptographicHash::hash(path.toUtf8(), QCryptographicHash::Sha256).toHex());
    }
    void configure(bool active, bool secure) {
        gates = {{"active", active}, {"secure", secure}};
        protocol.gate(active, secure);
        ensureStarted();
        if (ready) command("configure", controlId, gates);
    }
    void refresh() { if (ready) command("refresh", controlId); }
    QString capture(const QString &consumer, const QString &mode, const QString &device = {},
                    const QString &owner = {}, const QString &pin = {}, bool consent = false) {
        ensureStarted();
        if (!qEnvironmentVariableIsEmpty("AIOS_SESSION_SOCKET") || retries > 3) return {};
        return command("capture", consumer, {{"mode", mode}, {"device", deviceKey(device)},
            {"owner", owner}, {"pin", pin}, {"consent", consent}});
    }
    void release(const QString &consumer) {
        protocol.release(consumer);
        for (int i = pending.size() - 1; i >= 0; --i)
            if (pending[i].value("consumer").toString() == consumer) pending.removeAt(i);
        if (ready) command("release", consumer);
    }
    ~CameraClient() override {
        process.disconnect(this);
        socket.disconnect(this);
        ready = false;
        socket.abort();
        if (process.state() != QProcess::NotRunning) {
            process.kill();
            process.waitForFinished(500);
        }
    }
signals:
    void received(const QJsonObject &event);
    void unavailable();
    void generationChanged();
private:
    explicit CameraClient(QObject *parent) : QObject(parent) {
        connector.setInterval(50);
        socket.setReadBufferSize(262145);
        connect(&connector, &QTimer::timeout, this, [this] {
            if (process.state() == QProcess::Running && socket.state() == QLocalSocket::UnconnectedState)
                socket.connectToServer(directory.path() + "/capture.sock");
        });
        connect(&socket, &QLocalSocket::connected, this, [this] {
#ifdef Q_OS_LINUX
            struct ucred peer{};
            socklen_t length = sizeof(peer);
            if (::getsockopt(int(socket.socketDescriptor()), SOL_SOCKET, SO_PEERCRED, &peer, &length) != 0 ||
                peer.pid != process.processId() || peer.uid != ::getuid()) {
                process.kill();
                return;
            }
            ready = true;
            connector.stop();
            command("configure", controlId, gates);
            const auto queued = pending; pending.clear();
            for (const auto &item : queued)
                socket.write(QJsonDocument(item).toJson(QJsonDocument::Compact) + '\n');
#else
            socket.abort();
#endif
        });
        connect(&socket, &QLocalSocket::readyRead, this, [this] { readEvents(); });
        connect(&socket, &QLocalSocket::disconnected, this, [this] {
            ready = false;
            if (process.state() != QProcess::NotRunning) process.kill();
        });
        connect(&process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
                [this](int, QProcess::ExitStatus) { stopped(); });
        connect(&process, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
            if (error == QProcess::FailedToStart) stopped();
        });
        process.setStandardErrorFile(QProcess::nullDevice());
        process.setStandardOutputFile(QProcess::nullDevice());
    }
    void ensureStarted() {
        if (starting || retryPending || retries > 3 || !directory.isValid() || !qEnvironmentVariableIsEmpty("AIOS_SESSION_SOCKET")) return;
        starting = true;
        ready = false;
        protocol = CameraProtocol();
        protocol.gate(gates.value("active").toBool(), gates.value("secure").toBool());
        buffer.clear();
        QFile::remove(directory.path() + "/capture.sock");
        auto environment = QProcessEnvironment::systemEnvironment();
        const auto path = environment.value("AIOS_PYTHONPATH", environment.value("PYTHONPATH", "/usr/local/share/aios"));
        environment.insert("PYTHONPATH", path);
        process.setProcessEnvironment(environment);
        process.start("python3", {"-m", "aios.capture_service", "--socket", directory.path() + "/capture.sock"});
        connector.start();
        const auto attempt = retries;
        QTimer::singleShot(3000, this, [this, attempt] {
            if (!ready && starting && retries == attempt) process.kill();
        });
    }
    void stopped() {
        if (!starting) return;
        starting = false;
        ready = false;
        connector.stop();
        socket.abort();
        buffer.clear();
        pending.clear();
        emit unavailable();
        // A dead worker/service never creates an unbounded restart loop.
        static const int delays[] = {2000, 5000, 15000};
        if (retries < 3) {
            const int delay = delays[retries++];
            retryPending = true;
            QTimer::singleShot(delay, this, [this] { retryPending = false; ensureStarted(); });
        } else ++retries;
    }
    QString command(const QString &action, const QString &consumer, QJsonObject values = {}) {
        const auto request = identifier();
        values.insert("version", 1); values.insert("action", action);
        values.insert("request", request); values.insert("consumer", consumer);
        const auto data = QJsonDocument(values).toJson(QJsonDocument::Compact) + '\n';
        if (!ready && action == "capture") {
            if (pending.size() >= 4 || data.size() > 4096) return {};
            protocol.track(request, consumer, values.value("mode").toString());
            pending.append(values);
            return request;
        }
        if (data.size() > 4096 || socket.bytesToWrite() + data.size() > 8192) {
            process.kill();
            return {};
        }
        if (action == "capture") protocol.track(request, consumer, values.value("mode").toString());
        socket.write(data);
        return request;
    }
    void readEvents() {
        if (!ready) { socket.readAll(); return; }
        buffer.append(socket.read(262145));
        while (buffer.contains('\n')) {
            const auto split = buffer.indexOf('\n');
            if (split > 262144) { process.kill(); return; }
            const auto line = buffer.left(split); buffer.remove(0, split + 1);
            QJsonParseError error;
            const auto document = QJsonDocument::fromJson(line, &error);
            const auto event = document.object();
            if (error.error != QJsonParseError::NoError || !document.isObject() || !CameraProtocol::uniqueKeys(line)) {
                process.kill(); return;
            }
            const auto previous = protocol.generation();
            const auto accepted = protocol.accept(event, CameraProtocol::monotonic());
            if (accepted == CameraProtocol::Invalid) { process.kill(); return; }
            if (accepted == CameraProtocol::Drop) continue;
            if (protocol.generation() != previous) emit generationChanged();
            emit received(event);
        }
        if (buffer.size() > 262144) process.kill();
    }
    QTemporaryDir directory;
    QProcess process;
    QLocalSocket socket;
    QTimer connector;
    QByteArray buffer;
    QList<QJsonObject> pending;
    QString controlId = identifier();
    QJsonObject gates{{"active", false}, {"secure", false}};
    bool ready = false, starting = false, retryPending = false;
    int retries = 0;
    CameraProtocol protocol;
};
