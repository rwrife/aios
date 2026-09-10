#pragma once

#include <QMargins>
#include <QGuiApplication>
#include <QRect>
#include <QSize>
#include <QTimer>
#include <QWindow>
#include <QtMath>

#include <functional>

inline QSize initialWindowSize(const QSize &preferred, const QRect &available,
                               const QMargins &frame = {})
{
    const QSize limit(qFloor(available.width() * 0.7) - frame.left() - frame.right(),
                      qFloor(available.height() * 0.7) - frame.top() - frame.bottom());
    return preferred.boundedTo(limit);
}

inline void whenWindowFrameReady(QWindow *window, const std::function<void()> &applyGeometry)
{
    auto *timer = new QTimer(window);
    timer->setInterval(20);
    QObject::connect(timer, &QTimer::timeout, window,
                     [window, timer, applyGeometry, attempts = 50]() mutable {
        // X11 decorations arrive after show(), in a separate window-manager round trip.
        if (window->frameMargins().isNull()
                && QGuiApplication::platformName() == QStringLiteral("xcb") && --attempts > 0)
            return;
        if (attempts == 0)
            qWarning("Window frame extents unavailable; applying the client-area size limit.");
        timer->stop();
        applyGeometry();
        timer->deleteLater();
    });
    timer->start();
}
