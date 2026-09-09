#pragma once
#include <QObject>
#include <QJsonDocument>
#include <QJsonObject>
#include <QVariantMap>
#include <QVector>
#include <cstring>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

class DisplayBridge : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool enabled READ enabled CONSTANT)
public:
    using QObject::QObject;
    bool enabled() const {
#ifdef AIOS_EMBEDDED_DISPLAY
        return !qEnvironmentVariableIsEmpty("AIOS_SESSION_SOCKET");
#else
        return false;
#endif
    }
    Q_INVOKABLE void closeDescriptor(int descriptor) { if (descriptor >= 0) ::close(descriptor); }
    Q_INVOKABLE QVariantMap acquire() {
        if (!enabled()) return {};
        const QByteArray path = qgetenv("AIOS_SESSION_SOCKET");
        sockaddr_un address{};
        address.sun_family = AF_UNIX;
        if (path.isEmpty() || path.size() >= int(sizeof(address.sun_path))) return {};
        std::memcpy(address.sun_path, path.constData(), size_t(path.size()));
        const int connection = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (connection < 0) return {};
        timeval timeout{2, 0};
        ::setsockopt(connection, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        ::setsockopt(connection, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        ucred credentials{};
        socklen_t credentialSize = sizeof(credentials);
        if (::connect(connection, reinterpret_cast<sockaddr *>(&address), sizeof(address)) ||
            ::getsockopt(connection, SOL_SOCKET, SO_PEERCRED, &credentials, &credentialSize) || credentials.uid != 0) {
            ::close(connection); return {};
        }
        const QByteArray request = "{\"action\":\"display_acquire\"}\n";
        if (::send(connection, request.constData(), size_t(request.size()), MSG_NOSIGNAL) != request.size()) {
            ::close(connection); return {};
        }
        char bytes[4096];
        alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int) * 4)]{};
        iovec vector{bytes, sizeof(bytes)};
        msghdr message{};
        message.msg_iov = &vector; message.msg_iovlen = 1;
        message.msg_control = ancillary; message.msg_controllen = sizeof(ancillary);
        const auto count = ::recvmsg(connection, &message, MSG_CMSG_CLOEXEC);
        QVector<int> descriptors;
        if (count > 0) {
            for (auto header = CMSG_FIRSTHDR(&message); header; header = CMSG_NXTHDR(&message, header)) {
                if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_RIGHTS) {
                    const auto entries = (header->cmsg_len - CMSG_LEN(0)) / sizeof(int);
                    const auto values = reinterpret_cast<int *>(CMSG_DATA(header));
                    for (size_t index = 0; index < entries; ++index) descriptors.append(values[index]);
                }
            }
        }
        QByteArray payload = count > 0 ? QByteArray(bytes, int(count)) : QByteArray();
        while (!payload.endsWith('\n') && !payload.isEmpty() && payload.size() < 4096) {
            const auto more = ::recv(connection, bytes, sizeof(bytes), 0);
            if (more <= 0) break;
            payload.append(bytes, int(more));
        }
        ::close(connection);
        const auto response = QJsonDocument::fromJson(payload).object();
        if (descriptors.size() != 1 || (message.msg_flags & (MSG_CTRUNC | MSG_TRUNC)) ||
            !payload.endsWith('\n') || !response.value("ok").toBool()) {
            for (int descriptor : descriptors) ::close(descriptor);
            return {};
        }
        const int descriptor = descriptors.first();
        struct stat info{};
        int listening = 0; socklen_t size = sizeof(listening);
        if (::fstat(descriptor, &info) || !S_ISSOCK(info.st_mode) ||
            ::getsockopt(descriptor, SOL_SOCKET, SO_ACCEPTCONN, &listening, &size) || !listening) {
            ::close(descriptor); return {};
        }
        const auto lease = response.value("result").toObject().value("lease").toString();
        if (lease.isEmpty()) { ::close(descriptor); return {}; }
        return {{"descriptor", descriptor}, {"lease", lease}};
    }
};
