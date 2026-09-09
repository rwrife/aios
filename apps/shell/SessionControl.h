#pragma once
#include <QObject>
#include <QGuiApplication>
#include <QLocalSocket>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QTimer>
#include "ProfilePhoto.h"
#include <QProcess>
#include <QProcessEnvironment>

// Broker UI has no link to the chat worker or model tool registry. Experimental
// mode is explicit. The normal desktop retains its existing behavior.
class SessionControl : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool enabled READ enabled CONSTANT)
    Q_PROPERTY(bool greetingOnly READ greetingOnly CONSTANT)
    Q_PROPERTY(bool shield READ shield NOTIFY changed)
    Q_PROPERTY(bool simulator READ simulator NOTIFY changed)
    Q_PROPERTY(QString authority READ authority NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QVariantList sessions READ sessions NOTIFY changed)
    Q_PROPERTY(QVariantList messages READ messages NOTIFY changed)
    Q_PROPERTY(bool olderMessages READ olderMessages NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool embeddedDisplay READ embeddedDisplay NOTIFY changed)
    Q_PROPERTY(bool secureInput READ secureInput NOTIFY changed)
    Q_PROPERTY(bool personalAvailable READ personalAvailable NOTIFY changed)
    Q_PROPERTY(QVariantMap challenge READ challenge NOTIFY changed)
    Q_PROPERTY(QVariantMap profile READ profile NOTIFY changed)
    Q_PROPERTY(QVariantList profiles READ profiles NOTIFY changed)
public:
    explicit SessionControl(QObject *parent = nullptr) : QObject(parent) {
        path = qEnvironmentVariable("AIOS_SESSION_SOCKET");
        connect(&photoCapture, &ProfilePhoto::captured, this, &SessionControl::photoCaptured);
        connect(&photoCapture, &ProfilePhoto::failed, this, [this] { m_error = "Camera unavailable. You can create a profile without a photo."; emit changed(); });
        timer.setInterval(500);
        connect(&timer, &QTimer::timeout, this, [this] {
            if (pendingEnrollment) return;
            if (!demoState.isEmpty()) call({{"action", "simulate"}, {"state", demoState}});
            call({{"action", "status"}});
        });
        if (enabled()) timer.start();
    }
    bool enabled() const { return !path.isEmpty(); }
    bool greetingOnly() const { return !enabled(); }
    Q_INVOKABLE QObject *chatProfile() { return new SessionControl(this); }
    Q_INVOKABLE void dispose() { deleteLater(); }
    bool shield() const { return m_shield; }
    bool simulator() const { return m_simulator; }
    QString authority() const { return m_authority; }
    QString error() const { return m_error; }
    QVariantList sessions() const { return m_sessions; }
    QVariantList messages() const { return m_messages; }
    bool olderMessages() const { return !m_before.isNull(); }
    bool busy() const { return pendingEnrollment; }
    bool embeddedDisplay() const { return m_embedded; }
    bool secureInput() const { return m_secureInput; }
    bool personalAvailable() const { return greetingOnly() || m_personalAvailable; }
    QVariantMap profile() const { return m_profile; }
    QVariantList profiles() const { return m_profiles; }
    Q_INVOKABLE void listProfiles() { call({{"action", "profiles"}}); }
    Q_INVOKABLE void deleteAccount(const QString &id, const QString &pin) {
        if (greetingOnly()) call({{"action", "delete_profile"}, {"owner", id}, {"pin", pin}, {"confirmed", true}});
    }
    Q_INVOKABLE void takeProfilePhoto() { if (m_secureInput && personalAvailable()) photoCapture.take(); }
    Q_INVOKABLE void setSecureInput(bool active) { m_secureInput = active; if (!active) photoCapture.cancel(); emit changed(); }
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
    Q_INVOKABLE void enrollProfile(const QString &name, const QString &pin, bool consent, const QString &photo) {
        call({{"action", "enroll_profile"}, {"name", name}, {"pin", pin}, {"consent", consent}, {"photo", photo}});
    }
    Q_INVOKABLE void unlock(const QString &name, const QString &pin) {
        if (enabled()) clearPersonal();
        call({{"action", "activate_verified"}, {"owner", name}, {"pin", pin},
              {"title", QJsonValue::Null}, {"session", QJsonValue::Null}});
    }
    Q_INVOKABLE void recover(const QString &name, const QString &secret, const QString &pin) {
        call({{"action", "recover"}, {"owner", name}, {"recovery", secret}, {"pin", pin}});
    }
    Q_INVOKABLE void launch(const QString &app) {
        if (m_embedded) { emit displayRequested(app); return; }
        launchReady(app);
    }
    Q_INVOKABLE void launchReady(const QString &app) {
        QJsonArray arguments;
        if (app == "editor") arguments.append("Resume.txt");
        call({{"action", "launch"}, {"app", app}, {"arguments", arguments}});
    }
    Q_INVOKABLE void displayReady(const QString &lease, const QString &app) {
        pendingApp = app;
        call({{"action", "display_ready"}, {"lease", lease}});
    }
    Q_INVOKABLE void displayFailed() { m_error = "Private display is unavailable"; emit changed(); }
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
    void photoCaptured(const QString &preview, const QString &rgb);
    void accountDeleted(const QString &id);
    void unlocked();
    void displayRequested(const QString &app);
