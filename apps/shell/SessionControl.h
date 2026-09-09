#pragma once
#include <QObject>
#include <QLocalSocket>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QTimer>

// Broker UI has no link to the chat worker or model tool registry. Experimental
// mode is explicit. The normal desktop retains its existing behavior.
class SessionControl : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool enabled READ enabled CONSTANT)
    Q_PROPERTY(bool shield READ shield NOTIFY changed)
    Q_PROPERTY(bool simulator READ simulator NOTIFY changed)
    Q_PROPERTY(QString authority READ authority NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QVariantList sessions READ sessions NOTIFY changed)
    Q_PROPERTY(QVariantMap challenge READ challenge NOTIFY changed)
public:
    explicit SessionControl(QObject *parent = nullptr) : QObject(parent) {
        path = qEnvironmentVariable("AIOS_SESSION_SOCKET");
        timer.setInterval(500);
        connect(&timer, &QTimer::timeout, this, [this] {
            if (!demoState.isEmpty()) call({{"action", "simulate"}, {"state", demoState}});
            call({{"action", "status"}});
        });
        if (enabled()) timer.start();
    }
    bool enabled() const { return !path.isEmpty(); }
    bool shield() const { return m_shield; }
    bool simulator() const { return m_simulator; }
    QString authority() const { return m_authority; }
    QString error() const { return m_error; }
    QVariantList sessions() const { return m_sessions; }
    QVariantMap challenge() const { return m_challenge; }
    Q_INVOKABLE void simulate(const QString &state) { if (m_simulator) demoState = state; }
    Q_INVOKABLE void activate(const QString &title) {
        call({{"action", "activate"}, {"title", title}, {"session", QJsonValue::Null}});
    }
    Q_INVOKABLE void resume(const QString &id) {
        call({{"action", "activate"}, {"title", QJsonValue::Null}, {"session", id}});
    }
    Q_INVOKABLE void suspend() { call({{"action", "suspend"}}); }
    Q_INVOKABLE void search() { call({{"action", "search"}, {"query", ""}}); }
    Q_INVOKABLE void launch(const QString &app) {
        QJsonArray arguments;
        if (app == "editor") arguments.append("Resume.txt");
        call({{"action", "launch"}, {"app", app}, {"arguments", arguments}});
    }
    Q_INVOKABLE void protectedResource() {
        call({{"action", "request_capability"}, {"operation", "secrets.github.profile"}, {"resource", "github"}});
    }
    Q_INVOKABLE void verify(const QString &pin) {
        if (m_challenge.isEmpty()) return;
        const auto id = m_challenge.value("id").toString();
        m_challenge.clear();
        call({{"action", "verify"}, {"challenge", id}, {"pin", pin}, {"confirmed", false}});
    }
    Q_INVOKABLE void cancelChallenge() {
        m_challenge.clear(); call({{"action", "cancel_challenge"}}); emit changed();
    }
signals:
    void changed();
    void privacyLost();
private:
    QString path, demoState, m_error, m_authority = "anonymous";
    bool m_shield = true, m_simulator = false;
    QVariantList m_sessions;
    QVariantMap m_challenge;
    QTimer timer;
    bool pendingStatus = false;
    void failClosed() {
        m_shield = true; m_authority = "anonymous";
        m_sessions.clear(); m_challenge.clear();
        emit privacyLost(); emit changed();
    }
    void call(const QJsonObject &request) {
        if (!enabled()) return;
        const QString action = request.value("action").toString();
        if (action == "status" && pendingStatus) return;
        if (action == "status") pendingStatus = true;
        auto socket = new QLocalSocket(this);
        auto buffer = new QByteArray;
        connect(socket, &QObject::destroyed, [buffer] { delete buffer; });
        connect(socket, &QLocalSocket::connected, this, [socket, request] {
            socket->write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
        });
        connect(socket, &QLocalSocket::readyRead, this, [this, socket, buffer, action] {
            *buffer += socket->readAll();
            if (buffer->size() > 65536) { failClosed(); socket->abort(); return; }
            if (!buffer->endsWith('\n')) return;
            const auto response = QJsonDocument::fromJson(*buffer).object();
            if (!response.value("ok").toBool()) {
                m_error = response.value("error").toString();
                if (action == "status") failClosed();
            } else {
                const auto result = response.value("result").toObject();
                if (action == "status") {
                    const bool wasPersonal = m_authority != "anonymous";
                    m_shield = result.value("shield").toBool() || result.value("fault").toBool();
                    m_simulator = result.value("simulator").toBool();
                    m_authority = result.value("authority").toString();
                    if (m_shield || (wasPersonal && m_authority == "anonymous")) {
                        m_challenge.clear(); m_sessions.clear(); emit privacyLost();
                    }
                } else if (action == "search") m_sessions = result.value("sessions").toArray().toVariantList();
                else if (action == "request_capability") m_challenge = result.toVariantMap();
                else if (action == "verify") call({{"action", "github_profile"}, {"token", result.value("capability")}});
                else if (action == "github_profile") m_error = "Protected account: " + result.value("login").toString();
                else if (action != "simulate") m_error.clear();
            }
            emit changed(); socket->disconnectFromServer(); socket->deleteLater();
        });
        connect(socket, &QLocalSocket::errorOccurred, this, [this, action](QLocalSocket::LocalSocketError) {
            if (action == "status") failClosed();
        });
        connect(socket, &QObject::destroyed, this, [this, action] {
            if (action == "status") pendingStatus = false;
        });
        QTimer::singleShot(2000, socket, [this, socket, action] {
            if (action == "status") failClosed();
            socket->abort(); socket->deleteLater();
        });
        socket->connectToServer(path);
    }
};
