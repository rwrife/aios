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
    Q_PROPERTY(QVariantList messages READ messages NOTIFY changed)
    Q_PROPERTY(bool olderMessages READ olderMessages NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(QVariantMap challenge READ challenge NOTIFY changed)
public:
    explicit SessionControl(QObject *parent = nullptr) : QObject(parent) {
        path = qEnvironmentVariable("AIOS_SESSION_SOCKET");
        timer.setInterval(500);
        connect(&timer, &QTimer::timeout, this, [this] {
            if (pendingEnrollment) return;
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
    QVariantList messages() const { return m_messages; }
    bool olderMessages() const { return !m_before.isNull(); }
    bool busy() const { return pendingEnrollment; }
    QVariantMap challenge() const { return m_challenge; }
    Q_INVOKABLE void simulate(const QString &state) { if (m_simulator) demoState = state; }
    Q_INVOKABLE void activate(const QString &title) {
        clearPersonal();
        call({{"action", "activate"}, {"title", title}, {"session", QJsonValue::Null}});
    }
    Q_INVOKABLE void resume(const QString &id) {
        clearPersonal();
        call({{"action", "activate"}, {"title", QJsonValue::Null}, {"session", id}});
    }
    Q_INVOKABLE void suspend() { clearPersonal(); call({{"action", "suspend"}}); }
    Q_INVOKABLE void search() { call({{"action", "search"}, {"query", ""}}); }
    Q_INVOKABLE void history(bool older = false) {
        if (!older) { m_messages.clear(); m_before = QJsonValue::Null; }
        call({{"action", "history"}, {"before", older ? m_before : QJsonValue(QJsonValue::Null)}});
    }
    Q_INVOKABLE void note(const QString &content) {
        if (content.trimmed().isEmpty()) return;
        call({{"action", "message"}, {"role", "user"}, {"content", content}});
    }
    Q_INVOKABLE void readDocument(const QString &name) {
        call({{"action", "document_read"}, {"path", name}});
    }
    Q_INVOKABLE void saveDocument(const QString &name, const QString &content) {
        call({{"action", "document_save"}, {"path", name}, {"content", content}});
    }
    Q_INVOKABLE void enroll(const QString &name, const QString &pin, bool consent) {
        call({{"action", "enroll_manual"}, {"name", name}, {"pin", pin}, {"consent", consent}});
    }
    Q_INVOKABLE void unlock(const QString &name, const QString &pin) {
        clearPersonal();
        call({{"action", "activate_verified"}, {"owner", name}, {"pin", pin},
              {"title", QJsonValue::Null}, {"session", QJsonValue::Null}});
    }
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
    void documentLoaded(const QString &content);
    void documentSaved();
    void enrollmentCompleted(const QString &recovery);
    void unlocked();
private:
    QString path, demoState, m_error, m_authority = "anonymous";
    bool m_shield = true, m_simulator = false;
    QVariantList m_sessions, m_messages;
    QJsonValue m_before = QJsonValue::Null;
    quint64 generation = 0;
    QVariantMap m_challenge;
    QTimer timer;
    bool pendingStatus = false;
    bool pendingEnrollment = false;
    void clearPersonal() {
        ++generation;
        m_sessions.clear(); m_messages.clear(); m_challenge.clear();
        m_before = QJsonValue::Null; m_error.clear();
        emit privacyLost(); emit changed();
    }
    void failClosed() {
        m_shield = true; m_authority = "anonymous";
        clearPersonal();
    }
    void call(const QJsonObject &request) {
        if (!enabled()) return;
        const QString action = request.value("action").toString();
        if (pendingEnrollment) return;
        if (action == "enroll_manual") { pendingEnrollment = true; emit changed(); }
        if (action == "status" && pendingStatus) return;
        if (action == "status") pendingStatus = true;
        auto socket = new QLocalSocket(this);
        const auto requestGeneration = generation;
        auto buffer = new QByteArray;
        connect(socket, &QObject::destroyed, [buffer] { delete buffer; });
        connect(socket, &QLocalSocket::connected, this, [socket, request] {
            socket->write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
        });
        connect(socket, &QLocalSocket::readyRead, this, [this, socket, buffer, action, requestGeneration] {
            *buffer += socket->readAll();
            if (buffer->size() > 262144) { failClosed(); socket->abort(); return; }
            if (!buffer->endsWith('\n')) return;
            if (requestGeneration != generation) {
                socket->disconnectFromServer(); socket->deleteLater(); return;
            }
            const auto response = QJsonDocument::fromJson(*buffer).object();
            if (!response.value("ok").toBool()) {
                m_error = response.value("error").toString();
                if (action == "status" && !pendingEnrollment) failClosed();
            } else {
                const auto result = response.value("result").toObject();
                if (action != "status" && action != "simulate") m_error.clear();
                if (action == "status") {
                    const bool wasPersonal = m_authority != "anonymous";
                    m_shield = result.value("shield").toBool() || result.value("fault").toBool();
                    m_simulator = result.value("simulator").toBool();
                    m_authority = result.value("authority").toString();
                    if (m_shield || (wasPersonal && m_authority == "anonymous")) {
                        clearPersonal();
                    }
                } else if (action == "search") m_sessions = result.value("sessions").toArray().toVariantList();
                else if (action == "history") {
                    m_messages = result.value("messages").toArray().toVariantList() + m_messages;
                    m_before = result.value("before");
                }
                else if (action == "activate" || action == "message") history();
                else if (action == "activate_verified") { emit unlocked(); search(); }
                else if (action == "enroll_manual") emit enrollmentCompleted(result.value("recovery").toString());
                else if (action == "document_read") emit documentLoaded(result.value("content").toString());
                else if (action == "document_save") emit documentSaved();
                else if (action == "request_capability") m_challenge = result.toVariantMap();
                else if (action == "verify") call({{"action", "github_profile"}, {"token", result.value("capability")}});
                else if (action == "github_profile") m_error = "Protected account: " + result.value("login").toString();
                else if (action != "simulate") m_error.clear();
            }
            emit changed(); socket->disconnectFromServer(); socket->deleteLater();
        });
        connect(socket, &QLocalSocket::errorOccurred, this, [this, action](QLocalSocket::LocalSocketError) {
            if (action == "status" && !pendingEnrollment) failClosed();
        });
        connect(socket, &QObject::destroyed, this, [this, action] {
            if (action == "status") pendingStatus = false;
            if (action == "enroll_manual") { pendingEnrollment = false; emit changed(); }
        });
        QTimer::singleShot(action == "enroll_manual" ? 300000 : 2000, socket, [this, socket, action] {
            if (action == "status" && !pendingEnrollment) failClosed();
            socket->abort(); socket->deleteLater();
        });
        socket->connectToServer(path);
    }
};
