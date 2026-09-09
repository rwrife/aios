#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QProcess>
#include <QProcessEnvironment>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QTimer>
#include <QClipboard>
#include <QDir>
#include <QStandardPaths>
#include <QSaveFile>
#include <QQuickWindow>
#include <QPalette>
#include <QFont>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QUuid>
#include <QTemporaryDir>
#include <QDesktopServices>
#include "voice.h"
#include "SessionControl.h"
#ifdef Q_OS_LINUX
#include <sys/prctl.h>
#include <signal.h>
#include <unistd.h>
#endif

static void tieToDesktop(QProcess &process) {
#ifdef Q_OS_LINUX
    const auto parent = getpid();
    process.setChildProcessModifier([parent] {
        prctl(PR_SET_PDEATHSIG, SIGTERM);
        if (getppid() != parent) _exit(1);
    });
#endif
}

class Backend : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList messages READ messages NOTIFY changed)
    Q_PROPERTY(QVariantMap config READ config NOTIFY changed)
    Q_PROPERTY(QString status READ status NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool configuring READ configuring NOTIFY changed)
    Q_PROPERTY(QStringList attachments READ attachments NOTIFY changed)
    Q_PROPERTY(bool recording READ recording NOTIFY changed)
    Q_PROPERTY(bool speaking READ speaking NOTIFY changed)
    Q_PROPERTY(int sessionCount READ sessionCount NOTIFY changed)
    Q_PROPERTY(QVariantMap subscription READ subscription NOTIFY changed)
    Q_PROPERTY(QString loginUrl READ loginUrl NOTIFY changed)
    Q_PROPERTY(QString loginCode READ loginCode NOTIFY changed)
