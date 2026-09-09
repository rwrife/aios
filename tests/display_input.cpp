// A test-only shell scene uses the production private display and PIN overlay.
// No production executable installs or loads this fixture.
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickWindow>
#include <QWaylandQuickItem>
#include <QWaylandSurface>
#include <QKeyEvent>
#include <QFile>
#include <QClipboard>
#include "SessionControl.h"
#include "DisplayBridge.h"
#include "PrivateCompositor.h"

class TestControl : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool enabled READ enabled CONSTANT)
    Q_PROPERTY(bool shield READ shield CONSTANT)
    Q_PROPERTY(bool secureInput READ secureInput NOTIFY changed)
    Q_PROPERTY(QVariantMap challenge READ challenge NOTIFY changed)
public:
    explicit TestControl(SessionControl &session) : session(session) {}
    bool enabled() const { return true; }
    bool shield() const { return false; }
    bool secureInput() const { return secure; }
    QVariantMap challenge() const { return request; }
    Q_INVOKABLE void setSecureInput(bool value) { secure = value; emit changed(); }
    Q_INVOKABLE void cancelChallenge() { request.clear(); emit changed(); }
    Q_INVOKABLE void displayReady(const QString &lease, const QString &app) { session.displayReady(lease, app); }
    Q_INVOKABLE void displayFailed() {}
    void showPin() {
        request = {{"operation", "synthetic-test"}, {"resource", "No real account"}};
        emit changed();
    }
signals:
    void changed();
    void privacyLost();
    void displayRequested(const QString &app);
private:
    SessionControl &session;
    bool secure = false;
    QVariantMap request;
};

static void key(QQuickWindow *window, int value, quint32 scan, const QString &text) {
    QKeyEvent press(QEvent::KeyPress, value, Qt::NoModifier, scan, 0, 0, text);
    QKeyEvent release(QEvent::KeyRelease, value, Qt::NoModifier, scan, 0, 0, text);
    QCoreApplication::sendEvent(window, &press);
    QCoreApplication::sendEvent(window, &release);
}

int main(int argc, char **argv) {
    QQuickWindow::setGraphicsApi(QSGRendererInterface::OpenGL);
    QGuiApplication app(argc, argv);
    QGuiApplication::clipboard()->setText("Synthetic trusted-shell clipboard");
    qmlRegisterType<PrivateCompositor>("AIOS.Display", 1, 0, "PrivateCompositor");
    SessionControl session;
    DisplayBridge bridge;
    TestControl control(session);
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("testControl", &control);
    engine.rootContext()->setContextProperty("displayBridge", &bridge);
    engine.load(QUrl::fromLocalFile("/workspace/tests/display_input.qml"));
    if (engine.rootObjects().isEmpty()) return 2;
    auto window = qobject_cast<QQuickWindow *>(engine.rootObjects().first());
    if (!window) return 3;
    window->requestActivate();
    QTimer::singleShot(1500, &control, [&] { emit control.displayRequested(""); });
    QTimer poll;
    poll.setInterval(100);
    QObject::connect(&poll, &QTimer::timeout, &app, [&] {
        auto surface = window->findChild<QWaylandQuickItem *>();
        if (!surface || !surface->surface() || !surface->surface()->hasContent()) return;
        poll.stop();
        surface->takeFocus();
        QTimer::singleShot(150, &app, [&] {
            key(window, Qt::Key_A, 38, "a");
            QTimer::singleShot(150, &app, [&] {
                control.showPin();
                QTimer::singleShot(150, &app, [&] {
                    for (int digit = 1; digit <= 6; ++digit)
                        key(window, Qt::Key_0 + digit, 9 + digit, QString::number(digit));
                    QTimer::singleShot(150, &app, [&] {
                        auto input = window->findChild<QObject *>("secureOverlayPin");
                        const bool received = input && input->property("text").toString() == "123456";
                        QFile report(qgetenv("AIOS_TEST_REPORT"));
                        if (report.open(QIODevice::WriteOnly)) report.write(received ? "{\"pin_received\":true}" : "{\"pin_received\":false}");
                    });
                });
            });
        });
    });
    poll.start();
    QTimer::singleShot(30000, &app, &QGuiApplication::quit);
    return app.exec();
}
#include "display_input.moc"
