#pragma once
#include <QObject>
#include <QClipboard>
#include <QGuiApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocalSocket>
#include <QPointer>
#include <QSet>
#include <QTimer>
#include <QVariantList>
#include <functional>
#ifdef Q_OS_LINUX
#include <sys/socket.h>
#include <unistd.h>
#endif

class ProtectedChat : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList messages READ messages NOTIFY changed)
    Q_PROPERTY(QVariantMap config READ config NOTIFY changed)
    Q_PROPERTY(QString status READ status NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(QStringList attachments READ attachments NOTIFY changed)
    Q_PROPERTY(bool recording READ recording NOTIFY changed)
    Q_PROPERTY(bool speaking READ speaking NOTIFY changed)
    Q_PROPERTY(bool protectedMode READ protectedMode CONSTANT)
public:
    ProtectedChat(QString path, QJsonObject binding,
                  std::function<QJsonObject()> current, QObject *parent = nullptr)
        : QObject(parent), socketPath(std::move(path)), context(std::move(binding)),
          currentContext(std::move(current)) {
        pollTimer.setInterval(100);
        connect(&pollTimer, &QTimer::timeout, this, &ProtectedChat::poll);
        call("chat_open", {});
    }
    QVariantList messages() const { return m_messages; }
    QVariantMap config() const { return m_config; }
    QString status() const { return m_status; }
    bool busy() const { return m_busy; }
    QStringList attachments() const { return {}; }
    bool recording() const { return false; }
    bool speaking() const { return false; }
    bool protectedMode() const { return true; }
    void invalidate() {
        if (invalid) return;
        invalid = true;
        ++generation;
        pollTimer.stop();
        const auto pending = sockets;
        sockets.clear();
        for (auto socket : pending) {
            socket->disconnect(this);
            socket->abort();
            socket->deleteLater();
        }
        m_messages.clear();
        m_config.clear();
        m_status.clear();
        m_busy = false;
        emit changed();
        emit invalidated();
    }
    Q_INVOKABLE void send(const QString &text) {
        const auto content = text.trimmed();
        if (invalid || m_busy || content.isEmpty()) return;
        m_messages.append(QVariantMap{{"role", "user"}, {"content", content}});
        m_messages.append(QVariantMap{{"role", "assistant"}, {"content", ""}});
        m_busy = true;
        m_status = "Connecting…";
        emit changed();
        call("chat_send", {{"content", content}});
    }
    Q_INVOKABLE void stop() {
        if (!invalid && m_busy) call("chat_stop", {});
    }
    Q_INVOKABLE void newChat() {
        if (!invalid && !m_busy) {
            m_messages.clear();
            m_status.clear();
            emit changed();
        }
    }
    Q_INVOKABLE void closeSession() {
        if (closing) return;
        closing = true;
        pollTimer.stop();
        if (invalid || chatId.isEmpty()) {
            deleteLater();
            return;
        }
        call("chat_close", {});
    }
    Q_INVOKABLE void copy(const QString &text) { QGuiApplication::clipboard()->setText(text); }
    Q_INVOKABLE void attach(const QUrl &) {
        m_status = "Protected chat attachments are unavailable in this build.";
        emit changed();
    }
    Q_INVOKABLE void removeAttachment(int) {}
    Q_INVOKABLE void setVoiceActive(bool) {
        m_status = "Voice is unavailable in protected chat.";
        emit changed();
    }
    Q_INVOKABLE void cancelRecording() {}
    Q_INVOKABLE void readReply(const QString &) {
        m_status = "Spoken replies are unavailable in protected chat.";
        emit changed();
    }
    Q_INVOKABLE void setAuthenticationState(const QString &) {}
signals:
    void changed();
    void invalidated();
    void failed(const QString &message);
    void authenticationRequested();
    void transcribed(const QString &text);
private:
    QString socketPath;
    QJsonObject context;
    std::function<QJsonObject()> currentContext;
    QString chatId;
    QString chatKey;
    QVariantList m_messages;
    QVariantMap m_config;
    QString m_status;
    bool m_busy = false;
    bool invalid = false;
    bool closing = false;
    quint64 generation = 0;
    QTimer pollTimer;
    QSet<QLocalSocket *> sockets;

    void poll() {
        if (!invalid && !chatId.isEmpty()) call("chat_poll", {});
    }
    void call(const QString &action, QJsonObject fields) {
        if (invalid || currentContext() != context) {
            invalidate();
            return;
        }
        QJsonObject request{{"action", action}, {"lease", context.value("lease")},
                            {"scope", context.value("scope")}};
        if (action != "chat_open") {
            request["chat"] = chatId;
            request["key"] = chatKey;
        }
        for (auto it = fields.begin(); it != fields.end(); ++it) request[it.key()] = it.value();
        auto raw = QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n';
        if (raw.size() > 65536 || sockets.size() >= 4) {
            fail("Protected chat request is unavailable.");
            return;
        }
        auto socket = new QLocalSocket(this);
        auto buffer = new QByteArray;
        const auto epoch = generation;
        sockets.insert(socket);
        connect(socket, &QObject::destroyed, [buffer] { delete buffer; });
        const auto finish = [this, socket] {
            sockets.remove(socket);
            socket->disconnect(this);
            socket->abort();
            socket->deleteLater();
        };
        connect(socket, &QLocalSocket::connected, this, [this, socket, raw] {
#ifdef Q_OS_LINUX
            struct ucred peer{};
            socklen_t length = sizeof(peer);
            if (::getsockopt(socket->socketDescriptor(), SOL_SOCKET, SO_PEERCRED, &peer, &length)
                    || peer.uid != 0 || currentContext() != context) {
                fail("Protected chat service identity is unavailable.");
                return;
            }
            socket->write(raw);
#else
            Q_UNUSED(raw)
            fail("Protected chat requires the native Linux session.");
#endif
        });
        connect(socket, &QLocalSocket::readyRead, this,
                [this, socket, buffer, action, epoch, finish] {
            buffer->append(socket->readAll());
            if (buffer->size() > 2 * 1024 * 1024) {
                fail("Protected chat response is too large.");
                return;
            }
            const auto newline = buffer->indexOf('\n');
            if (newline < 0) return;
            QJsonParseError error;
            const auto document = QJsonDocument::fromJson(buffer->left(newline), &error);
            const auto envelope = document.object();
            if (epoch != generation || currentContext() != context) {
                finish();
                return;
            }
            if (error.error != QJsonParseError::NoError || !document.isObject()
                    || !envelope.value("ok").toBool() || !envelope.value("result").isObject()) {
                fail("Protected chat authorization is unavailable.");
                return;
            }
            apply(action, envelope.value("result").toObject());
            finish();
        });
        connect(socket, &QLocalSocket::errorOccurred, this,
                [this](QLocalSocket::LocalSocketError) {
            fail("Protected chat service is unavailable.");
        });
        QTimer::singleShot(action == "chat_poll" ? 1500 : 5000, socket, [this, socket] {
            if (sockets.contains(socket)) fail("Protected chat request timed out.");
        });
        socket->connectToServer(socketPath);
    }
    void apply(const QString &action, const QJsonObject &result) {
        if (action == "chat_open") {
            chatId = result.value("chat").toString();
            chatKey = result.value("key").toString();
            const auto history = result.value("history").toObject();
            m_messages = history.value("messages").toArray().toVariantList();
            m_config = result.value("config").toObject().toVariantMap();
            if (chatId.isEmpty() || chatKey.isEmpty()) {
                fail("Protected chat could not open.");
                return;
            }
            pollTimer.start();
        } else if (action == "chat_poll") {
            const auto events = result.value("events").toArray();
            for (const auto &entry : events) {
                const auto event = entry.toObject();
                const auto type = event.value("type").toString();
                if (type == "token" && !m_messages.isEmpty()) {
                    auto last = m_messages.last().toMap();
                    last["content"] = last.value("content").toString() + event.value("text").toString();
                    m_messages.last() = last;
                    m_status = "Replying…";
                } else if (type == "progress") {
                    m_status = event.value("text").toString();
                } else if (type == "done") {
                    m_busy = false;
                    m_status.clear();
                } else if (type == "error") {
                    m_busy = false;
                    m_status = event.value("text").toString();
                }
            }
            m_busy = result.value("busy").toBool();
        } else if (action == "chat_stop") {
            m_status = "Stopping…";
        } else if (action == "chat_close") {
            invalidate();
            deleteLater();
            return;
        }
        emit changed();
    }
    void fail(const QString &message) {
        if (invalid) return;
        m_status = message;
        emit changed();
        emit failed(message);
        invalidate();
        if (closing) deleteLater();
    }
};