public:
    QVariantList messages() const { return m_messages; }
    QVariantMap config() const { return m_config; }
    QString status() const { return m_status; }
    bool busy() const { return m_busy; }
    bool configuring() const { return m_configuring; }
    QStringList attachments() const { return attachmentNames; }
    bool recording() const { return voice.recording(); }
    bool speaking() const { return voice.speaking(); }
    int sessionCount() const { return openSessions; }
    QVariantMap subscription() const { return m_subscription; }
    QString loginUrl() const { return m_loginUrl; }
    QString loginCode() const { return m_loginCode; }
    explicit Backend(Backend *shared = nullptr) : QObject(shared), owner(shared), voice(this) {
        connect(&voice, &Voice::changed, this, &Backend::changed);
        connect(&voice, &Voice::error, this, [this](const QString &text) { m_status = text; emit changed(); });
        connect(&voice, &Voice::recorded, this, [this](const QString &path) {
            m_busy = true; m_status = "Transcribing…"; emit changed(); run({{"action", "transcribe"}, {"path", path}});
        });
        tieToDesktop(local);
        tieToDesktop(browser);
        readiness.setInterval(500);
        connect(&readiness, &QTimer::timeout, this, [this] {
            if (checkingReady) return;
            checkingReady = true;
            QNetworkRequest request(QUrl("http://127.0.0.1:8080/health"));
            request.setTransferTimeout(1500);
            auto reply = network.get(request);
            connect(reply, &QNetworkReply::finished, this, [this,reply] {
                checkingReady = false;
                if (reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt() == 200) {
                    readiness.stop();
                    if (!m_busy && local.state() == QProcess::Running && m_config.value("mode") == "local") {
                        m_status = "Ready"; emit changed();
                    }
                }
                reply->deleteLater();
            });
        });
        connect(&local, &QProcess::errorOccurred, this, [this] {
            m_status = "Local model could not start. Check the model file and available memory."; emit changed();
        });
        connect(&local, &QProcess::readyReadStandardError, this, [this] {
            // Drain model diagnostics. Never render logs as chat content.
            local.readAllStandardError();
        });
        connect(&local, &QProcess::readyReadStandardOutput, this, [this] { local.readAllStandardOutput(); });
        connect(&local, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this,
            [this](int code, QProcess::ExitStatus) {
                readiness.stop();
                if (code != 0 && m_config.value("mode") == "local") { m_status = "Local model stopped. Check model compatibility and available memory."; emit changed(); }
            });
        if (owner) {
            sessionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
            m_config = owner->config();
            connect(owner, &Backend::changed, this, [this] { m_config = owner->config(); emit changed(); });
        } else if (qEnvironmentVariableIsEmpty("AIOS_SESSION_SOCKET"))
            QTimer::singleShot(0, this, [this] { run({{"action", "load"}}); });
    }
    ~Backend() {
        voice.cancel();
        browser.terminate(); if (!browser.waitForFinished(5000)) { browser.kill(); browser.waitForFinished(1000); }
        for (auto p : findChildren<QProcess *>(QString(), Qt::FindDirectChildrenOnly)) {
            p->disconnect(this); p->kill(); p->waitForFinished(1000);
        }
        local.terminate(); if (!local.waitForFinished(1500)) local.kill();
    }
    Q_INVOKABLE QObject *createSession() {
        auto session = new Backend(this); ++openSessions; emit changed();
        connect(session, &QObject::destroyed, this, [this] { --openSessions; emit changed(); });
        return session;
    }
    Q_INVOKABLE void closeSession() { if (owner) { stop(); deleteLater(); } }
    Q_INVOKABLE void attach(const QUrl &url) {
        if (m_busy || voice.recording()) return;
        if (attachmentNames.size() >= 4) { m_status = "Attach up to four files per message."; emit changed(); return; }
        if (!url.isLocalFile()) return;
        m_busy = true; m_status = "Reading attachment…"; emit changed();
        run({{"action", "attachment"}, {"path", url.toLocalFile()}});
    }
    Q_INVOKABLE void removeAttachment(int index) {
        if (index < 0 || index >= attachmentNames.size() || m_busy) return;
        attachmentNames.removeAt(index); attachmentText.removeAt(index); emit changed();
    }
    Q_INVOKABLE void microphone() {
        setVoiceActive(!voice.recording());
    }
    Q_INVOKABLE void setVoiceActive(bool enabled) {
        if (!enabled && voice.recording()) voice.finish();
        else if (enabled && !m_busy && !voice.recording()) { m_status.clear(); voice.start(); }
    }
    Q_INVOKABLE void cancelRecording() { voice.cancel(); m_status.clear(); emit changed(); }
    Q_INVOKABLE void readReply(const QString &text) {
        if (voice.speaking()) { voice.stopPlayback(); return; }
        if (m_busy || voice.recording()) return;
        m_busy = true; m_status = "Preparing spoken reply…"; emit changed();
        run({{"action", "speak"}, {"text", text}, {"path", voice.speechPath()}});
    }
    Q_INVOKABLE void setupVoice() {
        if (m_busy) return;
        m_busy = true; m_status = "Downloading speech model…"; emit changed(); run({{"action", "setup-voice"}});
    }
    Q_INVOKABLE void send(const QString &text) {
        if (m_busy || voice.recording() || (text.trimmed().isEmpty() && attachmentNames.isEmpty())) return;
        QString content = text.trimmed();
        for (int i = 0; i < attachmentNames.size(); ++i)
            content += "\n\n[Attached file: " + attachmentNames[i] + "]\n" + attachmentText[i] + "\n[End attachment]";
        QString display = text.trimmed();
        if (!attachmentNames.isEmpty()) display += "\n\n" + attachmentNames.join(" · ");
        m_messages.append(QVariantMap{{"role", "user"}, {"content", content}, {"display_text", display.trimmed()}});
        attachmentNames.clear(); attachmentText.clear();
        const auto prompt = m_messages;
        m_messages.append(QVariantMap{{"role", "assistant"}, {"content", ""}});
        m_busy = true; m_status = "Connecting…"; emit changed();
        run({{"action", "chat"}, {"messages", QJsonArray::fromVariantList(prompt)}});
    }
    Q_INVOKABLE void stop() {
        voice.cancel();
        if (active) {
            active->disconnect(this); active->terminate();
            if (!active->waitForFinished(1500)) { active->kill(); active->waitForFinished(500); }
            active->deleteLater(); active = nullptr;
        }
        m_loginUrl.clear(); m_loginCode.clear();
        if (browser.state() != QProcess::NotRunning) {
            browser.terminate();
            if (!browser.waitForFinished(5000)) { browser.kill(); browser.waitForFinished(1000); }
        }
        m_busy = false; m_status = "Stopped"; persist(); emit changed();
    }
    Q_INVOKABLE void newChat() { if (m_busy) stop(); m_messages.clear(); m_status.clear(); persist(); emit changed(); }
    Q_INVOKABLE void copy(const QString &text) { QGuiApplication::clipboard()->setText(text); }
    Q_INVOKABLE void terminal() { QProcess::startDetached("xterm", {"-fa", "DejaVu Sans Mono", "-fs", "11"}); }
    Q_INVOKABLE void openSystemSettings(const QString &section) {
        QString program;
        QStringList args;
        if (section == "sound") program = "pavucontrol";
        else if (section == "display") program = "arandr";
        else if (section == "network") { program = "xterm"; args = {"-T", "AIOS Network & Wi-Fi", "-fa", "DejaVu Sans Mono", "-fs", "11", "-e", "nmtui"}; }
        else return;
        if (!QProcess::startDetached(program, args)) {
            m_status = "Could not open " + section + " settings. Check that the system settings packages are installed.";
            emit changed();
        }
    }
    Q_INVOKABLE void power(const QString &action) {
        if (action != "reboot" && action != "poweroff") return;
        auto p = new QProcess(this);
        connect(p, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this, [this,p](int code, QProcess::ExitStatus) {
            if (code) { m_status = "Power action failed. Use the recovery terminal."; emit changed(); } p->deleteLater();
        });
        p->start("doas", {"-n", "/sbin/" + action});
    }
    Q_INVOKABLE void configure(const QVariantMap &values) {
        if (m_busy || m_configuring) return;
        m_configuring = true;
        pendingConfig = values;
        emit changed();
        run({{"action", "configure"}, {"config", QJsonObject::fromVariantMap(values)}});
    }
    Q_INVOKABLE void subscriptionAction(const QString &operation, bool device = false) {
        if (m_busy || m_configuring || (operation != "login" && operation != "logout" && operation != "status")) return;
        m_busy = true; m_loginUrl.clear(); m_loginCode.clear();
        m_status = operation == "login" ? "Starting ChatGPT sign-in…" : "Checking ChatGPT account…";
        emit changed();
        run({{"action", "subscription"}, {"operation", operation}, {"device", device}});
    }
    Q_INVOKABLE void openSubscriptionLogin() {
        const QUrl url(m_loginUrl);
        if (url.scheme() != "https" || (url.host() != "auth.openai.com" && url.host() != "chatgpt.com") || !url.userInfo().isEmpty()) return;
        if (!QDesktopServices::openUrl(url)) { m_status = "Open the sign-in address in your browser."; emit changed(); }
    }
    Q_INVOKABLE void setupLocal() {
        if (m_busy) return;
        m_busy = true; m_status = "Downloading starter model…"; emit changed();
        run({{"action", "setup-local"}});
    }
