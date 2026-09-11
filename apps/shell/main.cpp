#include <QGuiApplication>
#include <QFile>
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
#include <QRegularExpression>
#include <QUuid>
#include <QTemporaryDir>
#include <QSettings>
#include <QLocalServer>
#include <QSysInfo>
#include <QThread>
#include "BuildInfo.h"
#include "voice.h"
#include "CameraDevice.h"
#include "SessionControl.h"
#include "ScheduledJobs.h"
#include "DisplayBridge.h"
#ifdef AIOS_EMBEDDED_DISPLAY
#include "PrivateCompositor.h"
#endif
#ifdef Q_OS_LINUX
#include <sys/prctl.h>
#include <signal.h>
#include <unistd.h>
#endif

static constexpr int ToolHostGracefulWaitMs = 15000;

static void tieToDesktop(QProcess &process) {
#ifdef Q_OS_LINUX
    const auto parent = getpid();
    process.setChildProcessModifier([parent] {
        prctl(PR_SET_PDEATHSIG, SIGTERM);
        if (getppid() != parent) _exit(1);
    });
#endif
}

static QString fileValue(const QString &path, const QString &key) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return {};
    const QByteArray prefix = key.toUtf8() + '=';
    while (!file.atEnd()) {
        const QByteArray line = file.readLine().trimmed();
        if (!line.startsWith(prefix)) continue;
        QString value = QString::fromUtf8(line.mid(prefix.size()));
        if (value.size() >= 2 && value.front() == '"' && value.back() == '"')
            value = value.mid(1, value.size() - 2);
        return value;
    }
    return {};
}

static QVariantMap systemInformation() {
    QString cpu;
    QFile cpuInfo("/proc/cpuinfo");
    if (cpuInfo.open(QIODevice::ReadOnly | QIODevice::Text)) {
        const auto match = QRegularExpression(
            R"(^model name\s*:\s*(.+)$)", QRegularExpression::MultilineOption)
            .match(QString::fromUtf8(cpuInfo.readAll()));
        if (match.hasMatch()) cpu = match.captured(1).trimmed();
    }
    if (cpu.isEmpty()) cpu = QSysInfo::currentCpuArchitecture();
    const int logicalCpus = QThread::idealThreadCount();
    if (logicalCpus > 0)
        cpu += QString(" · %1 logical CPU%2").arg(logicalCpus).arg(logicalCpus == 1 ? "" : "s");

    QString memory = "Unknown";
    QFile memoryInfo("/proc/meminfo");
    if (memoryInfo.open(QIODevice::ReadOnly | QIODevice::Text)) {
        const auto match = QRegularExpression(R"(^MemTotal:\s+(\d+)\s+kB$)", QRegularExpression::MultilineOption)
            .match(QString::fromUtf8(memoryInfo.readAll()));
        if (match.hasMatch()) {
            const double gibibytes = match.captured(1).toDouble() / (1024.0 * 1024.0);
            memory = gibibytes >= 1.0
                ? QString::number(gibibytes, 'f', gibibytes >= 10.0 ? 0 : 1) + " GiB"
                : QString::number(match.captured(1).toLongLong() / 1024) + " MiB";
        }
    }

    QString operatingSystem = fileValue("/etc/os-release", "PRETTY_NAME");
    if (operatingSystem.isEmpty()) operatingSystem = QSysInfo::prettyProductName();
    return {
        {"version", AIOS_VERSION},
        {"build", AIOS_BUILD_NUMBER},
        {"commit", AIOS_COMMIT},
        {"os", operatingSystem},
        {"kernel", QSysInfo::kernelType() + " " + QSysInfo::kernelVersion()},
        {"architecture", QSysInfo::currentCpuArchitecture()},
        {"cpu", cpu},
        {"memory", memory}
    };
}

class Backend : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList messages READ messages NOTIFY changed)
    Q_PROPERTY(QVariantMap config READ config NOTIFY changed)
    Q_PROPERTY(QVariantMap localModels READ localModels NOTIFY changed)
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
    Q_PROPERTY(int volume READ volume NOTIFY volumeChanged)
    Q_PROPERTY(bool muted READ muted NOTIFY volumeChanged)
    Q_PROPERTY(bool volumeAvailable READ volumeAvailable NOTIFY volumeChanged)
    Q_PROPERTY(QVariantMap systemInfo READ systemInfo CONSTANT)
