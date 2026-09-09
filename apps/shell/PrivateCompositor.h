#pragma once
#include <QWaylandQuickCompositor>
#include <QWaylandSeat>
#include <unistd.h>

// One instance per broker lease. No screen-copy, virtual-input, X11, or desktop
// clipboard extension is instantiated by the private scene.
class PrivateCompositor : public QWaylandQuickCompositor {
    Q_OBJECT
    Q_PROPERTY(int descriptor WRITE setDescriptor READ descriptor)
public:
    explicit PrivateCompositor(QObject *parent = nullptr) : QWaylandQuickCompositor(parent) {}
    ~PrivateCompositor() override { if (fd >= 0) ::close(fd); }
    int descriptor() const { return fd; }
    void setDescriptor(int value) { if (!isCreated() && fd < 0) fd = value; }
    void create() override {
        if (fd < 0 || isCreated()) return;
        setRetainedSelectionEnabled(false);
        setUseHardwareIntegrationExtension(false);
        addSocketDescriptor(fd);
        QWaylandQuickCompositor::create();
    }
    Q_INVOKABLE void initialize() { create(); }
    Q_INVOKABLE void clearInput() {
        if (defaultSeat()) defaultSeat()->setKeyboardFocus(nullptr);
    }
private:
    int fd = -1;
};