signals:
    void changed();
    void configured();
    void transcribed(const QString &text);
private:
    Backend *owner = nullptr;
    QString sessionId;
    int openSessions = 0;
    QStringList attachmentNames, attachmentText;
    Voice voice;
    QVariantList m_messages;
    QVariantMap m_config, pendingConfig, m_subscription;
    QString m_loginUrl, m_loginCode;
    QString m_status;
    bool m_busy = false;
    bool m_configuring = false;
    QProcess *active = nullptr;
    QProcess local;
    QProcess browser;
    QTemporaryDir browserDirectory;
    QNetworkAccessManager network;
    QTimer readiness;
    bool checkingReady = false;
    void persist() {
        if (sessionId.isEmpty() || m_messages.isEmpty()) return;
        const QString dir = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) + "/aios/conversations";
        QDir().mkpath(dir);
        QSaveFile file(dir + "/" + sessionId + ".json");
        if (!file.open(QIODevice::WriteOnly)) { m_status = "Could not save conversation. Check free space."; return; }
        file.setPermissions(QFile::ReadOwner | QFile::WriteOwner);
        file.write(QJsonDocument(QJsonArray::fromVariantList(m_messages)).toJson(QJsonDocument::Compact));
        if (!file.commit()) m_status = "Could not save conversation. Check free space.";
    }
    void startLocal() {
        if (local.state() != QProcess::NotRunning) {
            local.terminate(); if (!local.waitForFinished(1000)) { local.kill(); local.waitForFinished(1000); }
        }
        if (m_config.value("mode") == "local" && !m_config.value("model_path").toString().isEmpty()) {
            local.start("llama-server", {"--model", m_config.value("model_path").toString(), "--alias", "local",
                "--host", "127.0.0.1", "--port", "8080", "--ctx-size", "8192", "--jinja"});
            m_status = "Local model starting. You can chat when it is ready.";
            readiness.start();
        }
    }
    void run(QJsonObject request) {
        auto p = new QProcess(this);
        tieToDesktop(*p);
        auto buffer = new QByteArray;
        const auto action = request.value("action").toString();
        if (action != "load" && action != "configure") active = p;
        QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
        if (env.value("AIOS_PYTHONPATH").isEmpty()) env.insert("PYTHONPATH", "/usr/local/share/aios");
        else env.insert("PYTHONPATH", env.value("AIOS_PYTHONPATH"));
        if (action == "chat" && browserDirectory.isValid()) {
            const auto socket = browserDirectory.path() + "/browser.sock";
            if (browser.state() == QProcess::NotRunning) {
                browser.setProcessEnvironment(env);
                browser.setStandardOutputFile(QProcess::nullDevice());
                browser.setStandardErrorFile(QProcess::nullDevice());
                QFile::remove(socket);
                browser.start("python3", {"-m", "aios.browser", socket});
            }
            request.insert("browser_socket", socket);
        }
        p->setProcessEnvironment(env);
        connect(p, &QObject::destroyed, [buffer] { delete buffer; });
        connect(p, &QProcess::started, this, [p,request] {
            p->write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n'); p->closeWriteChannel();
        });
        connect(p, &QProcess::readyReadStandardOutput, this, [this,p,buffer,action] {
            buffer->append(p->readAllStandardOutput());
            int end;
            while ((end = buffer->indexOf('\n')) >= 0) {
                const auto value = QJsonDocument::fromJson(buffer->left(end)).object(); buffer->remove(0, end + 1);
                const auto type = value.value("type").toString();
                if (type == "loaded") {
                    m_config = value.value("config").toObject().toVariantMap();
                    m_messages = value.value("messages").toArray().toVariantList(); startLocal();
                } else if (type == "token" && !m_messages.isEmpty()) {
                    auto last = m_messages.last().toMap(); last["content"] = last.value("content").toString() + value.value("text").toString();
                    m_messages.last() = last; m_status = "Replying…";
                } else if (type == "done") { m_status.clear(); persist(); }
                else if (type == "progress") { m_status = value.value("text").toString(); }
                else if (type == "subscription-login") {
                    m_loginUrl = value.value("url").toString(); m_loginCode = value.value("code").toString();
                    m_status = "Complete sign-in in your browser.";
                    if (m_loginCode.isEmpty()) openSubscriptionLogin();
                }
                else if (type == "subscription-account") {
                    m_subscription = value.value("account").toObject().toVariantMap();
                    m_loginUrl.clear(); m_loginCode.clear();
                    m_status = m_subscription.value("signed_in").toBool() ? "ChatGPT connected" : "Signed out of ChatGPT";
                }
                else if (type == "installed") {
                    m_config["mode"] = "local"; m_config["model_path"] = value.value("path").toString();
                    startLocal(); emit configured();
                }
                else if (type == "error") { m_status = value.value("text").toString(); }
                else if (type == "attached") {
                    attachmentNames.append(value.value("name").toString()); attachmentText.append(value.value("text").toString()); m_status.clear();
                } else if (type == "transcribed") {
                    m_status.clear(); emit transcribed(value.value("text").toString());
                } else if (type == "spoken") {
                    m_status.clear(); voice.play(value.value("path").toString());
                } else if (type == "voice-installed") {
                    m_config["voice_mode"] = "local"; m_config["speech_model_path"] = value.value("path").toString();
                    m_status = "Local voice is ready"; emit configured();
                }
                else if (type == "saved" && action == "configure") {
                    const bool modelChanged = pendingConfig.value("mode", m_config.value("mode")) != m_config.value("mode") || pendingConfig.value("model_path", m_config.value("model_path")) != m_config.value("model_path");
                    for (auto it = pendingConfig.begin(); it != pendingConfig.end(); ++it) if (it.key() != "api_key" && it.key() != "voice_key") m_config[it.key()] = it.value();
                    pendingConfig.clear(); m_status = "Saved"; if (modelChanged) startLocal(); emit configured();
                }
                emit changed();
            }
        });
        connect(p, &QProcess::errorOccurred, this, [this,p,action](QProcess::ProcessError) {
            if (action == "configure") m_configuring = false;
            m_status = "The AIOS backend could not start.";
            if (active == p) { active = nullptr; m_busy = false; } emit changed();
        });
        connect(p, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this, [this,p,action,request](int, QProcess::ExitStatus) {
            if (action == "subscription") { m_loginUrl.clear(); m_loginCode.clear(); }
            if (action == "configure") { m_configuring = false; emit changed(); }
            if (action == "transcribe") QFile::remove(request.value("path").toString());
            if (active == p) { active = nullptr; m_busy = false; emit changed(); } p->deleteLater();
        });
        p->start("python3", {"-m", "aios.worker"});
    }
};

