# Release evidence checklist

Check a row only after running it against the relevant image. Source inspection
and fake-backend tests do not establish working inference or hardware support.

- [ ] Clean build with recorded source pins and package manifest
- [x] QEMU BIOS offline boot to ordinary-user launcher (r10, 4 GiB)
- [x] QEMU UEFI offline boot to ordinary-user launcher (r10, 4 GiB)
- [x] Two launcher activations created independent centered windows/sessions with isolated histories and closure (r7)
- [x] Source-level QML tests restore minimized or hidden chats newest-first before creating another session
- [x] Real starter-model response on CPU (r4/r7)
- [x] Real compatible remote endpoint response (HTTPS test server)
- [ ] Download/import/configuration and invalid credentials
- [ ] Stream cancellation and backend crash recovery
- [ ] Reduced motion and software rendering
- [ ] Default image compiles and launches example without network
- [x] Installer cancellation and in-use disk refusal (r4/r5)
- [ ] BIOS install, ISO removed, reboot, persisted conversation/project
- [x] UEFI install, ISO removed, reboot, persisted conversation/project (r4)
- [x] Power icons shut down and restart correctly (r4/r5)
- [x] Voice waveform illuminates during actual recording (r7)
- [x] Remote HTTPS transcription fills composer without sending (r7, synthetic microphone)
- [x] Real local transcription and synthesis (pinned engines)
- [x] Spoken reply reaches guest audio output (r7 with r9 playback dependency)
- [ ] USB/Bluetooth microphone and real speaker validation
- [x] Selected text attachment appears as chip and enters only its session's sent message (r7)
- [ ] PDF/scanned-file UX and long attachment context tests
- [ ] Ten cold boots and measured image/idle resources
- [ ] VMware display/network/storage validation

Backend automation: `bash scripts/test.sh`. Actual execution evidence is recorded
in `docs/qa/implementation-status.md` as work proceeds.