public:
    QVariantList messages() const { return m_messages; }
    QVariantMap config() const { return m_config; }
    QVariantMap localModels() const { return m_localModels; }
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
    int volume() const { return m_volume; }
    bool muted() const { return m_muted; }
    bool volumeAvailable() const { return m_volumeAvailable; }
    QVariantMap systemInfo() const { return m_systemInfo; }
    explicit Backend(Backend *shared = nullptr) : QObject(shared), owner(shared), voice(this) {
        connect(&voice, &Voice::changed, this, &Backend::changed);
        connect(&voice, &Voice::error, this, [this](const QString &text) { m_status = text; emit changed(); });
        connect(&voice, &Voice::recorded, this, [this](const QString &path) {
            m_busy = true; m_status = "Transcribing…"; emit changed(); run({{"action", "transcribe"}, {"path", path}});
        });
        tieToDesktop(local);
        tieToDesktop(tools);
        connect(&local, &QProcess::errorOccurred, this, [this] {
            m_status = "Local model could not start. Check the model file and available memory."; emit changed();
        });
        connect(&local, &QProcess::readyReadStandardError, this, [this] {
            // Drain readiness diagnostics. Never render logs as chat content.
            local.readAllStandardError();
        });
        connect(&local, &QProcess::readyReadStandardOutput, this, [this] { local.readAllStandardOutput(); });
        connect(&local, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this,
            [this](int code, QProcess::ExitStatus) {
                if (code != 0 && m_config.value("mode") == "local") { m_status = "Local model stopped. Check model compatibility and available memory."; emit changed(); }
                else if (!m_busy && m_config.value("mode") == "local") { m_status = "Ready"; emit changed(); }
            });
        startDesktopControls();
        if (owner) {
            sessionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
            m_config = owner->config();
            connect(owner, &Backend::changed, this, [this] { m_config = owner->config(); emit changed(); });
        } else if (qEnvironmentVariableIsEmpty("AIOS_SESSION_SOCKET"))
            QTimer::singleShot(0, this, [this] { run({{"action", "load"}}); });
    }
    ~Backend() {
        voice.cancel();
        tools.terminate(); if (!tools.waitForFinished(ToolHostGracefulWaitMs)) { tools.kill(); tools.waitForFinished(1000); }
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
    Q_INVOKABLE void setAuthenticationState(const QString &state) {
        if (state == "authenticated" || state == "unavailable" || state == "awaiting_user")
            authenticationState = state;
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
        if (tools.state() != QProcess::NotRunning) {
            tools.terminate();
            if (!tools.waitForFinished(ToolHostGracefulWaitMs)) { tools.kill(); tools.waitForFinished(1000); }
        }
        m_busy = false; m_status = "Stopped"; persist(); emit changed();
    }
    Q_INVOKABLE void newChat() { if (m_busy) stop(); m_messages.clear(); m_status.clear(); persist(); emit changed(); }
    Q_INVOKABLE void copy(const QString &text) { QGuiApplication::clipboard()->setText(text); }
    Q_INVOKABLE void terminal() { QProcess::startDetached("aios-terminal", {}); }
    Q_INVOKABLE void refreshVolume() {
        if (m_volumeRefreshing) {
            m_volumeRefreshPending = true;
            return;
        }
        m_volumeRefreshing = true;
        auto process = new QProcess(this);
        connect(process, &QProcess::errorOccurred, this, [this,process](QProcess::ProcessError error) {
            if (error != QProcess::FailedToStart) return;
            process->deleteLater();
            setVolumeAvailable(false);
            finishVolumeRefresh();
        });
        connect(process, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this,
            [this,process](int code, QProcess::ExitStatus) {
                const QString output = QString::fromUtf8(process->readAllStandardOutput());
                process->deleteLater();
                const auto match = QRegularExpression(R"((\d+)%))").match(output);
                if (code != 0 || !match.hasMatch()) {
                    setVolumeAvailable(false);
                    finishVolumeRefresh();
                    return;
                }
                const int currentVolume = qBound(0, match.captured(1).toInt(), 100);
                auto muteProcess = new QProcess(this);
                connect(muteProcess, &QProcess::errorOccurred, this, [this,muteProcess](QProcess::ProcessError error) {
                    if (error != QProcess::FailedToStart) return;
                    muteProcess->deleteLater();
                    setVolumeAvailable(false);
                    finishVolumeRefresh();
                });
                connect(muteProcess, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this,
                    [this,muteProcess,currentVolume](int muteCode, QProcess::ExitStatus) {
                        const QString muteOutput = QString::fromUtf8(muteProcess->readAllStandardOutput());
                        muteProcess->deleteLater();
                        const auto muteMatch = QRegularExpression(
                            R"(Mute:\s*(yes|no))", QRegularExpression::CaseInsensitiveOption).match(muteOutput);
                        if (muteCode != 0 || !muteMatch.hasMatch()) {
                            setVolumeAvailable(false);
                            finishVolumeRefresh();
                            return;
                        }
                        setVolumeState(currentVolume, muteMatch.captured(1).compare("yes", Qt::CaseInsensitive) == 0);
                        finishVolumeRefresh();
                    });
                muteProcess->start("pactl", {"get-sink-mute", "@DEFAULT_SINK@"});
            });
        process->start("pactl", {"get-sink-volume", "@DEFAULT_SINK@"});
    }
    Q_INVOKABLE void setVolume(int volume) {
        runVolumeCommand({"set-sink-volume", "@DEFAULT_SINK@", QString::number(qBound(0, volume, 100)) + "%"});
    }
    Q_INVOKABLE void setMuted(bool muted) {
        runVolumeCommand({"set-sink-mute", "@DEFAULT_SINK@", muted ? "1" : "0"});
    }
    Q_INVOKABLE void openSystemSettings(const QString &section) {
        QString program;
        QStringList args;
        if (section == "sound") program = "pavucontrol";
        else if (section == "display") program = "arandr";
        else if (section == "network") { program = "aios-terminal"; args = {"-title", "AIOS Network & Wi-Fi", "-e", "nmtui"}; }
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
    Q_INVOKABLE QString defaultRecognitionCamera() const {
        return CameraDevice::stablePath();
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
        if (!QProcess::startDetached("aios-browser", {
                "--url", url.toString(),
                "--theme", m_config.value("theme_color", "blue").toString()
            })) {
            m_status = "Open the sign-in address in your browser.";
            emit changed();
        }
    }
    Q_INVOKABLE void refreshLocalModels() {
        if (!m_busy && !m_configuring) run({{"action", "local-models"}});
    }
    Q_INVOKABLE void setupLocal(const QString &modelId = "qwen3-0.6b") {
        if (m_busy || m_configuring) return;
        m_busy = true; m_status = "Checking local model…"; emit changed();
        run({{"action", "setup-local"}, {"model_id", modelId}});
    }
    Q_INVOKABLE bool setupPending() const {
        return !QSettings("aios", "setup").value("dismissed", false).toBool();
    }
    Q_INVOKABLE void dismissSetup() {
        QSettings settings("aios", "setup");
        settings.setValue("dismissed", true);
        settings.sync();
        if (settings.status() != QSettings::NoError) {
            m_status = "Could not save setup preference. Setup may appear again next time.";
            emit changed();
        }
    }
signals:
    void loaded();
    void changed();
    void configured();
    void transcribed(const QString &text);
    void volumeChanged();
    void authenticationRequested();
private:
    QLocalServer desktopControls;
    QString authenticationState = "unavailable";
    void startDesktopControls() {
        if (!toolDirectory.isValid()) return;
        desktopControls.setSocketOptions(QLocalServer::UserAccessOption);
        connect(&desktopControls, &QLocalServer::newConnection, this, [this] {
            while (desktopControls.hasPendingConnections()) {
                auto socket = desktopControls.nextPendingConnection();
                socket->setParent(this);
                socket->setReadBufferSize(4097);
                auto buffer = new QByteArray;
                connect(socket, &QObject::destroyed, [buffer] { delete buffer; });
                connect(socket, &QLocalSocket::disconnected, socket, &QObject::deleteLater);
                QTimer::singleShot(2000, socket, [socket] { socket->abort(); socket->deleteLater(); });
                connect(socket, &QLocalSocket::readyRead, this, [this, socket, buffer] {
                    buffer->append(socket->readAll());
                    if (buffer->size() > 4096) { socket->abort(); return; }
                    if (!buffer->contains('\n')) return;
                    const auto request = QJsonDocument::fromJson(buffer->left(buffer->indexOf('\n'))).object();
                    const auto action = request.value("action").toString();
                    QJsonObject reply{{"error", "Unsupported desktop request."}};
                    if (action == "ping" && request.size() == 1) {
                        reply = {{"result", QJsonObject{{"available", true}}}};
                    } else if (action == "appearance" && request.size() == 3) {
                        const auto setting = request.value("setting").toString();
                        const auto value = request.value("value");
                        const QStringList colors{"blue", "teal", "sage", "amber", "copper", "rose", "violet", "slate"};
                        if ((setting == "theme_color" && value.isString() && colors.contains(value.toString())) ||
                            (setting == "reduced_motion" && value.isBool())) {
                            auto target = owner ? owner : this;
                            target->m_config[setting] = value.toVariant();
                            emit target->changed();
                            emit target->configured();
                            reply = {{"result", QJsonObject{{"applied", true}}}};
                        }
                    } else if (action == "open" && request.size() == 2) {
                        const auto section = request.value("section").toString();
                        QString program;
                        QStringList args;
                        if (section == "sound") program = "pavucontrol";
                        else if (section == "display") program = "arandr";
                        else if (section == "network") { program = "aios-terminal"; args = {"-title", "AIOS Network & Wi-Fi", "-e", "nmtui"}; }
                        if (!program.isEmpty()) reply = {{"result", QJsonObject{{"opened", QProcess::startDetached(program, args)}}}};
                    } else if (action == "authenticate" && request.size() == 1) {
                        authenticationState = "unavailable";
                        emit authenticationRequested();
                        reply = {{"result", QJsonObject{{"status", authenticationState}}}};
                    } else if (action == "authentication_status" && request.size() == 1) {
                        reply = {{"result", QJsonObject{{"status", authenticationState}}}};
                    }
                    socket->write(QJsonDocument(reply).toJson(QJsonDocument::Compact) + '\n');
                    socket->disconnectFromServer();
                });
            }
        });
        desktopControls.listen(toolDirectory.path() + "/desktop.sock");
    }
    Backend *owner = nullptr;
    QString sessionId;
    int openSessions = 0;
    QStringList attachmentNames, attachmentText;
    Voice voice;
    QVariantList m_messages;
    QVariantMap m_config, pendingConfig, m_subscription, m_localModels;
    const QVariantMap m_systemInfo = systemInformation();
    QString m_loginUrl, m_loginCode;
    QString m_status;
    bool m_busy = false;
    bool m_configuring = false;
    QProcess *active = nullptr;
    QProcess local; // Readiness client only; the desktop runtime owns llama-server.
    QProcess tools;
    QTemporaryDir toolDirectory;
    int m_volume = 50;
    bool m_muted = false;
    bool m_volumeAvailable = false;
    bool m_volumeRefreshing = false;
    bool m_volumeRefreshPending = false;
    void setVolumeAvailable(bool available) {
        if (m_volumeAvailable == available) return;
        m_volumeAvailable = available;
        emit volumeChanged();
    }
    void setVolumeState(int volume, bool muted) {
        const bool stateChanged = m_volume != volume || m_muted != muted || !m_volumeAvailable;
        m_volume = volume;
        m_muted = muted;
        m_volumeAvailable = true;
        if (stateChanged) emit volumeChanged();
    }
    void finishVolumeRefresh() {
        m_volumeRefreshing = false;
        if (!m_volumeRefreshPending) return;
        m_volumeRefreshPending = false;
        QTimer::singleShot(0, this, &Backend::refreshVolume);
    }
    void runVolumeCommand(const QStringList &arguments) {
        auto process = new QProcess(this);
        connect(process, &QProcess::errorOccurred, this, [this,process](QProcess::ProcessError error) {
            if (error != QProcess::FailedToStart) return;
            process->deleteLater();
            setVolumeAvailable(false);
            m_status = "Could not adjust speaker volume. Open Sound settings to check the audio service.";
            emit changed();
        });
        connect(process, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this,
            [this,process](int code, QProcess::ExitStatus) {
                process->deleteLater();
                if (code != 0) {
                    setVolumeAvailable(false);
                    m_status = "Could not adjust speaker volume. Open Sound settings to check the audio service.";
                    emit changed();
                    return;
                }
                refreshVolume();
            });
        process->start("pactl", arguments);
    }
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
            QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
            env.insert("PYTHONPATH", env.value("AIOS_PYTHONPATH").isEmpty()
                       ? "/usr/local/share/aios" : env.value("AIOS_PYTHONPATH"));
            local.setProcessEnvironment(env);
            local.start("python3", {"-m", "aios.local_runtime", "ready"});
            m_status = "Local model starting. You can chat when it is ready.";
        }
    }
    void run(QJsonObject request) {
        auto p = new QProcess(this);
        tieToDesktop(*p);
        auto buffer = new QByteArray;
        const auto action = request.value("action").toString();
        if (action != "load" && action != "configure" && action != "local-models") active = p;
        QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
        if (env.value("AIOS_PYTHONPATH").isEmpty()) env.insert("PYTHONPATH", "/usr/local/share/aios");
        else env.insert("PYTHONPATH", env.value("AIOS_PYTHONPATH"));
        if (action == "chat" && toolDirectory.isValid()) {
            const auto socket = toolDirectory.path() + "/tools.sock";
            if (tools.state() == QProcess::NotRunning) {
                env.insert("AIOS_BROWSER_SESSION", sessionId);
                if (desktopControls.isListening()) env.insert("AIOS_DESKTOP_CONTROL_SOCKET", desktopControls.fullServerName());
                env.insert("AIOS_BROWSER_THEME", m_config.value("theme_color", "blue").toString());
                tools.setProcessEnvironment(env);
                tools.setStandardOutputFile(QProcess::nullDevice());
                tools.setStandardErrorFile(QProcess::nullDevice());
                QFile::remove(socket);
                tools.start("python3", {"-m", "aios.toolhost", socket});
            }
            request.insert("tool_socket", socket);
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
                if (value.contains("local_models")) m_localModels = value.value("local_models").toObject().toVariantMap();
                if (type == "loaded") {
                    m_config = value.value("config").toObject().toVariantMap();
                    m_messages = value.value("messages").toArray().toVariantList(); startLocal();
                    emit loaded();
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
                else if (type == "error") {
                    if (action == "configure") { pendingConfig.clear(); m_configuring = false; }
                    m_status = value.value("text").toString();
                }
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
                    for (auto it = pendingConfig.begin(); it != pendingConfig.end(); ++it) if (it.key() != "api_key" && it.key() != "voice_key" && it.key() != "agent_api_key") m_config[it.key()] = it.value();
                    pendingConfig.clear(); m_status = "Saved"; if (modelChanged) startLocal(); emit configured();
                }
                emit changed();
            }
        });
        connect(p, &QProcess::errorOccurred, this, [this,p,action](QProcess::ProcessError) {
            if (action == "configure") { pendingConfig.clear(); m_configuring = false; }
            m_status = "The AIOS backend could not start.";
            if (active == p) { active = nullptr; m_busy = false; } emit changed();
        });
        connect(p, qOverload<int,QProcess::ExitStatus>(&QProcess::finished), this, [this,p,action,request](int, QProcess::ExitStatus) {
            if (action == "subscription") { m_loginUrl.clear(); m_loginCode.clear(); }
            if (action == "configure") { pendingConfig.clear(); m_configuring = false; emit changed(); }
            if (action == "transcribe") QFile::remove(request.value("path").toString());
            if (active == p) { active = nullptr; m_busy = false; emit changed(); } p->deleteLater();
        });
        p->start("python3", {"-m", "aios.worker"});
    }
};