private:
    QString path, demoState, m_error, pendingApp, m_lease, m_authority = "anonymous";
    bool m_embedded = false, m_secureInput = false, m_attested = false, m_personalAvailable = false;
    bool m_shield = true, m_simulator = false;
    QVariantList m_sessions, m_messages;
    QJsonValue m_before = QJsonValue::Null;
    quint64 generation = 0;
    QVariantMap m_challenge;
    QVariantMap m_profile;
    QVariantList m_profiles;
    ProfilePhoto photoCapture;
    QTimer timer;
    bool pendingStatus = false;
    bool pendingEnrollment = false;
    void clearPersonal() {
        photoCapture.cancel(); m_profile.clear(); m_profiles.clear();
        ++generation;
        m_sessions.clear(); m_messages.clear(); m_challenge.clear();
        m_before = QJsonValue::Null; m_error.clear();
        pendingApp.clear();
        m_lease.clear();
        emit privacyLost(); emit changed();
    }
    void failClosed() {
        m_shield = true; m_authority = "anonymous";
        clearPersonal();
    }
    void call(const QJsonObject &request) {
        if (!enabled()) { callGreeting(request); return; }
        const QString action = request.value("action").toString();
        if (pendingEnrollment) return;
        if (action == "enroll_manual" || action == "enroll_profile") { pendingEnrollment = true; emit changed(); }
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
                    m_embedded = result.value("embedded_display").toBool();
                    m_personalAvailable = result.value("personal_available").toBool() && (m_attested || m_simulator || !m_embedded);
                    if (m_embedded && (!m_attested || !result.value("display_attested").toBool())) {
                        m_attested = false;
#ifdef AIOS_EMBEDDED_DISPLAY
                        const bool embedded = true;
#else
                        const bool embedded = false;
#endif
                        call({{"action", "display_attest"}, {"platform", QGuiApplication::platformName()}, {"embedded", embedded}});
                    }
                    m_authority = result.value("authority").toString();
                    const auto nextLease = result.value("lease").toString();
                    if (m_shield || (wasPersonal && m_authority == "anonymous") ||
                        (!m_lease.isEmpty() && m_lease != nextLease)) {
                        clearPersonal();
                    }
                      m_lease = nextLease;
                      m_profile = result.value("profile").toObject().toVariantMap();
                } else if (action == "search") m_sessions = result.value("sessions").toArray().toVariantList();
                else if (action == "history") {
                    m_messages = result.value("messages").toArray().toVariantList() + m_messages;
                    m_before = result.value("before");
                }
                else if (action == "activate" || action == "message") {
                    history();
                    if (action == "activate" && m_embedded) emit displayRequested("");
                }
                else if (action == "display_attest") m_attested = true;
                else if (action == "activate_verified") {
                    emit unlocked(); search();
                    if (m_embedded) emit displayRequested("");
                }
                else if (action == "display_ready") {
                    const auto app = pendingApp; pendingApp.clear();
                    if (!app.isEmpty()) launchReady(app);
                }
                else if (action == "enroll_manual" || action == "enroll_profile" || action == "recover") emit enrollmentCompleted(result.value("recovery").toString());
                else if (action == "profiles") m_profiles = result.value("profiles").toArray().toVariantList();
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
            if (action == "enroll_manual" || action == "enroll_profile") { pendingEnrollment = false; emit changed(); }
        });
        QTimer::singleShot((action == "enroll_manual" || action == "enroll_profile") ? 300000 : 2000, socket, [this, socket, action] {
            if (action == "status" && !pendingEnrollment) failClosed();
            socket->abort(); socket->deleteLater();
        });
        socket->connectToServer(path);
    }
    void callGreeting(const QJsonObject &request) {
        if (pendingEnrollment) return;
        const auto action = request.value("action").toString();
        if (action != "profiles" && action != "enroll_manual" && action != "enroll_profile" && action != "activate_verified" && action != "delete_profile") return;
        pendingEnrollment = true; m_error.clear(); emit changed();
        auto process = new QProcess(this);
        auto environment = QProcessEnvironment::systemEnvironment();
        const auto modulePath = environment.value("AIOS_PYTHONPATH");
        if (!modulePath.isEmpty()) environment.insert("PYTHONPATH", modulePath);
        else if (environment.value("PYTHONPATH").isEmpty())
            environment.insert("PYTHONPATH", "/usr/local/share/aios");
        process->setProcessEnvironment(environment);
        const auto epoch = generation;
        connect(process, &QProcess::started, process, [process, request] {
            process->write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n'); process->closeWriteChannel();
        });
        connect(process, &QProcess::readyReadStandardError, process, [process] { process->readAllStandardError(); });
        connect(process, &QProcess::errorOccurred, this, [this, process](QProcess::ProcessError) {
            m_error = "Could not open local profiles"; pendingEnrollment = false; emit changed(); process->deleteLater();
        });
        connect(process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
                [this, process, epoch, action](int code, QProcess::ExitStatus) {
            pendingEnrollment = false;
            if (epoch == generation) {
                const auto bytes = process->readAllStandardOutput();
                const auto reply = bytes.size() <= 262144 ? QJsonDocument::fromJson(bytes).object() : QJsonObject();
                if (code || !reply.value("ok").toBool()) m_error = reply.value("error").toString("Could not open local profiles");
                else {
                    const auto result = reply.value("result").toObject();
                    if (action == "profiles") m_profiles = result.value("profiles").toArray().toVariantList();
                    else if (action == "delete_profile") {
                        const auto id = result.value("deleted").toString();
                        auto root = this;
                        while (auto ancestor = qobject_cast<SessionControl *>(root->parent())) root = ancestor;
                        auto controls = root->findChildren<SessionControl *>(); controls.prepend(root);
                        for (auto control : controls) {
                            ++control->generation;
                            if (control->m_profile.value("id").toString() == id) control->m_profile.clear();
                            for (int i = control->m_profiles.size()-1; i >= 0; --i)
                                if (control->m_profiles[i].toMap().value("id").toString() == id) control->m_profiles.removeAt(i);
                            emit control->accountDeleted(id); emit control->changed();
                        }
                    }
                    else { m_profile = result.value("profile").toObject().toVariantMap(); emit unlocked(); }
                }
            }
            emit changed();
            process->deleteLater();
        });
        QTimer::singleShot(5000, process, [process] { if (process->state() != QProcess::NotRunning) process->kill(); });
        process->start("python3", {"-m", "aios.chat_profiles"});
    }
};
