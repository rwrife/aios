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
public:
    QVariantList messages() const { return m_messages; }
    QVariantMap config() const { return m_config; }
    QString status() const { return m_status; }
    bool busy() const { return m_busy; }
    Backend() {
        tieToDesktop(local);
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
        QTimer::singleShot(0, this, [this] { run({{"action", "load"}}); });
    }
    ~Backend() { local.terminate(); if (!local.waitForFinished(1500)) local.kill(); }
    Q_INVOKABLE void send(const QString &text) {
        if (m_busy || text.trimmed().isEmpty()) return;
        m_messages.append(QVariantMap{{"role", "user"}, {"content", text.trimmed()}});
        const auto prompt = m_messages;
        m_messages.append(QVariantMap{{"role", "assistant"}, {"content", ""}});
        m_busy = true; m_status = "Connecting…"; emit changed();
        run({{"action", "chat"}, {"messages", QJsonArray::fromVariantList(prompt)}});
    }
    Q_INVOKABLE void stop() {
        if (active) { active->disconnect(this); active->kill(); active->deleteLater(); active = nullptr; }
        m_busy = false; m_status = "Stopped"; persist(); emit changed();
    }
    Q_INVOKABLE void newChat() { if (m_busy) stop(); m_messages.clear(); m_status.clear(); persist(); emit changed(); }
    Q_INVOKABLE void copy(const QString &text) { QGuiApplication::clipboard()->setText(text); }
    Q_INVOKABLE void terminal() { QProcess::startDetached("xterm", {"-fa", "DejaVu Sans Mono", "-fs", "11"}); }
    Q_INVOKABLE void power(const QString &action) {
        if (action != "reboot" && action != "poweroff") return;
        auto p = new QProcess(this);
        connect(p, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this, [this,p](int code, QProcess::ExitStatus) {
            if (code) { m_status = "Power action failed. Use the recovery terminal."; emit changed(); } p->deleteLater();
        });
        p->start("doas", {"-n", "/sbin/" + action});
    }
    Q_INVOKABLE void configure(const QVariantMap &values) {
        if (m_busy) return;
        pendingConfig = values;
        run({{"action", "configure"}, {"config", QJsonObject::fromVariantMap(values)}});
    }
    Q_INVOKABLE void setupLocal() {
        if (m_busy) return;
        m_busy = true; m_status = "Downloading starter model…"; emit changed();
        run({{"action", "setup-local"}});
    }
signals:
    void changed();
    void configured();
private:
    QVariantList m_messages;
    QVariantMap m_config, pendingConfig;
    QString m_status;
    bool m_busy = false;
    QProcess *active = nullptr;
    QProcess local;
    QNetworkAccessManager network;
    QTimer readiness;
    bool checkingReady = false;
    void persist() {
        const QString dir = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) + "/aios";
        QDir().mkpath(dir);
        QSaveFile file(dir + "/conversation.json");
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
                "--host", "127.0.0.1", "--port", "8080", "--ctx-size", "4096"});
            m_status = "Local model starting. You can chat when it is ready.";
            readiness.start();
        }
    }
    void run(const QJsonObject &request) {
        auto p = new QProcess(this);
        tieToDesktop(*p);
        auto buffer = new QByteArray;
        const auto action = request.value("action").toString();
        if (action == "chat" || action == "setup-local") active = p;
        QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
        if (env.value("AIOS_PYTHONPATH").isEmpty()) env.insert("PYTHONPATH", "/usr/local/share/aios");
        else env.insert("PYTHONPATH", env.value("AIOS_PYTHONPATH"));
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
                else if (type == "installed") {
                    m_config["mode"] = "local"; m_config["model_path"] = value.value("path").toString();
                    startLocal(); emit configured();
                }
                else if (type == "error") { m_status = value.value("text").toString(); }
                else if (type == "saved" && action == "configure") {
                    for (auto it = pendingConfig.begin(); it != pendingConfig.end(); ++it) if (it.key() != "api_key") m_config[it.key()] = it.value();
                    pendingConfig.clear(); m_status = "Saved"; startLocal(); emit configured();
                }
                emit changed();
            }
        });
        connect(p, &QProcess::errorOccurred, this, [this,p](QProcess::ProcessError) {
            m_status = "The AIOS backend could not start.";
            if (active == p) { active = nullptr; m_busy = false; } emit changed();
        });
        connect(p, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this, [this,p](int, QProcess::ExitStatus) {
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
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("backend", &backend);
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
