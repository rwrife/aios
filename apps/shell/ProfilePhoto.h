#pragma once
#include <QObject>
#include <QCamera>
#include <QImageCapture>
#include <QMediaCaptureSession>
#include <QMediaDevices>
#include <QBuffer>
#include <QTimer>
#include <QPointer>
#include <memory>

// A single explicit enrollment snapshot. Nothing is saved to a file.
class ProfilePhoto : public QObject {
    Q_OBJECT
public:
    using QObject::QObject;
    void cancel() { if (job) { delete job; job = nullptr; } }
    void take() {
        cancel();
        if (QMediaDevices::defaultVideoInput().isNull()) { emit failed(); return; }
        job = new QObject(this);
        auto camera = new QCamera(QMediaDevices::defaultVideoInput(), job);
        auto session = new QMediaCaptureSession(job);
        auto capture = new QImageCapture(job);
        session->setCamera(camera); session->setImageCapture(capture);
        auto requested = std::make_shared<bool>(false);
        connect(capture, &QImageCapture::readyForCaptureChanged, job, [capture, requested](bool ready) {
            if (ready && !*requested) { *requested = true; capture->capture(); }
        });
        connect(capture, &QImageCapture::imageCaptured, job, [this, camera](int, const QImage &source) {
            camera->stop();
            auto scaled = source.scaled(64, 64, Qt::KeepAspectRatioByExpanding, Qt::SmoothTransformation);
            auto photo = scaled.copy((scaled.width()-64)/2, (scaled.height()-64)/2, 64, 64).convertToFormat(QImage::Format_RGB888);
            if (photo.isNull()) { emit failed(); cancelLater(); return; }
            QByteArray rgb;
            for (int row = 0; row < 64; ++row) rgb.append(reinterpret_cast<const char *>(photo.constScanLine(row)), 192);
            QByteArray png; QBuffer buffer(&png); buffer.open(QIODevice::WriteOnly); photo.save(&buffer, "PNG");
            emit captured("data:image/png;base64," + QString::fromLatin1(png.toBase64()), QString::fromLatin1(rgb.toBase64()));
            cancelLater();
        });
        connect(capture, &QImageCapture::errorOccurred, job, [this](int, QImageCapture::Error, const QString &) { emit failed(); cancelLater(); });
        connect(camera, &QCamera::errorOccurred, job, [this](QCamera::Error, const QString &) { emit failed(); cancelLater(); });
        QTimer::singleShot(5000, job, [this] { emit failed(); cancelLater(); });
        camera->start();
    }
signals:
    void captured(const QString &preview, const QString &rgb);
    void failed();
private:
    QPointer<QObject> job;
    void cancelLater() { if (job) { job->deleteLater(); job = nullptr; } }
};
