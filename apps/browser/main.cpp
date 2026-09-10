// Hallmark · component: browser chrome · genre: modern-minimal · theme: AIOS Ocean
// Hallmark · pre-emit critique: P5 H5 E5 S5 R5 V4
#include <QApplication>
#include <QBoxLayout>
#include <QCloseEvent>
#include <QColor>
#include <QCommandLineParser>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFont>
#include <QHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLineEdit>
#include <QLocalServer>
#include <QLocalSocket>
#include <QMainWindow>
#include <QPalette>
#include <QPointer>
#include <QProgressBar>
#include <QPushButton>
#include <QSaveFile>
#include <QSet>
#include <QStandardPaths>
#include <QTimer>
#include <QUrl>
#include <QWebEngineCertificateError>
#include <QWebEngineDownloadRequest>
#include <QWebEngineFullScreenRequest>
#include <QWebEngineHistory>
#include <QWebEngineLoadingInfo>
#include <QWebEnginePage>
#include <QWebEnginePermission>
#include <QWebEngineProfile>
#include <QWebEngineScript>
#include <QWebEngineSettings>
#include <QWebEngineView>
#include <QWidget>

#include <functional>

namespace {

constexpr int kMaxRequestBytes = 32768;
constexpr int kMaxResponseBytes = 128 * 1024;
constexpr int kOperationTimeoutMs = 35000;

const QStringList kActions = {
    "open", "navigate", "snapshot", "click", "type", "press", "scroll",
    "back", "forward", "reload", "stop", "tabs", "switch", "close"
};

struct Palette {
    QColor night;
    QColor horizon;
    QColor panel;
    QColor input;
    QColor ink;
    QColor muted;
    QColor accent;
    QColor line;
    QColor wave;
};

Palette paletteFor(const QString &name)
{
    if (name == "blue" || name.isEmpty()) {
        return {
            QColor("#101b27"), QColor("#354e60"), QColor("#172633"),
            QColor("#203340"), QColor("#f1f5f6"), QColor("#b2c3cd"),
            QColor("#bde4e6"), QColor("#4c6574"), QColor("#a7c7d5")
        };
    }
    const QHash<QString, qreal> hues = {
        {"teal", 0.48}, {"sage", 0.30}, {"amber", 0.11}, {"copper", 0.055},
        {"rose", 0.96}, {"violet", 0.73}, {"slate", 0.60}
    };
    const qreal hue = hues.value(name, 0.57);
    const qreal saturation = name == "slate" ? 0.055 : 0.24;
    const auto hsl = [hue, saturation](qreal lightness, qreal saturationScale = 1.0) {
        return QColor::fromHslF(hue, saturation * saturationScale, lightness);
    };
    return {
        hsl(0.10), hsl(0.29), hsl(0.145), hsl(0.19), QColor("#f1f5f6"),
        hsl(0.74, 0.55), hsl(0.81), hsl(0.38), hsl(0.74)
    };
}

QString cssColor(const QColor &color)
{
    return color.name(QColor::HexRgb);
}

bool allowedUrl(const QUrl &url)
{
    return url.isValid()
        && (url.scheme() == "http" || url.scheme() == "https")
        && !url.host().isEmpty()
        && url.userName().isEmpty()
        && url.password().isEmpty()
        && url.toString().size() <= 8192;
}

QString jsString(const QString &value)
{
    const auto encoded = QJsonDocument(QJsonArray{value}).toJson(QJsonDocument::Compact);
    return QString::fromUtf8(encoded.mid(1, encoded.size() - 2));
}

QJsonValue jsonValue(const QVariant &value)
{
    return QJsonValue::fromVariant(value);
}

class RestrictedPage final : public QWebEnginePage {
    Q_OBJECT
public:
    explicit RestrictedPage(QWebEngineProfile *profile, QObject *parent = nullptr)
        : QWebEnginePage(profile, parent) {}

signals:
    void blocked(const QString &message);

protected:
    bool acceptNavigationRequest(const QUrl &url, NavigationType type, bool mainFrame) override
    {
        Q_UNUSED(type)
        if (!mainFrame || url == QUrl("about:blank") || allowedUrl(url))
            return true;
        emit blocked("Browser navigation requires an HTTP(S) URL without embedded credentials.");
        return false;
    }

    QWebEnginePage *createWindow(WebWindowType type) override
    {
        Q_UNUSED(type)
        emit blocked("Popup windows are not available in the AIOS browser.");
        return nullptr;
    }

