#pragma once
#include <QCameraDevice>
#include <QDir>
#include <QFileInfo>
#include <QMediaDevices>
#include <QRegularExpression>

namespace CameraDevice {

inline QString localPath(const QCameraDevice &device) {
    const auto match = QRegularExpression(R"((/dev/video[0-9]+))")
        .match(QString::fromUtf8(device.id()));
    if (!match.hasMatch()) return {};
    const auto path = match.captured(1);
    return QFileInfo::exists(path) ? path : QString();
}

inline QString stablePath(const QCameraDevice &preferred = QMediaDevices::defaultVideoInput()) {
    const auto preferredPath = localPath(preferred);
    const QRegularExpression stableName(
        R"(^[A-Za-z0-9._:+-]+-video-index[0-9]+$)");
    const QRegularExpression localName(R"(^/dev/video[0-9]+$)");
    QDir directory("/dev/v4l/by-id");
    QString fallback;
    for (const auto &name : directory.entryList(
             {"*-video-index0"}, QDir::AllEntries | QDir::NoDotAndDotDot, QDir::Name)) {
        if (!stableName.match(name).hasMatch()) continue;
        const auto stable = directory.absoluteFilePath(name);
        const auto target = QFileInfo(stable).canonicalFilePath();
        if (!localName.match(target).hasMatch()) continue;
        if (fallback.isEmpty()) fallback = stable;
        if (!preferredPath.isEmpty() && target == preferredPath) return stable;
    }
    return fallback;
}

inline QString capturePath(const QCameraDevice &preferred = QMediaDevices::defaultVideoInput()) {
    const auto stable = stablePath(preferred);
    return stable.isEmpty() ? localPath(preferred) : stable;
}

}
