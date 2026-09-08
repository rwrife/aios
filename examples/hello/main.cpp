#include <QApplication>
#include <QLabel>
int main(int argc, char **argv) {
    QApplication app(argc, argv);
    QLabel label("Hello from your AIOS desktop app");
    label.setAlignment(Qt::AlignCenter);
    label.resize(480, 240);
    label.show();
    return app.exec();
}