    QStringList chooseFiles(FileSelectionMode mode, const QStringList &oldFiles,
                            const QStringList &acceptedMimeTypes) override
    {
        Q_UNUSED(mode)
        Q_UNUSED(oldFiles)
        Q_UNUSED(acceptedMimeTypes)
        emit blocked("File uploads are not available in the AIOS browser.");
        return {};
    }

    void javaScriptAlert(const QUrl &origin, const QString &message) override
    {
        Q_UNUSED(origin)
        Q_UNUSED(message)
    }

    bool javaScriptConfirm(const QUrl &origin, const QString &message) override
    {
        Q_UNUSED(origin)
        Q_UNUSED(message)
        return false;
    }

    bool javaScriptPrompt(const QUrl &origin, const QString &message,
                          const QString &defaultValue, QString *result) override
    {
        Q_UNUSED(origin)
        Q_UNUSED(message)
        Q_UNUSED(defaultValue)
        if (result)
            result->clear();
        return false;
    }
};

class BrowserWindow final : public QMainWindow {
    Q_OBJECT
public:
    BrowserWindow(QString socketPath, QString sessionId, const QString &themeName)
        : m_socketPath(std::move(socketPath)),
          m_sessionId(std::move(sessionId)),
          m_palette(paletteFor(themeName))
    {
        setWindowTitle("AIOS Browser");
        setMinimumSize(640, 480);
        resize(1100, 760);

        auto frame = new QWidget(this);
        frame->setObjectName("browserFrame");
        auto layout = new QVBoxLayout(frame);
        layout->setContentsMargins(1, 1, 1, 1);
        layout->setSpacing(0);

        auto bar = new QWidget(frame);
        bar->setObjectName("browserBar");
        bar->setFixedHeight(56);
        auto controls = new QHBoxLayout(bar);
        controls->setContentsMargins(12, 8, 12, 8);
        controls->setSpacing(8);

        m_back = new QPushButton(QString::fromUtf8("←"), bar);
        m_back->setObjectName("browserControl");
        m_back->setAccessibleName("Back");
        m_back->setToolTip("Back");
        m_back->setFixedSize(38, 38);

        m_refresh = new QPushButton(QString::fromUtf8("↻"), bar);
        m_refresh->setObjectName("browserControl");
        m_refresh->setAccessibleName("Refresh");
        m_refresh->setToolTip("Refresh");
        m_refresh->setFixedSize(38, 38);

        m_stop = new QPushButton(QString::fromUtf8("×"), bar);
        m_stop->setObjectName("browserControl");
        m_stop->setAccessibleName("Stop loading");
        m_stop->setToolTip("Stop loading");
        m_stop->setFixedSize(38, 38);
        m_stop->setEnabled(false);

        m_address = new QLineEdit(bar);
        m_address->setObjectName("browserAddress");
        m_address->setAccessibleName("Web address");
        m_address->setPlaceholderText("Enter a web address");
        m_address->setClearButtonEnabled(true);

        controls->addWidget(m_back);
        controls->addWidget(m_refresh);
        controls->addWidget(m_stop);
        controls->addWidget(m_address, 1);
        layout->addWidget(bar);

        m_progress = new QProgressBar(frame);
        m_progress->setObjectName("browserProgress");
        m_progress->setTextVisible(false);
        m_progress->setFixedHeight(2);
        m_progress->setRange(0, 100);
        m_progress->hide();
        layout->addWidget(m_progress);

        m_profile = new QWebEngineProfile(this);
        m_profile->setHttpCacheType(QWebEngineProfile::MemoryHttpCache);
        m_profile->setPersistentCookiesPolicy(QWebEngineProfile::NoPersistentCookies);
        m_profile->setPersistentPermissionsPolicy(
            QWebEngineProfile::PersistentPermissionsPolicy::StoreInMemory);
        m_profile->setSpellCheckEnabled(false);

        m_view = new QWebEngineView(frame);
        m_view->setObjectName("browserView");
        m_view->setContextMenuPolicy(Qt::NoContextMenu);
        m_view->setAcceptDrops(false);
        m_page = new RestrictedPage(m_profile, m_view);
        m_view->setPage(m_page);
        layout->addWidget(m_view, 1);
        setCentralWidget(frame);

        auto settings = m_page->settings();
        settings->setAttribute(QWebEngineSettings::LocalContentCanAccessFileUrls, false);
        settings->setAttribute(QWebEngineSettings::LocalContentCanAccessRemoteUrls, false);
        settings->setAttribute(QWebEngineSettings::JavascriptCanOpenWindows, false);
        settings->setAttribute(QWebEngineSettings::JavascriptCanAccessClipboard, false);
        settings->setAttribute(QWebEngineSettings::JavascriptCanPaste, false);
        settings->setAttribute(QWebEngineSettings::ScreenCaptureEnabled, false);
        settings->setAttribute(QWebEngineSettings::FullScreenSupportEnabled, false);
        settings->setAttribute(QWebEngineSettings::PluginsEnabled, false);
        settings->setAttribute(QWebEngineSettings::NavigateOnDropEnabled, false);
        settings->setUnknownUrlSchemePolicy(QWebEngineSettings::DisallowUnknownUrlSchemes);

        applyTheme();
        connectUi();
        updateNavigation();
        startServer();
        writeRegistration();
    }

