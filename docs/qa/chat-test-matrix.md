# Release evidence checklist

Check a row only after running it against the relevant image. Source inspection
and fake-backend tests do not establish working inference or hardware support.

- [ ] Clean build with recorded source pins and package manifest
- [ ] QEMU BIOS offline boot to ordinary-user launcher
- [ ] QEMU UEFI offline boot to ordinary-user launcher
- [ ] Single centered chat window; close/reopen; terminal and app switching
- [ ] Real starter-model response on CPU
- [ ] Real compatible remote endpoint response
- [ ] Download/import/configuration and invalid credentials
- [ ] Stream cancellation and backend crash recovery
- [ ] Reduced motion and software rendering
- [ ] Default image compiles and launches example without network
- [ ] Installer cancellation and in-use disk refusal
- [ ] BIOS install, ISO removed, reboot, persisted conversation/project
- [ ] UEFI install, ISO removed, reboot, persisted conversation/project
- [ ] Power icons shut down and restart correctly
- [ ] Ten cold boots and measured image/idle resources
- [ ] VMware display/network/storage validation

Backend automation: `bash scripts/test.sh`. Actual execution evidence is recorded
in `docs/qa/implementation-status.md` as work proceeds.
