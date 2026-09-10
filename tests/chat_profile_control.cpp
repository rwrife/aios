#include "SessionControl.h"
#include <QTemporaryDir>
#include <QElapsedTimer>
#include <QThread>

static void check(bool condition, const char *message) {
    if (!condition) qFatal("%s", message);
}

static void finish(SessionControl &control) {
    QElapsedTimer elapsed;
    elapsed.start();
    while (control.busy() && elapsed.elapsed() < 10000) {
        QCoreApplication::processEvents();
        QThread::msleep(10);
    }
    check(!control.busy(), "Profile operation did not finish");
}

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    QTemporaryDir data;
    check(data.isValid(), "Could not create isolated profile storage");
    qputenv("XDG_DATA_HOME", data.path().toUtf8());
    qunsetenv("AIOS_SESSION_SOCKET");
    SessionControl control;
    int unlocked = 0;
    int cameraReleases = 0;
    int recognitionPurges = 0;
    QObject::connect(&control, &SessionControl::unlocked, [&] { ++unlocked; });
    QObject::connect(&control, &SessionControl::cameraReleaseRequested, [&] { ++cameraReleases; });
    QObject::connect(&control, &SessionControl::recognitionDataPurged,
                     [&] { ++recognitionPurges; });
    control.setSecureInput(true);
    control.takeProfilePhoto();
    check(cameraReleases == 1, "Profile photo did not request exclusive camera ownership");
    check(!control.setCameraPreviewActive(true), "Preview started during profile photo handoff");
    control.setSecureInput(false);
    auto child = qobject_cast<SessionControl *>(control.chatProfile());
    check(child, "Could not create child profile control");
    child->setSecureInput(true);
    child->takeProfilePhoto();
    check(cameraReleases == 2, "Child profile photo did not release root camera consumers");
    check(!control.setCameraPreviewActive(true), "Preview started during child profile photo handoff");
    child->setSecureInput(false);
    child->dispose();
    control.purgeRecognitionData();
    QElapsedTimer purgeElapsed;
    purgeElapsed.start();
    while (!recognitionPurges && purgeElapsed.elapsed() < 10000) {
        QCoreApplication::processEvents();
        QThread::msleep(10);
    }
    check(recognitionPurges == 1, "Facial recognition data purge did not finish");
    control.enroll("First account", "1234", true);
    finish(control);
    check(control.error().isEmpty(), "Creating the first account failed");
    check(control.profile().value("name") == "First account" && unlocked == 1,
          "The new account was not automatically selected");
    const auto firstId = control.profile().value("id");
    control.enroll("Second account", "5678", true);
    finish(control);
    check(control.profile().value("name") == "Second account" && unlocked == 2,
          "Creating another account did not switch selection");
    control.listProfiles();
    finish(control);
    check(control.profiles().size() == 2, "Created accounts were not persisted");
    check(control.profile().value("name") == "Second account", "Refreshing accounts lost selection");
    control.enroll("Second account", "5678", true);
    finish(control);
    check(!control.error().isEmpty() && unlocked == 2, "Duplicate creation falsely succeeded");
    control.unlock("First account", "1234");
    finish(control);
    check(control.error().isEmpty() && control.profile().value("id") == firstId && unlocked == 3,
          "The saved account could not be selected again");
}