    ~BrowserWindow() override
    {
        clearRegistration();
        m_server.close();
        if (!m_socketPath.isEmpty())
            QLocalServer::removeServer(m_socketPath);
    }

    void openUrl(const QUrl &url)
    {
        if (!allowedUrl(url))
            return;
        m_opened = true;
        show();
        raise();
        activateWindow();
        m_view->setUrl(url);
    }

private:
    QString m_socketPath;
    QString m_sessionId;
    QString m_registrationPath;
    Palette m_palette;
    QLocalServer m_server;
    QWebEngineProfile *m_profile = nullptr;
    RestrictedPage *m_page = nullptr;
    QWebEngineView *m_view = nullptr;
    QLineEdit *m_address = nullptr;
    QPushButton *m_back = nullptr;
    QPushButton *m_refresh = nullptr;
    QPushButton *m_stop = nullptr;
    QProgressBar *m_progress = nullptr;
    bool m_opened = false;
    bool m_loading = false;
    int m_generation = 0;
    QPointer<QLocalSocket> m_pending;
    QTimer m_operationTimer;
    quint64 m_operation = 0;
    quint64 m_snapshotOperation = 0;
    quint64 m_navigationOperation = 0;
    bool m_snapshotInFlight = false;
    bool m_actionInFlight = false;
    bool m_navigationStarted = false;

