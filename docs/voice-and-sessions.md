# Chat windows, attachments and voice

Proposed next step: [wake-word activation and spoken conversation implementation
plan](plans/wake-word-speech-to-text.md), including concise spoken replies and
optional local model upgrades. Wake-word listening and automatic voice-origin
replies are not yet implemented.

The launcher restores the most recently minimized or hidden QML chat window
before creating a new `Backend` session and window. The desktop owns shared model
configuration and one loopback model server; each session owns its conversation,
attachment snapshot, worker process and audio capture/playback. Closing a window
cancels that session's work. Histories use UUID filenames with owner-only
permissions. Legacy `conversation.json` is retained but never loaded into a
newly opened chat.

The waveform/Voice button has one explicit `active` property bound to recording
state, plus immediate pressed feedback. `Backend::setVoiceActive(bool)` is the
shared entry point for UI and future callers. Failure to open a microphone leaves
the control inactive and displays an error. Activation never merely paints a
recording indicator without starting capture.

Qt captures 16 kHz mono PCM WAV through PulseAudio. Clips are held in a per-session
temporary directory, capped at 60 seconds, and removed after transcription or
cancellation. Transcribed text is inserted into the draft. A reply's audio action
requests a WAV response and plays it through Qt Multimedia; stopping/closing the
session removes temporary audio. Voices are synthesized, not recordings of a person.

Remote voice uses separately stored URL/key/model/voice settings. Chat credentials
are never reused implicitly. Changing the voice URL clears its saved key unless a
replacement is supplied. TLS verification is enabled and authenticated redirects
are refused. Requests follow the compatible
[transcription](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
and [speech](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)
schemas. Endpoint/model support depends on the configured service.

On-device speech recognition uses pinned whisper.cpp and a checksum-verified
Whisper tiny.en download (MIT, English, approximately 75 MiB). Its recognition is
limited; the synthetic test phrase had one word error. eSpeak NG provides local
speech synthesis. The default ISO includes both engines but downloads recognition
weights separately. Local recognition and remote services use the same composer.

Attachments are explicitly selected text/source files or PDFs with selectable
text, up to four files per message and 256 KiB extracted text per file. A file's
snapshot is included only when Send is pressed. Model output remains inert plain
text. No attached instructions trigger desktop commands. Images/scanned PDFs are
rejected with an explanation.

CLI examples:

```sh
aios-llm configure --voice-mode remote --voice-url https://speech.example/v1 --ask-voice-key
aios-llm transcribe recording.wav
aios-llm speak 'Hello' --output hello.wav
aios-llm setup-voice
aios-llm configure --voice-mode local
```