int main(int argc, char **argv) {
#ifdef AIOS_EMBEDDED_DISPLAY
    if (!qEnvironmentVariableIsEmpty("AIOS_SESSION_SOCKET"))
        QQuickWindow::setGraphicsApi(QSGRendererInterface::OpenGL);
    qmlRegisterType<PrivateCompositor>("AIOS.Display", 1, 0, "PrivateCompositor");
#endif
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
    app.setApplicationVersion(AIOS_VERSION);
    app.setOrganizationName("AIOS");
    app.setQuitOnLastWindowClosed(false);
    Backend backend;
    SessionControl sessionControl;
    ScheduledJobs scheduledJobs;
    // The global client is desktop-only. Protected native widgets obtain a
    // separate lease/work-bound instance from SessionControl.
    QObject::connect(&sessionControl, &SessionControl::privacyLost, &scheduledJobs, &ScheduledJobs::invalidate);
    DisplayBridge displayBridge;
    QObject::connect(&sessionControl, &SessionControl::displayRequested, &app, [&] {
        if (!displayBridge.enabled()) sessionControl.displayFailed();
    });
    QObject::connect(&sessionControl, &SessionControl::privacyLost, &app, [] {
        QGuiApplication::clipboard()->clear();
    });
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("backend", &backend);
    engine.rootContext()->setContextProperty("sessionControl", &sessionControl);
    engine.rootContext()->setContextProperty("scheduledJobs", &scheduledJobs);
    engine.rootContext()->setContextProperty("displayBridge", &displayBridge);
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed, &app, [] { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.load(QUrl("qrc:/Main.qml"));
    const auto arguments = app.arguments();
    if (arguments.contains("--prepare-display") && displayBridge.enabled()) {
        QTimer::singleShot(1500, &sessionControl, [&sessionControl] {
            emit sessionControl.displayRequested("");
        });
    }
    if (arguments.contains("--chat") && !engine.rootObjects().isEmpty())
        QMetaObject::invokeMethod(engine.rootObjects().first(), "openChat");
    const int capture = arguments.indexOf("--capture");
    if (capture >= 0 && capture + 1 < arguments.size()) {
        QTimer::singleShot(arguments.contains("--prepare-display") ? 5000 : 1500, &app, [&app, arguments, capture] {
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
