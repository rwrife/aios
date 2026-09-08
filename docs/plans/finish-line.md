# Finish-line backlog

Updated 2026-09-08. Detailed execution evidence is in
[implementation status](../qa/implementation-status.md).

| Task | Implementation | Remaining acceptance work |
|---|---|---|
| T01 Product specification | Done | Keep release scope aligned with validated machines |
| T02 Build and offline boot | Working BIOS/UEFI ISO, pins and manifests | Independent clean rebuild comparison |
| T03 Ordinary user and recovery | Done; forced shell failure tested | Broader failure injection |
| T04 Native UI and inference feasibility | Done; real local and HTTPS remote answers | Additional hardware/provider coverage |
| T05 LLM runtime/configuration/CLI | Done; shell-owned local server and shared Python backend | Model failure and timeout coverage |
| T06 Desktop and launcher | Done; centered chat, terminal, power | Keyboard, small-screen and multi-monitor QA |
| T07 Streaming chat | Done; plain text, copy, new chat, stop | Interactive cancellation and long-reply QA |
| T08 Onboarding and persistence | Done; download, import, remote configuration, saved history | Interrupted download/storage exhaustion QA |
| T09 Ambient theme | Done; original waves, software renderer, reduced motion option | Reduced-motion and resolution QA |
| T10 Default developer tools | Done; default image compiles a Qt app offline | Complete app launch/window-switch QA |
| T11 Installer | UEFI install/reboot/persistence passed | Final pristine BIOS install pass |
| T12 Release validation | Backend tests, BIOS/UEFI offline smoke automation, manifests | CI build execution, ten cold boots, VMware/hardware, release publication |

Next order: close BIOS installer and interaction QA, execute the clean CI image
build, then validate VMware and the first physical target before publishing a
release. The current image is a development candidate. Do not equate implemented
features with completion of every release acceptance check.
