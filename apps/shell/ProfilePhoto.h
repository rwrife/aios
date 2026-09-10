#pragma once
#include <QObject>
#include <QCameraDevice>
#include <QImage>
#include <QMediaDevices>
#include <QBuffer>
#include <QProcess>
#include <QRegularExpression>
#include <QStringList>
#include <QTimer>
#include <QPointer>
#include <QElapsedTimer>
#include <memory>
#include "CameraDevice.h"

// A single explicit enrollment snapshot. Nothing is saved to a file.
class ProfilePhoto : public QObject {
    Q_OBJECT
public:
    explicit ProfilePhoto(QObject *parent = nullptr) : QObject(parent) {
        retryTimer.setSingleShot(true);
        connect(&retryTimer, &QTimer::timeout, this, &ProfilePhoto::startAttempt);
    }
    void cancel() {
        if (!captureActive) return;
        captureActive = false;
        ++generation;
        retryTimer.stop();
        auto process = qobject_cast<QProcess *>(job.data());
        job = nullptr;
        if (process) process->kill();
        if (process) process->deleteLater();
    }
    void take(const QString &devicePath = {}) {
        if (captureActive) {
            emit failed("A camera capture is already in progress.");
            return;
        }
        selectedPath = devicePath;
        captureActive = true;
        const auto current = ++generation;
        captureClock.start();
        QTimer::singleShot(5000, this, [this, current] {
            if (!captureActive || generation != current) return;
            captureActive = false;
            retryTimer.stop();
            auto process = qobject_cast<QProcess *>(job.data());
            job = nullptr;
            if (process) process->kill();
            if (process) process->deleteLater();
            emit failed("Camera capture timed out.");
        });
        startAttempt();
    }
signals:
    void captured(const QString &preview, const QString &rgb);
    void failed(const QString &message);
private:
    void retryOrFail(const QString &message) {
        if (!captureActive) return;
        if (captureClock.elapsed() < 4500) {
            retryTimer.start(250);
            return;
        }
        captureActive = false;
        emit failed(message);
    }
    void startAttempt() {
        if (!captureActive || job) return;
        const auto path = selectedPath.isEmpty() ? CameraDevice::capturePath() : selectedPath;
        if (path.isEmpty()) {
            retryOrFail("No usable local camera was found.");
            return;
        }
        auto process = new QProcess(this);
        job = process;
        const auto current = generation;
        process->setProgram("ffmpeg");
        process->setArguments(QStringList{
            "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "video4linux2", "-input_format", "mjpeg",
            "-video_size", "640x360", "-framerate", "15",
            "-i", path, "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"
        });
        process->setStandardErrorFile(QProcess::nullDevice());
        constexpr qsizetype expected = 640 * 360 * 3;
        auto output = std::make_shared<QByteArray>();
        auto finished = std::make_shared<bool>(false);
        connect(process, &QProcess::readyReadStandardOutput, process, [process, output] {
            output->append(process->readAllStandardOutput());
            if (output->size() > expected) process->kill();
        });
        connect(process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), process,
                [this, process, output, finished, current](int code, QProcess::ExitStatus status) {
            if (*finished) return;
            output->append(process->readAllStandardOutput());
            *finished = true;
            if (job == process) job = nullptr;
            process->deleteLater();
            if (!captureActive || generation != current) return;
            if (status != QProcess::NormalExit || code != 0 || output->size() != expected) {
                retryOrFail("The camera could not capture a profile photo.");
                return;
            }
            auto source = QImage(reinterpret_cast<const uchar *>(output->constData()),
                                 640, 360, 640 * 3, QImage::Format_RGB888).copy();
            auto scaled = source.scaled(64, 64, Qt::KeepAspectRatioByExpanding, Qt::SmoothTransformation);
            auto photo = scaled.copy((scaled.width()-64)/2, (scaled.height()-64)/2, 64, 64).convertToFormat(QImage::Format_RGB888);
            if (photo.isNull()) {
                captureActive = false;
                emit failed("The camera returned an invalid profile photo.");
                return;
            }
            QByteArray rgb;
            for (int row = 0; row < 64; ++row) rgb.append(reinterpret_cast<const char *>(photo.constScanLine(row)), 192);
            QByteArray png; QBuffer buffer(&png); buffer.open(QIODevice::WriteOnly); photo.save(&buffer, "PNG");
            captureActive = false;
            emit captured("data:image/png;base64," + QString::fromLatin1(png.toBase64()), QString::fromLatin1(rgb.toBase64()));
        });
        connect(process, &QProcess::errorOccurred, process, [this, process, finished, current](QProcess::ProcessError) {
            if (*finished) return;
            *finished = true;
            if (job == process) job = nullptr;
            process->deleteLater();
            if (captureActive && generation == current)
                retryOrFail("The camera capture process could not start.");
        });
        process->start();
    }
    QPointer<QObject> job;
    QTimer retryTimer;
    QElapsedTimer captureClock;
    QString selectedPath;
    quint64 generation = 0;
    bool captureActive = false;
};
