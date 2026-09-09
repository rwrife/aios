# Ambient identity: initial decisions and threat model

Status: experimental; production integration blocked on display and input isolation.

## Decisions

1. **Principals and execution.** UUIDs, never display names, select administrator
   mappings to distinct Linux UIDs. A privileged broker owns mapping, lifecycle,
   capabilities and journals. Apps get an artifact subtree through bubblewrap,
   private namespaces, an empty environment and a cgroup v2 scope. The first
   version is sequential. UID/namespace isolation needs kernel-level validation.
2. **Storage.** Use administrator-provisioned LUKS volumes for personal journals
   and artifacts, with separate AES-GCM records for verifiers/templates/secrets.
   PINs are scrypt verifiers and never volume keys. Root-only key files are a
   development deployment choice; TPM binding remains required for the stronger
   unattended-device threat model. No disk is automatically formatted.
3. **Display.** Shared X11 cannot be a trusted input/display boundary. Require
   isolated Wayland compositor endpoints and a trusted shell; deny production
   personal activation until this prerequisite has been validated. The QML shield
   is a privacy aid in the simulator, not a defense against hostile X11 clients.
4. **Capabilities.** Use opaque random in-memory tokens rather than portable
   signed bearer claims. Every use checks the active lease, owner, resource,
   operation and deadline. Loss/conflict revokes all tokens. Restart loses all
   authority. Transaction tokens are exact-resource, single-use; UI must present
   that resource without model reinterpretation.
5. **Model trust.** Pin local weights by digest, source, license and revision.
   No default weights or empirical thresholds are shipped. No liveness adapter
   means no liveness, not implicit success. Name metadata cannot recover accounts.

## Trust boundaries and attacks

| Threat | Enforcement / remaining release gate |
| --- | --- |
| Prompt/document asks to change UID or fetch secrets | Strict action/field schema, peer UID role checks, no model-facing privileged tools; worker principal migration still required |
| Another app connects to the broker | Private socket permissions plus Linux peer credentials; apps must run under distinct UIDs with socket hidden |
| Face/voice replay or recognized bystander | Required interaction, stability/liveness and unambiguous speaker policy; actual anti-spoofing not implemented |
| Conflicting or stale identity | Decay, immediate conflict revocation, no ownership transfer, shield and teardown timers |
| Enrollment name poisoning | New UUID, explicit consent, duplicate-name denial, high-entropy recovery rather than spoken name |
| PIN brute force / restart | Persistent scrypt verifier/counters, exponential delay and ten-failure lockout; power rollback and TPM protection remain |
| Stolen capability / changed transaction | Lease-owner-operation-resource binding, short expiry and single use |
| Process forks or becomes orphan | cgroup scope attached before exec; cgroup.kill on suspension/recovery; real kernel testing remains |
| Journal reads or symlink attacks by apps | Journals are root-owned outside the app-mounted artifact subtree; file claim/export broker remains unimplemented |
| Shared display input snooping or clipboard theft | Fail closed until separately isolated display passes tests; current X11 must not be marked validated |
| Stolen disk / root compromise | Encrypted volumes and authenticated records; root key files do not resist root compromise or disk theft by themselves |
| Secret in error/log/transcript | Generic service errors; audit records contain only time/action/decision; restricted account adapter omits raw responses and tokens |
| Slow or malformed local client | Bounded frame, deadline, duplicate-key rejection, no exception/body echo; single-threaded expensive actions still affect timer latency |
| Failed teardown/unmount | Sticky fault/shield prevents activation; operator must repair storage and restart service |

The identity service is trusted to report calibrated evidence. The session broker
is trusted for authorization, volume and process operations. Their compromise is
outside the current application-sandbox guarantee. Separating secret and policy
processes, secure update/rollback protections, and recovery/deletion audit remain
follow-up implementation and review work.
