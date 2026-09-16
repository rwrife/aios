#pragma once
#include <QObject>
#include "CameraClient.h"

// Portraits are separate from templates; only the desktop service opens devices.
class ProfilePhoto : public QObject {
    Q_OBJECT
public:
    explicit ProfilePhoto(QObject *parent = nullptr) : QObject(parent) {}
    ~ProfilePhoto() override { cancel(); }
    void cancel() {
        if (request.isEmpty()) return;
        request.clear();
        CameraClient::instance()->release(consumer);
    }
    void take(const QString &devicePath = {}) {
        if (!request.isEmpty()) { emit failed("The camera is busy."); return; }
        auto client = CameraClient::instance();
        if (!connected) {
            connected = true;
            connect(client, &CameraClient::received, this, [this](const QJsonObject &event) {
                if (request.isEmpty() || event.value("request").toString() != request) return;
                const auto kind = event.value("event").toString();
                if (kind == "photo") {
                    request.clear();
                    CameraClient::instance()->release(consumer);
                    const auto payload = event.value("payload").toObject();
                    emit captured(payload.value("image").toString(), payload.value("rgb").toString());
                } else if (kind == "error" || kind == "cancelled") {
                    request.clear(); CameraClient::instance()->release(consumer);
                    emit failed("The camera capture was unavailable or cancelled.");
                }
            });
            connect(client, &CameraClient::unavailable, this, [this] {
                if (request.isEmpty()) return;
                request.clear(); emit failed("The camera service is unavailable.");
            });
        }
        request = client->capture(consumer, "photo", devicePath);
        if (request.isEmpty()) emit failed("The camera service is unavailable.");
    }
signals:
    void captured(const QString &preview, const QString &rgb);
    void failed(const QString &message);
private:
    QString consumer = CameraClient::identifier();
    QString request;
    bool connected = false;
};
