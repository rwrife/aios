#pragma once
#include <QObject>
#include <QAudioSource>
#include <QAudioDevice>
#include <QMediaDevices>
#include <QAudioOutput>
#include <QMediaPlayer>
#include <QBuffer>
#include <QDataStream>
#include <QFile>
#include <QTemporaryDir>
#include <QTimer>

class Voice : public QObject {
    Q_OBJECT
public:
    explicit Voice(QObject *parent = nullptr) : QObject(parent) {
        limit.setSingleShot(true);
        connect(&limit, &QTimer::timeout, this, &Voice::finish);
    }
    ~Voice() { cancel(); }
    bool recording() const { return source != nullptr; }
    bool speaking() const { return player && player->playbackState() == QMediaPlayer::PlayingState; }
    QString speechPath() const { return files.path() + "/reply.wav"; }
    void start() {
        if (source) return;
        stopPlayback();
        auto device = QMediaDevices::defaultAudioInput();
        if (device.isNull()) { emit error("No microphone found. Connect one and try again."); return; }
        QAudioFormat format; format.setSampleRate(16000); format.setChannelCount(1); format.setSampleFormat(QAudioFormat::Int16);
        if (!device.isFormatSupported(format)) { emit error("This microphone cannot record in the supported format."); return; }
        samples.clear(); buffer.setBuffer(&samples); buffer.open(QIODevice::WriteOnly);
        source = new QAudioSource(device, format, this);
        connect(source, &QAudioSource::stateChanged, this, [this](QtAudio::State state) {
            if (source && state == QtAudio::StoppedState && source->error() != QtAudio::NoError)
                QTimer::singleShot(0, this, [this] { cancel(); emit error("Microphone recording failed. Check audio settings."); });
        });
        source->start(&buffer); limit.start(60000); emit changed();
    }
    void finish() {
        if (!source) return;
        auto old = source; source = nullptr; old->stop(); old->deleteLater(); limit.stop(); buffer.close();
        const QString path = files.path() + "/recording.wav";
        QFile file(path);
        if (samples.size() < 3200 || !file.open(QIODevice::WriteOnly)) {
            samples.clear(); emit changed(); emit error("Record a little longer and try again."); return;
        }
        file.setPermissions(QFile::ReadOwner | QFile::WriteOwner);
        QDataStream stream(&file); stream.setByteOrder(QDataStream::LittleEndian);
        stream.writeRawData("RIFF", 4); stream << quint32(samples.size() + 36); stream.writeRawData("WAVEfmt ", 8);
        stream << quint32(16) << quint16(1) << quint16(1) << quint32(16000) << quint32(32000) << quint16(2) << quint16(16);
        stream.writeRawData("data", 4); stream << quint32(samples.size()); stream.writeRawData(samples.constData(), samples.size());
        file.close(); samples.clear(); emit changed(); emit recorded(path);
    }
    void cancel() {
        limit.stop();
        if (source) { auto old = source; source = nullptr; old->stop(); delete old; }
        buffer.close(); samples.clear(); QFile::remove(files.path() + "/recording.wav"); stopPlayback(); emit changed();
    }
    void play(const QString &path) {
        ensurePlayer();
        player->setSource(QUrl::fromLocalFile(path));
        player->play();
    }
    void stopPlayback() {
        if (player) {
            player->stop();
            player->setSource({});
        }
        QFile::remove(speechPath());
    }
signals:
    void changed();
    void recorded(const QString &path);
    void error(const QString &text);
private:
    QTemporaryDir files;
    QByteArray samples;
    QBuffer buffer;
    QAudioSource *source = nullptr;
    QTimer limit;
    QAudioOutput *output = nullptr;
    QMediaPlayer *player = nullptr;
    void ensurePlayer() {
        if (player)
            return;
        output = new QAudioOutput(this);
        player = new QMediaPlayer(this);
        player->setAudioOutput(output);
        connect(player, &QMediaPlayer::playbackStateChanged, this, [this] { emit changed(); });
        connect(player, &QMediaPlayer::errorOccurred, this, [this] {
            emit error("Could not play the spoken reply. Check your audio output.");
        });
        connect(player, &QMediaPlayer::mediaStatusChanged, this, [this](QMediaPlayer::MediaStatus status) {
            if (status == QMediaPlayer::EndOfMedia) {
                player->setSource({});
                QFile::remove(speechPath());
            }
        });
    }
};
