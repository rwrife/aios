// Synthetic client installed at the calculator path only in a disposable test
// container. It exercises the real allowlisted launch and shared-memory surface.
#include <QGuiApplication>
#include <QRasterWindow>
#include <QPainter>
#include <QTimer>
#include <QKeyEvent>
#include <QClipboard>
#include <fstream>
#include <unistd.h>

class TestSurface : public QRasterWindow {
protected:
    void keyPressEvent(QKeyEvent *event) override {
        std::ofstream("/workspace/display-keys.txt", std::ios::app) << event->key() << '\n';
        std::ofstream("/workspace/display-clipboard.txt") << (QGuiApplication::clipboard()->text().isEmpty() ? "isolated" : "unexpected selection");
    }
    void paintEvent(QPaintEvent *) override {
        QPainter painter(this);
        const QRect bounds(QPoint(), size());
        painter.fillRect(bounds, QColor(255, 0, 0));
        painter.setPen(Qt::white);
        painter.drawText(bounds, Qt::AlignCenter, "Isolated test surface");
        std::ofstream("/workspace/display-test.json") << "{\"painted\":true,\"uid\":" << getuid() << "}";
    }
};

int main(int argc, char **argv) {
    qInstallMessageHandler([](QtMsgType, const QMessageLogContext &, const QString &message) {
        std::ofstream("/workspace/display-client.log", std::ios::app) << message.toStdString() << '\n';
    });
    QGuiApplication application(argc, argv);
    TestSurface surface;
    surface.resize(320, 240);
    surface.show();
    QTimer::singleShot(20000, &application, &QGuiApplication::quit);
    return application.exec();
}