int main(int argc, char **argv) {
    qputenv("QT_QUICK_CONTROLS_STYLE", "Basic");
    QGuiApplication app(argc, argv);
    app.setFont(QFont("DejaVu Sans", 10));
    QPalette palette;
    palette.setColor(QPalette::Window, QColor("#172633"));
    palette.setColor(QPalette::Base, QColor("#203340"));
    palette.setColor(QPalette::Text, QColor("#f1f5f6"));
    palette.setColor(QPalette::WindowText, QColor("#f1f5f6"));
    palette.setColor(QPalette::Button, QColor("#203340"));
    palette.setColor(QPalette::ButtonText, QColor("#f1f5f6"));
    palette.setColor(QPalette::Highlight, QColor("#bde4e6"));
    palette.setColor(QPalette::HighlightedText, QColor("#101b27"));
    app.setPalette(palette);
    app.setApplicationName("AIOS");
    app.setOrganizationName("AIOS");
    app.setQuitOnLastWindowClosed(false);
    Backend backend;
    SessionControl sessionControl;
    QObject::connect(&sessionControl, &SessionControl::privacyLost, &app, [] {
        QGuiApplication::clipboard()->clear();
    });
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("backend", &backend);
    engine.rootContext()->setContextProperty("sessionControl", &sessionControl);
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed, &app, [] { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.load(QUrl("qrc:/Main.qml"));
    const auto arguments = app.arguments();
    if (arguments.contains("--chat") && !engine.rootObjects().isEmpty())
        QMetaObject::invokeMethod(engine.rootObjects().first(), "openChat");
    const int capture = arguments.indexOf("--capture");
    if (capture >= 0 && capture + 1 < arguments.size()) {
        QTimer::singleShot(1500, &app, [&app, arguments, capture] {
            bool saved = false;
            for (auto window : app.allWindows()) {
                if (window->isVisible() && (window->title() == "AIOS Chat" || !arguments.contains("--chat"))) {
                    auto quick = qobject_cast<QQuickWindow *>(window);
                    if (quick) { saved = quick->grabWindow().save(arguments[capture + 1]); if (saved) break; }
                }
            }
            app.exit(saved ? 0 : 1);
        });
    }
    return app.exec();
}
#include "main.moc"
