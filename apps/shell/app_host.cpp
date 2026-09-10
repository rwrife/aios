#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQuickWindow>
#include <QStringDecoder>
#include <QVariantMap>

#include <cerrno>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <unistd.h>

namespace {

class ReadyDescriptor
{
public:
    explicit ReadyDescriptor(int descriptor = -1)
        : descriptor_(descriptor)
    {
    }

    ~ReadyDescriptor()
    {
        if (descriptor_ >= 0)
            ::close(descriptor_);
    }

    ReadyDescriptor(const ReadyDescriptor &) = delete;
    ReadyDescriptor &operator=(const ReadyDescriptor &) = delete;

    void reset(int descriptor)
    {
        if (descriptor_ >= 0)
            ::close(descriptor_);
        descriptor_ = descriptor;
    }

    int release()
    {
        const int descriptor = descriptor_;
        descriptor_ = -1;
        return descriptor;
    }

private:
    int descriptor_;
};

int fail()
{
    std::fputs("aios-app-host: startup failed\n", stderr);
    return EXIT_FAILURE;
}

bool parseReadyDescriptor(ReadyDescriptor &ready)
{
    if (!qEnvironmentVariableIsSet("AIOS_APP_READY_FD"))
        return true;

    const QByteArray value = qgetenv("AIOS_APP_READY_FD");
    if (value.isEmpty())
        return false;
    for (const char character : value) {
        if (character < '0' || character > '9')
            return false;
    }

    bool valid = false;
    const qlonglong descriptor = value.toLongLong(&valid, 10);
    if (!valid || descriptor > INT_MAX)
        return false;
    ready.reset(static_cast<int>(descriptor));
    return true;
}

bool readTitle(QString &title)
{
    const QByteArray value = qgetenv("AIOS_APP_TITLE");
    QStringDecoder decoder(QStringDecoder::Utf8);
    title = decoder.decode(value);
    if (decoder.hasError() || title.toUcs4().size() < 1 || title.toUcs4().size() > 100)
        return false;
    for (const QChar character : title) {
        const ushort codeUnit = character.unicode();
        if (codeUnit <= 0x1f || codeUnit == 0x7f)
            return false;
    }
    return true;
}

void signalReady(int descriptor)
{
    static constexpr char message[] = "ready\n";
    std::size_t written = 0;
    while (written < sizeof(message) - 1) {
        const ssize_t result = ::write(
            descriptor,
            message + written,
            sizeof(message) - 1 - written);
        if (result > 0) {
            written += static_cast<std::size_t>(result);
            continue;
        }
        if (result < 0 && errno == EINTR)
            continue;
        break;
    }
    ::close(descriptor);
}

} // namespace

int main(int argc, char *argv[])
{
    ReadyDescriptor ready;
    if (!parseReadyDescriptor(ready))
        return fail();
    if (argc != 1)
        return fail();
    if (qgetenv("AIOS_APP_TEMPLATE") != QByteArrayLiteral("calculator"))
        return fail();

    QString title;
    if (!readTitle(title))
        return fail();

    QGuiApplication application(argc, argv);
    QQmlApplicationEngine engine;
    engine.setInitialProperties({{QStringLiteral("appTitle"), title}});
    engine.load(QStringLiteral("qrc:/AppHost.qml"));
    if (engine.rootObjects().size() != 1)
        return fail();

    auto *window = qobject_cast<QQuickWindow *>(engine.rootObjects().constFirst());
    if (window == nullptr)
        return fail();

    const int descriptor = ready.release();
    if (descriptor >= 0) {
        const auto pendingDescriptor = std::make_shared<int>(descriptor);
        QObject::connect(window, &QQuickWindow::frameSwapped, window, [pendingDescriptor]() {
            if (*pendingDescriptor < 0)
                return;
            const int current = *pendingDescriptor;
            *pendingDescriptor = -1;
            signalReady(current);
        });
    }

    return application.exec();
}