    void applyTheme()
    {
        const auto panel = cssColor(m_palette.panel);
        const auto input = cssColor(m_palette.input);
        const auto horizon = cssColor(m_palette.horizon);
        const auto ink = cssColor(m_palette.ink);
        const auto muted = cssColor(m_palette.muted);
        const auto accent = cssColor(m_palette.accent);
        const auto line = cssColor(m_palette.line);
        QColor contour = m_palette.wave;
        contour.setAlphaF(0.5);
        const auto border = contour.name(QColor::HexArgb);

        setStyleSheet(QString(R"(
            QWidget#browserFrame {
                background: %1;
                border: 1px solid %8;
            }
            QWidget#browserBar {
                background: %1;
                border: 0;
                border-bottom: 1px solid %7;
            }
            QPushButton#browserControl {
                background: transparent;
                color: %5;
                border: 1px solid transparent;
                border-radius: 6px;
                font-size: 20px;
            }
            QPushButton#browserControl:hover {
                background: %2;
                color: %4;
            }
            QPushButton#browserControl:focus {
                background: %2;
                color: %4;
                border-color: %6;
            }
            QPushButton#browserControl:pressed {
                background: %3;
                color: %4;
            }
            QPushButton#browserControl:disabled {
                color: %7;
                background: transparent;
            }
            QLineEdit#browserAddress {
                min-height: 36px;
                padding: 0 12px;
                color: %4;
                selection-color: %1;
                selection-background-color: %6;
                background: %2;
                border: 1px solid %7;
                border-radius: 6px;
                font-size: 14px;
            }
            QLineEdit#browserAddress:hover {
                border-color: %5;
            }
            QLineEdit#browserAddress:focus {
                border-color: %6;
            }
            QLineEdit#browserAddress:disabled {
                color: %5;
                background: %1;
            }
            QProgressBar#browserProgress {
                background: %1;
                border: 0;
            }
            QProgressBar#browserProgress::chunk {
                background: %6;
            }
        )").arg(panel, input, horizon, ink, muted, accent, line, border));

        QPalette palette;
        palette.setColor(QPalette::Window, m_palette.panel);
        palette.setColor(QPalette::Base, m_palette.input);
        palette.setColor(QPalette::Text, m_palette.ink);
        palette.setColor(QPalette::WindowText, m_palette.ink);
        palette.setColor(QPalette::Button, m_palette.input);
        palette.setColor(QPalette::ButtonText, m_palette.ink);
        palette.setColor(QPalette::Highlight, m_palette.accent);
        palette.setColor(QPalette::HighlightedText, m_palette.night);
        qApp->setPalette(palette);
    }

    void connectUi()
    {
        m_operationTimer.setSingleShot(true);
        m_operationTimer.setInterval(kOperationTimeoutMs);
        connect(&m_operationTimer, &QTimer::timeout, this, [this] {
            failPending("Browser operation timed out. Try a fresh snapshot.");
        });

        connect(m_back, &QPushButton::clicked, m_view, &QWebEngineView::back);
        connect(m_refresh, &QPushButton::clicked, m_view, &QWebEngineView::reload);
        connect(m_stop, &QPushButton::clicked, m_view, &QWebEngineView::stop);
        connect(m_address, &QLineEdit::returnPressed, this, [this] {
            QString text = m_address->text().trimmed();
            if (!text.contains("://"))
                text.prepend("https://");
            const QUrl url(text);
            if (!allowedUrl(url)) {
                m_address->setToolTip("Enter a valid HTTP(S) address.");
                m_address->selectAll();
                return;
            }
            m_opened = true;
            m_view->setUrl(url);
        });

        connect(m_view, &QWebEngineView::urlChanged, this, [this](const QUrl &url) {
            if (!m_address->hasFocus())
                m_address->setText(url == QUrl("about:blank") ? QString() : url.toString());
        });
        connect(m_view, &QWebEngineView::titleChanged, this, [this](const QString &title) {
            setWindowTitle(title.isEmpty() ? "AIOS Browser" : title + " — AIOS Browser");
        });
        connect(m_view, &QWebEngineView::loadStarted, this, [this] {
            m_loading = true;
            m_stop->setEnabled(true);
            m_progress->setValue(0);
            m_progress->show();
            updateNavigation();
        });
        connect(m_view, &QWebEngineView::loadProgress, m_progress, &QProgressBar::setValue);
        connect(m_view, &QWebEngineView::loadFinished, this, [this](bool) {
            m_loading = false;
            m_stop->setEnabled(false);
            m_progress->hide();
            updateNavigation();
        });
        connect(m_page, &QWebEnginePage::loadingChanged, this,
                [this](const QWebEngineLoadingInfo &info) {
            if (!m_pending || m_actionInFlight || m_navigationOperation != m_operation)
                return;
            if (info.status() == QWebEngineLoadingInfo::LoadStartedStatus) {
                if (allowedUrl(info.url()))
                    m_navigationStarted = true;
                return;
            }
            if (m_navigationStarted) {
                m_navigationStarted = false;
                snapshotPending();
            }
        });
        connect(m_page, &RestrictedPage::blocked, this, [this](const QString &message) {
            if (m_pending)
                failPending(message);
            m_address->setToolTip(message);
        });
        connect(m_profile, &QWebEngineProfile::downloadRequested, this,
                [](QWebEngineDownloadRequest *download) { download->cancel(); });
        connect(m_page, &QWebEnginePage::permissionRequested, this,
                [](QWebEnginePermission permission) { permission.deny(); });
        connect(m_page, &QWebEnginePage::certificateError, this,
                [](const QWebEngineCertificateError &error) {
                    auto rejected = error;
                    rejected.rejectCertificate();
                });
        connect(m_page, &QWebEnginePage::fullScreenRequested, this,
                [](QWebEngineFullScreenRequest request) { request.reject(); });
        connect(m_page, &QWebEnginePage::renderProcessTerminated, this,
                [this](QWebEnginePage::RenderProcessTerminationStatus, int) {
                    failPending("The browser renderer stopped. Reopen the page.");
                });
    }

    void updateNavigation()
    {
        m_back->setEnabled(m_view->history()->canGoBack());
        m_refresh->setEnabled(m_opened);
    }

    void startServer()
    {
        if (m_socketPath.isEmpty())
            return;
        QFileInfo socketInfo(m_socketPath);
        QDir().mkpath(socketInfo.absolutePath());
        QFile::setPermissions(socketInfo.absolutePath(),
            QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner);
        QLocalServer::removeServer(m_socketPath);
        m_server.setSocketOptions(QLocalServer::UserAccessOption);
        if (!m_server.listen(m_socketPath))
            qFatal("Could not create browser control socket: %s", qPrintable(m_server.errorString()));
        QFile::setPermissions(m_socketPath, QFileDevice::ReadOwner | QFileDevice::WriteOwner);
        connect(&m_server, &QLocalServer::newConnection, this, [this] {
            while (m_server.hasPendingConnections()) {
                auto socket = m_server.nextPendingConnection();
                socket->setReadBufferSize(kMaxRequestBytes + 1);
                connect(socket, &QLocalSocket::readyRead, this, [this, socket] {
                    if (socket->property("handled").toBool())
                        return;
                    if (socket->bytesAvailable() > kMaxRequestBytes) {
                        socket->setProperty("handled", true);
                        respond(socket, QJsonObject{{"error", "Browser request is too large."}});
                        return;
                    }
                    if (!socket->canReadLine())
                        return;
                    socket->setProperty("handled", true);
                    const auto raw = socket->readLine(kMaxRequestBytes + 1);
                    handleRequest(socket, QJsonDocument::fromJson(raw.trimmed()).object());
                });
                connect(socket, &QLocalSocket::disconnected, socket, &QObject::deleteLater);
            }
        });
    }

    QString runtimeDirectory() const
    {
        QString runtime = QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation);
        if (runtime.isEmpty())
            runtime = QDir::tempPath() + "/aios-" + QString::number(QCoreApplication::applicationPid());
        return runtime + "/aios/browsers";
    }

    void writeRegistration()
    {
        if (m_socketPath.isEmpty())
            return;
        const QString directory = runtimeDirectory();
        QDir().mkpath(directory);
        QFile::setPermissions(directory,
            QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner);
        m_registrationPath = directory + "/" + m_sessionId + ".json";
        QSaveFile file(m_registrationPath);
        if (!file.open(QIODevice::WriteOnly))
            return;
        file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
        const QJsonObject registration{
            {"version", 1},
            {"session", m_sessionId},
            {"socket", m_socketPath},
            {"pid", QCoreApplication::applicationPid()},
            {"actions", QJsonArray::fromStringList(kActions)}
        };
        file.write(QJsonDocument(registration).toJson(QJsonDocument::Compact));
        file.commit();
    }

    void clearRegistration()
    {
        if (!m_registrationPath.isEmpty())
            QFile::remove(m_registrationPath);
    }

    void handleRequest(QLocalSocket *socket, const QJsonObject &request)
    {
        const QString action = request.value("action").toString();
        if (!kActions.contains(action)) {
            respond(socket, QJsonObject{{"error", "Unknown browser action."}});
            return;
        }
        if (action == "close") {
            if (request.size() != 1) {
                respond(socket, QJsonObject{{"error", "Browser request contains unsupported fields."}});
                return;
            }
            if (m_pending) {
                auto pending = m_pending;
                m_pending.clear();
                respond(pending, QJsonObject{{"error", "Browser closed."}});
            }
            ++m_operation;
            m_snapshotInFlight = false;
            m_actionInFlight = false;
            m_operationTimer.stop();
            clearRegistration();
            m_server.close();
            respond(socket, QJsonObject{{"closed", true}});
            QTimer::singleShot(0, qApp, &QCoreApplication::quit);
            return;
        }
        if (m_pending) {
            respond(socket, QJsonObject{{"error", "Another browser operation is still running."}});
            return;
        }
        QSet<QString> allowedFields{"action"};
        if (action == "open" || action == "navigate")
            allowedFields.insert("url");
        else if (action == "click" || action == "type" || action == "press")
            allowedFields.insert("element");
        else if (action == "scroll")
            allowedFields.insert("direction");
        else if (action == "switch")
            allowedFields.insert("tab");
        if (action == "type" || action == "press")
            allowedFields.insert("text");
        for (auto it = request.begin(); it != request.end(); ++it) {
            if (!allowedFields.contains(it.key())) {
                respond(socket, QJsonObject{{"error", "Browser request contains unsupported fields."}});
                return;
            }
        }
        if (action == "open" || action == "navigate") {
            if (!request.value("url").isString()) {
                respond(socket, QJsonObject{{"error", "Enter a valid web URL."}});
                return;
            }
            const QUrl url(request.value("url").toString());
            if (!allowedUrl(url)) {
                respond(socket, QJsonObject{{"error", "Browser navigation requires an HTTP(S) URL without embedded credentials."}});
                return;
            }
            if (action == "navigate" && !m_opened) {
                respond(socket, QJsonObject{{"error", "Open the browser first."}});
                return;
            }
            m_opened = true;
            show();
            raise();
            activateWindow();
            beginPending(socket);
            m_navigationOperation = m_operation;
            const quint64 operation = m_operation;
            m_view->setUrl(url);
            QTimer::singleShot(250, this, [this, operation] {
                if (m_pending && operation == m_operation && !m_loading)
                    snapshotPending();
            });
            return;
        }
        if (!m_opened) {
            respond(socket, QJsonObject{{"error", "Open the browser first."}});
            return;
        }
        if (action == "snapshot") {
            beginPending(socket);
            snapshotPending();
        } else if (action == "back" || action == "forward" || action == "reload") {
            beginPending(socket);
            m_navigationOperation = m_operation;
            const quint64 operation = m_operation;
            if (action == "back")
                m_view->back();
            else if (action == "forward")
                m_view->forward();
            else
                m_view->reload();
            QTimer::singleShot(250, this, [this, operation] {
                if (m_pending && operation == m_operation && !m_loading)
                    snapshotPending();
            });
        } else if (action == "stop") {
            m_view->stop();
            beginPending(socket);
            snapshotPending();
        } else if (action == "tabs") {
            respond(socket, QJsonObject{{"tabs", QJsonArray{"main"}}, {"current", "main"}});
        } else if (action == "switch") {
            if (!request.value("tab").isString() || request.value("tab").toString() != "main") {
                respond(socket, QJsonObject{{"error", "Choose a tab from the tabs result."}});
                return;
            }
            beginPending(socket);
            snapshotPending();
        } else {
            beginPending(socket);
            runElementAction(action, request);
        }
    }

    void beginPending(QLocalSocket *socket)
    {
        ++m_operation;
        m_pending = socket;
        m_snapshotInFlight = false;
        m_actionInFlight = false;
        m_navigationOperation = 0;
        m_navigationStarted = false;
        m_operationTimer.start();
    }

    void failPending(const QString &message)
    {
        if (!m_pending)
            return;
        auto socket = m_pending;
        m_pending.clear();
        ++m_operation;
        m_snapshotInFlight = false;
        m_actionInFlight = false;
        m_navigationOperation = 0;
        m_navigationStarted = false;
        m_operationTimer.stop();
        respond(socket, QJsonObject{{"error", message}});
    }

    void respond(QLocalSocket *socket, const QJsonValue &value)
    {
        if (!socket || socket->state() == QLocalSocket::UnconnectedState)
            return;
        QByteArray payload;
        if (value.isObject())
            payload = QJsonDocument(value.toObject()).toJson(QJsonDocument::Compact);
        else
            payload = QJsonDocument(value.toArray()).toJson(QJsonDocument::Compact);
        if (payload.size() > kMaxResponseBytes)
            payload = QJsonDocument(QJsonObject{{"error", "Browser response was too large."}}).toJson(QJsonDocument::Compact);
        socket->write(payload + '\n');
        socket->flush();
        socket->disconnectFromServer();
    }

    void snapshotPending()
    {
        if (!m_pending || m_snapshotInFlight)
            return;
        m_snapshotInFlight = true;
        m_snapshotOperation = m_operation;
        const quint64 operation = m_operation;
        const int generation = ++m_generation;
        const QString script = QString(R"JS(
(() => {
  const generation = %1;
  const inView = r => r.width && r.height && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  const visuallyVisible = e => {
    if (![...e.getClientRects()].some(inView)) return false;
    for (let node = e; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const visible = e => visuallyVisible(e) && getComputedStyle(e).pointerEvents !== 'none';
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  let node, text = '';
  while ((node = walker.nextNode()) && text.length < 6000) {
    if (!node.textContent.trim() || ['SCRIPT','STYLE','NOSCRIPT'].includes(node.parentElement?.tagName)) continue;
    if (!visuallyVisible(node.parentElement)) continue;
    const range = document.createRange(); range.selectNodeContents(node);
    if ([...range.getClientRects()].some(inView)) text += node.textContent.trim() + '\n';
  }
  const elements = [...document.querySelectorAll('a,button,input,textarea,select,[role="button"],[contenteditable="true"]')]
    .filter(visible).slice(0, 40);
  globalThis.__aiosBrowser = {generation, elements};
  return {
    url: location.href,
    title: document.title,
    text: text.slice(0, 6000),
    viewport: {x: scrollX, y: scrollY, height: innerHeight},
    controls: elements.map((e, index) => ({
      element: `e${generation}-${index}`,
      tag: e.tagName.toLowerCase(),
      type: e.type || '',
      label: (e.getAttribute('aria-label') || e.labels?.[0]?.innerText || e.innerText || e.placeholder || e.name || '').slice(0, 100),
      disabled: !!e.disabled,
      checked: !!e.checked
    })),
    untrusted_page_content: true
  };
})()
)JS").arg(generation);
        m_page->runJavaScript(script, QWebEngineScript::ApplicationWorld, [this, operation](const QVariant &result) {
            if (!m_pending || operation != m_operation || operation != m_snapshotOperation)
                return;
            m_snapshotInFlight = false;
            auto socket = m_pending;
            m_pending.clear();
            m_operationTimer.stop();
            const auto value = jsonValue(result);
            if (!value.isObject()) {
                respond(socket, QJsonObject{{"error", "Browser snapshot failed. Reopen the page."}});
                return;
            }
            respond(socket, value);
        });
    }

    bool parseElement(const QString &identifier, int *generation, int *index) const
    {
        if (!identifier.startsWith('e'))
            return false;
        const auto parts = identifier.mid(1).split('-');
        bool generationOk = false;
        bool indexOk = false;
        const int parsedGeneration = parts.value(0).toInt(&generationOk);
        const int parsedIndex = parts.value(1).toInt(&indexOk);
        if (parts.size() != 2 || !generationOk || !indexOk || parsedIndex < 0 || parsedIndex >= 40)
            return false;
        *generation = parsedGeneration;
        *index = parsedIndex;
        return true;
    }

    void runElementAction(const QString &action, const QJsonObject &request)
    {
        const quint64 operation = m_operation;
        QString script;
        if (action == "scroll") {
            if (request.contains("direction") && !request.value("direction").isString()) {
                failPending("Choose up or down.");
                return;
            }
            const QString direction = request.value("direction").toString("down");
            if (direction != "up" && direction != "down") {
                failPending("Choose up or down.");
                return;
            }
            script = QString("window.scrollBy(0, %1 * innerHeight * 0.75); ({ok:true})")
                .arg(direction == "up" ? -1 : 1);
        } else {
            int generation = 0;
            int index = 0;
            if (!request.value("element").isString()
                    || !parseElement(request.value("element").toString(), &generation, &index)
                    || generation != m_generation) {
                failPending("Use an element ID from the latest snapshot.");
                return;
            }
            const QString prefix = QString(R"JS(
(() => {
  const state = globalThis.__aiosBrowser;
  if (!state || state.generation !== %1) return {error:'Use an element ID from the latest snapshot.'};
  const element = state.elements[%2];
  if (!element || !element.isConnected) return {error:'Use an element ID from the latest snapshot.'};
  const rect = element.getBoundingClientRect();
  if (!rect.width || !rect.height) return {error:'The selected control is not visible.'};
  for (let node = element; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0)
      return {error:'The selected control is not visible.'};
  }
  if (getComputedStyle(element).pointerEvents === 'none') return {error:'The selected control is not actionable.'};
  if (element.disabled) return {error:'The selected control is disabled.'};
  if ((element.type || '').toLowerCase() === 'file') return {error:'File upload controls are not available to the browser tool.'};
  element.scrollIntoView({block:'center', inline:'nearest'});
  const updated = element.getBoundingClientRect();
  const x = Math.max(0, Math.min(innerWidth - 1, updated.left + updated.width / 2));
  const y = Math.max(0, Math.min(innerHeight - 1, updated.top + updated.height / 2));
  const topmost = document.elementsFromPoint(x, y).find(node => getComputedStyle(node).pointerEvents !== 'none');
  if (!topmost || (topmost !== element && !element.contains(topmost)))
    return {error:'The selected control is covered by another element.'};
)JS").arg(generation).arg(index);
            if (action == "click") {
                script = prefix + "  element.click(); return {ok:true};\n})()";
            } else if (action == "type") {
                if (!request.value("text").isString()) {
                    failPending("Enter at most 8000 characters.");
                    return;
                }
                const QString text = request.value("text").toString();
                if (text.size() > 8000) {
                    failPending("Enter at most 8000 characters.");
                    return;
                }
                script = prefix + QString(R"JS(
  const text = %1;
  element.focus();
  if (element.isContentEditable) {
    element.textContent = text;
  } else {
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value');
    if (descriptor?.set) descriptor.set.call(element, text); else element.value = text;
  }
  element.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
  element.dispatchEvent(new Event('change', {bubbles:true}));
  return {ok:true};
})()
)JS").arg(jsString(text));
            } else if (action == "press") {
                if (!request.value("text").isString()) {
                    failPending("Supported keys: Enter, Tab, Escape.");
                    return;
                }
                const QString key = request.value("text").toString();
                if (key != "Enter" && key != "Tab" && key != "Escape") {
                    failPending("Supported keys: Enter, Tab, Escape.");
                    return;
                }
                script = prefix + QString(R"JS(
  const key = %1;
  element.focus();
  if (key === 'Enter') {
    const tag = element.tagName.toLowerCase();
    const type = (element.type || '').toLowerCase();
    if (tag === 'textarea') {
      const start = element.selectionStart ?? element.value.length;
      const end = element.selectionEnd ?? start;
      element.setRangeText('\n', start, end, 'end');
      element.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertLineBreak', data:null}));
    } else if (element.isContentEditable) {
      document.execCommand('insertLineBreak');
      element.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertLineBreak', data:null}));
    } else if (tag === 'a' || tag === 'button' || ['button','submit','reset'].includes(type)) {
      element.click();
    } else if (tag === 'input' && element.form) {
      element.form.requestSubmit();
    } else {
      return {error:'Enter is not available for the selected control.'};
    }
  } else if (key === 'Tab') {
    const candidates = [...document.querySelectorAll('a,button,input,textarea,select,[tabindex]')]
      .filter(e => !e.disabled && e.tabIndex >= 0 && e.getClientRects().length);
    if (!candidates.length) return {error:'No next control is available.'};
    const position = candidates.indexOf(element);
    candidates[(position + 1) % candidates.length]?.focus();
  } else {
    element.blur();
  }
  return {ok:true};
})()
)JS").arg(jsString(key));
            }
        }
        m_actionInFlight = true;
        m_page->runJavaScript(script, QWebEngineScript::ApplicationWorld, [this, operation](const QVariant &result) {
            if (!m_pending || operation != m_operation)
                return;
            m_actionInFlight = false;
            const auto object = jsonValue(result).toObject();
            if (object.contains("error")) {
                failPending(object.value("error").toString());
                return;
            }
            m_navigationOperation = operation;
            m_navigationStarted = m_loading;
            QTimer::singleShot(250, this, [this, operation] {
                if (m_pending && operation == m_operation && !m_loading)
                    snapshotPending();
            });
        });
    }
};

} // namespace

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    app.setApplicationName("AIOS Browser");
    app.setOrganizationName("AIOS");
    app.setFont(QFont("DejaVu Sans", 10));

    QCommandLineParser parser;
    parser.setApplicationDescription("AIOS private themed browser");
    parser.addHelpOption();
    parser.addOption({{"s", "socket"}, "Owner-only control socket path.", "path"});
    parser.addOption({{"i", "browser-session"}, "Chat session identifier.", "id"});
    parser.addOption({{"t", "theme"}, "AIOS theme key.", "theme", "blue"});
    parser.addOption({{"u", "url"}, "Open a standalone trusted HTTP(S) URL.", "url"});
    parser.process(app);

    const QString socketPath = parser.value("socket");
    QString sessionId = parser.value("browser-session");
    const QUrl initialUrl(parser.value("url"));
    if ((socketPath.isEmpty() || sessionId.isEmpty()) && !allowedUrl(initialUrl))
        parser.showHelp(2);
    if (sessionId.isEmpty())
        sessionId = "standalone-" + QString::number(QCoreApplication::applicationPid());

    BrowserWindow window(socketPath, sessionId, parser.value("theme"));
    if (allowedUrl(initialUrl))
        window.openUrl(initialUrl);
    return app.exec();
}

#include "main.moc"
