# NVIDIA Build LB — Canonical Product and Implementation Contract (v3)

## 1. Authority, completion rule, and retired material

This document is the single shipping design authority for `nvidia-build-lb`.
The user's current Goal and repository safety policy outrank it. Any design
change requires a new complete snapshot and independent read-only review before
implementation. A substantive source change invalidates older candidate,
browser, scan, live, and implementation-review receipts.

The Python/FastAPI/SQLAlchemy/Alembic application, vanilla-JavaScript admin
fragments, `docs/ARCHITECTURE.md`, earlier `DESIGN.md` revisions, and their test
or image evidence are **retired shipping authority**. They may be read only as
behavioral or provenance oracles while equivalent v3 fixtures are created.
They cannot decide v3 behavior and cannot appear in the final application
image. Git history is the only archive of the retired design; this file has no
partly authoritative legacy appendix.

The result is complete only when one unchanged Rust/Svelte/SQLx source commit
and its immutable application image have all required evidence, both fresh
NVIDIA credentials have succeeded independently, every advertised modality
profile has succeeded through the balancer, the two-key routing and recovery
matrix has passed, the public hostname and Hermes end-to-end task have passed,
secret scans are clean, `codex-lb` is unchanged and healthy, both application
and infrastructure PRs have received current-head Codex LGTM, and the user has
merged both PRs. Codex never merges a PR.

### 1.1 Exposed credential rule

The two NVIDIA credentials exposed in conversation are compromised. They are
never called, copied to v3, attached, logged, displayed, or used as evidence.
Immediately after this design gate is approved, and before implementation or
another legacy upstream call, the operator runs the one-shot bootstrap below
and enters `compromised_hold`. Each invocation first acquires the stable
operation lock and then the existing legacy
`/etc/nvidia-build-lb/runtime.env.lock` exclusively. It records the delayed
updater timer/service unit hashes, metadata,
`is-enabled`/active states, and `Persistent` value in the root hold journal;
disables and stops the timer; materializes runtime masks for the current boot;
and installs
persistent root-owned hold drop-ins at
`/etc/systemd/system/agent-apps-delayed-update.timer.d/00-nblb-compromised-hold.conf`
and
`/etc/systemd/system/agent-apps-delayed-update.service.d/00-nblb-compromised-hold.conf`,
both 0644, with
`ConditionPathExists=/run/nvidia-build-lb-updater-unblocked` while that path is
guaranteed absent. It then durably requests/stops an already active updater
service, waits for its exact InvocationID's MainPID/ControlPID and all children
to reach zero, and proves an attempted start refused or condition-skipped. Only
after that wrapper has released any Hermes lock does hold request and acquire
the Hermes lock. This ordering cannot deadlock with the updater, which takes
only Hermes. After daemon-reload it proves the timer/service inactive and
inhibited on every resume.
The journal is `/opt/nvidia-build-lb/state/compromised-hold.json`, root:root
0600 under the bootstrap-created/validated 0700 state parent. This survives
reboot and Persistent catch-up. It then stops Hermes request intake, stops only
`agent-hermes`, drains the legacy app, proves `live_pin=0` and
`pending_attempt=0`, and stops only the legacy app on 2456.
The legacy PostgreSQL volume, ciphertext, containers, and evidence remain
preserved. If drain cannot be proved, Hermes remains paused and the app is
stopped without deleting state. Both exposed provider key IDs must then be
shown as revoked/deleted in the NVIDIA control plane without revealing their
values, and a secret-free receipt must be retained. No source implementation,
legacy export, ciphertext retirement, public v3 cutover, or fresh-key entry may
cross this external gate, except the narrowly defined hold artifact below. Only
two fresh user-owned keys entered through the v3 owner console may become
routing credentials.

The circular dependency is closed explicitly. After all design reviewers LGTM
and before hold, Codex may implement only
`scripts/ops/compromised-hold.sh` plus its no-root fixture/syntax tests from this
approved section. It contains no provider API client, never reads an
NVIDIA credential value, and can only bootstrap locks/state, call the existing
loopback legacy drain/status surface or read its safe DB counts, quiesce the
named timer/service, Hermes container, and legacy app, and record revocation receipts. It contains
no NVIDIA/provider API call path. No other source,
runtime, migration, UI, or provider work is allowed first. The operator entry
points are exactly; the two UUID arguments are the nonsecret NVIDIA
control-plane IDs of the exposed credentials, supplied in either order and
canonicalized by UUID network bytes:

```text
HOLD=/home/dongwonttuna/Documents/Programming/nvidia-build-lb/scripts/ops/compromised-hold.sh
sudo -- /bin/bash "$HOLD" enter <credential-id-1> <credential-id-2>
sudo -- /bin/bash "$HOLD" status
sudo -- /bin/bash "$HOLD" confirm-revocation <receipt.json>
```

`HOLD` is a nonsecret shell variable and each sudo invocation is one command. Before
sudo, the script SHA-256 and `bash -n`/fixture receipt are shown. The root journal
is canonical minified UTF-8 JSON plus LF with exactly:

```text
{
  "version":1,"transaction_id":UUID,"phase":HoldPhase,
  "created_at":Timestamp,"updated_at":Timestamp,
  "initial_boot_id":UUID,
  "initial_boot_inhibition_sha256":LowerHex64|null,
  "expected_revocation_ids":[UUID,UUID],
  "legacy_lock":{
    "path":"/etc/nvidia-build-lb/runtime.env.lock","sha256":LowerHex64,
    "uid":0,"gid":0,"mode":"0644","inode":Decimal,"nlink":1
  },
  "timer":{
    "unit":"agent-apps-delayed-update.timer","unit_sha256":LowerHex64,
    "is_enabled":"enabled"|"disabled","active":"active"|"inactive",
    "persistent":true
  },
  "service":{
    "unit":"agent-apps-delayed-update.service","unit_sha256":LowerHex64,
    "is_enabled":"static","active":"active"|"inactive",
    "invocation_id":LowerHex32|null,"main_pid":Decimal,"control_pid":Decimal,
    "cgroup_receipt":CgroupQuiesceReceipt|null
  },
  "hermes":ContainerTuple,"legacy_app":ContainerTuple,
  "preexisting_files":[HoldFile,HoldFile,HoldFile,HoldFile],
  "revocations":[Revocation|null,Revocation|null],"failure":HoldFailure|null
}
ContainerTuple={"name":String,"id":LowerHex64,"image":String,
                "config_sha256":LowerHex64,"before":"running"|"stopped",
                "restart_policy_before":RestartPolicy,
                "restart_policy_after":"no","after":"stopped"}
RestartPolicy="no"|"always"|"unless-stopped"|"on-failure"|
              "on-failure:<1..2147483647>"
LowerHex32=[0-9a-f]{32}
Revocation={"credential_id":UUID,"state":"revoked"|"deleted",
            "observed_at":Timestamp,"evidence_sha256":LowerHex64}
HoldPathTuple={"kind":"regular","path":AbsolutePath,"sha256":LowerHex64,
               "uid":Decimal,"gid":Decimal,"mode":Octal4,"device":Decimal,
               "inode":Decimal,"size":Decimal,"nlink":1}
             |{"kind":"symlink","path":AbsolutePath,"target":AbsolutePath,
               "uid":Decimal,"gid":Decimal,"mode":Octal4,"device":Decimal,
               "inode":Decimal,"size":Decimal,"nlink":1}
HoldFile={"path":AbsolutePath,"role":"timer_dropin"|"service_dropin"|
          "remask_fragment"|"wants_link","before":HoldPathTuple|null,
          "classification":"absent"|"managed_previous"|"user_drift"}
CgroupQuiesceReceipt={"version":1,"invocation_id":LowerHex32|null,
                      "boot_id":UUID,"observed_at":Timestamp,
                      "cgroup_path":AbsolutePath,
                      "main_pid":Decimal,"control_pid":Decimal,
                      "pids":[{"pid":Decimal,"start_ticks":Decimal}],
                      "all_zero":true,"sha256":LowerHex64}
CgroupQuiesceReceipt.sha256 is
`SHA-256(RFC8785_JCS(receipt with the sha256 member omitted))`. Before hashing,
`pids` is sorted by `(pid,start_ticks)` ascending network-order decimal and
duplicates are rejected; `all_zero:true` means the sorted list is empty and
`main_pid=control_pid=0`. The digest input is the exact UTF-8 JCS bytes and
never contains a self-reference.
HoldFailure="invalid_metadata"|"lock_busy"|"unit_state_changed"|
            "unit_quiesce_failed"|"restart_policy_failed"|
            "hermes_stop_failed"|
            "legacy_drain_unproved"|"legacy_stop_failed"|
            "receipt_invalid"|"tuple_unknown"
```

Before any hold file or symlink side effect, `enter` inspects exactly these
four paths: the two condition drop-ins
`agent-apps-delayed-update.timer.d/00-nblb-compromised-hold.conf`
and `agent-apps-delayed-update.service.d/00-nblb-compromised-hold.conf`,
the `nvidia-build-lb-compromised-hold-remask.service` fragment, and its
`multi-user.target.wants` link. An absent path is installed; a path whose bytes,
owner, mode, and link target equal the prior managed tuple is adopted; any
other existing file or link is `user_drift`, persists in `preexisting_files`,
and stops at `failed_attention` without overwrite. Release restores or removes
only an exact `managed_previous` tuple and never unlinks a user file, fragment,
or Wants link merely because its name matches. The same CAS/readback rule is
applied after daemon-reload and on every reboot resume.

`expected_revocation_ids` is strictly sorted by UUID network bytes and cannot
change after `prepared`. Revocation slots have the same order. Before receipt
confirmation both are null; confirmation fills only the matching slot and an
identical receipt is idempotent. `complete` requires two nonnull, distinct
receipts matching those exact expected IDs. A receipt file is canonical
minified UTF-8 JSON plus LF with exactly
`{"version":1,"credential_id":UUID,"state":"revoked"|"deleted",
"observed_at":Timestamp,"evidence_sha256":LowerHex64}`. The evidence digest
binds the separately retained, manually inspected NVIDIA control-plane export
or screenshot; `confirm-revocation` verifies the closed file, matching ID,
canonical bytes, timestamp, state, and idempotency but never opens a provider
session. No credential value, provider response body, account identity, or
free-form text is allowed in either file or journal.

The exact phases are
`prepared|operations_legacy_locks_ready|timer_state_saved|timer_disable_requested|
timer_disabled|masks_requested|masks_installed|dropins_requested|
dropins_installed|daemon_reload_requested|daemon_reloaded|
remask_install_requested|remask_installed|remask_enable_requested|
remask_enabled_verified|
service_stop_requested|service_stopped|
hermes_lock_requested|locks_ready|units_quiescent|hermes_restart_policy_requested|
hermes_restart_policy_no|hermes_stop_requested|hermes_stopped|
legacy_drain_requested|legacy_drained|legacy_restart_policy_requested|
legacy_restart_policy_no|legacy_stop_requested|legacy_stopped|
revocation_pending|complete|failed_attention`. Before every side effect the
corresponding requested phase and expected tuple are temp-written, fsynced,
renamed, parent-fsynced, and read back; after the side effect its result is
verified and the completed phase is persisted the same way. `enter` on a
partial journal accepts only exact before/after tuples and resumes toward the
safer stopped state; it never re-enables/restarts anything. Unknown tuples stop
at `failed_attention`. A resumed invocation never trusts a historic lock phase:
it reacquires operations/legacy, re-proves both unit inhibitors and service PID
zero, then reacquires Hermes before any container step. The restart policy is
changed with an exact container-ID
CAS before each stop and read back as `no`; therefore Docker daemon or host
restart cannot revive either compromised path while the systemd updater is
persistently inhibited. `status` is read-only and complete is impossible until
both closed receipt files validate. This journal is immutable hold evidence;
later updater restoration is owned by the separate release journal below and
never adds an undeclared phase to this file.

Systemd runtime masks live under `/run` and are not falsely treated as
 persistent across reboot. The persistent safety authority is the two exact
condition drop-ins plus absent unblock path. Hold installs one root unit and an
explicit boot trigger: `nvidia-build-lb-compromised-hold-remask.service` has
`DefaultDependencies=no`, `Before=timers.target
agent-apps-delayed-update.timer agent-apps-delayed-update.service`, and a fixed
ExecStart in the hold script, then is enabled through the exact
`/etc/systemd/system/multi-user.target.wants/nvidia-build-lb-compromised-hold-remask.service`
symlink (`WantedBy=multi-user.target`). `remask_install_requested`,
`remask_installed`, `remask_enable_requested`, and
`remask_enabled_verified` journal the fragment/link tuple, daemon-reload,
`is-enabled`, and effective trigger readback; a unit file without this link is
not a boot guarantee. On the initial boot and every later boot, or
before any hold `status|confirm-revocation`/release action, it owns
`/opt/nvidia-build-lb/state/compromised-hold-boots/<boot-id>.json`:

```text
HoldBootInhibitionJournal={
 version:1,hold_transaction_id:UUID,hold_journal_sha256:LowerHex64|null,
 boot_id:UUID,phase:"prepared"|"mask_requested"|"masked_verified",
 timer:{unit:"agent-apps-delayed-update.timer",masked:Boolean,
        active:"active"|"inactive"},
service:{unit:"agent-apps-delayed-update.service",masked:Boolean,
          active:"active"|"inactive",invocation_id:LowerHex32|null,
          main_pid:Decimal,control_pid:Decimal,
          cgroup_receipt:CgroupQuiesceReceipt|null},
remask:{unit:"nvidia-build-lb-compromised-hold-remask.service",
         fragment:PathFileTuple|null,boot_trigger:"multi-user.target",
         wants_link:PathFileTuple|null,enabled_before:Boolean,
         enabled_target:true},
 condition_dropins_sha256:LowerHex64,unblock_path_absent:true,
 verified_at:Timestamp|null
}
```

The initial-boot child has null hold hash and is bound by transaction ID plus
the exact expected-ID/drop-in projection, avoiding a hash cycle while the hold
is still advancing; every post-complete boot child requires the immutable
complete hold hash. `mask_requested` is durable before recreating both runtime
masks for the current boot; repeated mask is idempotent. `masked_verified` requires both masked/inactive, zero service
PIDs, exact effective persistent conditions and absent unblock path. The initial
child hash fills the nullable hold field before `masks_installed`; later boot
children do not rewrite the complete hold. Every child also verifies the
remask fragment/link/hash and effective `multi-user.target` trigger. If the
early unit has not run, the
persistent condition still blocks execution and the first operator command
creates/verifies the child before any other step. Hold release binds the current
boot child's hash and remask trigger tuple, accepts no prior-boot mask observation, and only then performs
its service/timer unmask phases. Thus reboot changes ephemeral mask material,
not the immutable safety decision or saved pre-hold unit state.

The boot-child union is nullability-closed. In `prepared|mask_requested`,
`hold_journal_sha256` may be null only for the initial child, both runtime
masks are either absent or the exact requested tuple, and
`remask.fragment`/`remask.wants_link` are null until their corresponding
install/enable phases. `masked_verified` requires a nonnull fragment and
nonnull enabled Wants link, exact fragment/link hashes, `enabled_target:true`,
both masks active, zero service PIDs, and an absent unblock path; its hold hash
is null only for that first pre-complete child and nonnull for every later boot.
The child never claims `masked_verified` from a pre-hold or prior-boot mask.
Hold release's `fragment_before`/`wants_link_before` are nullable only when the
unit/link was absent at capture; `fragment_target` and `wants_link_target` are
nonnull exact installed tuples. `remask_disable_requested` may remove only the
exact target Wants link; `remask_disabled_verified` requires link absence,
`is-enabled=disabled`, no effective boot trigger, and the target fragment still
byte-identical. A crash before that completed readback leaves the link enabled
and the persistent condition fail-closed; release never silently deletes the
unit or treats a missing link as disabled success.

`daemon_reload_requested` is durable before the one `systemctl daemon-reload`
that makes both hold drop-ins effective. `daemon_reloaded` requires a fresh
manager readback proving both exact drop-in paths/hashes are in the effective
unit definitions, the unblock path is absent, the timer is disabled/inactive,
and a service start is condition-skipped with no new InvocationID or process.
A crash at the requested phase repeats daemon-reload, which is the only
idempotent side effect in this transition, and performs the same readback; no
service/container step may begin from `dropins_installed` alone.

Hold release uses
`/opt/nvidia-build-lb/state/compromised-hold-release.json`, root:root 0600, JCS
UTF-8 with no final LF. It is created only after `complete` and contains exactly
`version:1`, a transaction UUID, the SHA-256 of the complete hold journal,
the exact installed updater-wrapper file tuple, the exact lock drop-in tuple,
both exact hold-drop-in tuples, both saved unit states, the current boot child
and its two runtime-mask states, the Hermes container before/target tuples, the exact initial Hermes
`active.json` bytes hash and `bound_generation_id`, the active generation
pointer hash, the newly issued downstream credential ID/digest version/digest,
and `phase` plus nullable closed failure code. Its phases are:

```text
HoldReleaseJournal={
 version:1,transaction_id:UUID,phase:HoldReleasePhase,
 hold_journal_sha256:LowerHex64,
 boot_inhibition:{boot_id:UUID,journal_sha256:LowerHex64},
wrapper:{path:"/usr/local/libexec/nvidia-build-lb-agent-apps-delayed-update",
          before:PathFileTuple|null,before_classification:"absent"|"managed_previous"|"user_drift",
          target:ManagedRegularFileTuple},
lock_dropin:{path:"/etc/systemd/system/agent-apps-delayed-update.service.d/nblb-cutover-lock.conf",
              before:PathFileTuple|null,before_classification:"absent"|"managed_previous"|"user_drift",
              target:ManagedRegularFileTuple},
 hold_dropins:[ManagedRegularFileTuple,ManagedRegularFileTuple],
remask:{unit:"nvidia-build-lb-compromised-hold-remask.service",
         fragment_before:HoldPathTuple|null,fragment_target:ManagedRegularFileTuple,
         wants_link_before:HoldPathTuple|null,wants_link_target:ManagedRegularFileTuple,
         boot_trigger:"multi-user.target",enabled_target:true},
 timer_before:{enabled:"enabled"|"disabled",active:"active"|"inactive",
               masked:Boolean,persistent:true},
 service_before:{enabled:"static",active:"active"|"inactive",masked:Boolean,
                 invocation_id:LowerHex32|null},
 updater_catchup:UpdaterCatchup,
 hermes:{id:LowerHex64,image:String,config_sha256:LowerHex64,
         restart_policy_before:"no",restart_policy_target:RestartPolicy,
         target_state:"running",post_updater:null|{
           id:LowerHex64,image:String,config_sha256:LowerHex64,
           compose_sha256:LowerHex64,restart_policy:RestartPolicy,
           state:"running"|"stopped",env:PathFileTuple,config:PathFileTuple},
         boot_epochs:[HermesBootEpoch],e2e:HermesE2ETxn,
         recovery_e2e:[HermesE2ETxn],
         reverify_receipt_sha256:LowerHex64|null},
 active_generation:{id:UUID,pointer_sha256:LowerHex64},
 hermes_binding:{active_json_sha256:LowerHex64,bound_generation_id:UUID,
                 credential_id:UUID,digest_version:"downstream_v2",
                 credential_digest:LowerHex64},
 failure:HoldReleaseFailure|null
}
PathFileTuple={kind:"regular",path:AbsolutePath,sha256:LowerHex64,uid:Decimal,gid:Decimal,
           mode:Octal4,device:Decimal,inode:Decimal,size:Decimal,nlink:1}
|{kind:"symlink",path:AbsolutePath,target:AbsolutePath,uid:Decimal,gid:Decimal,
  mode:Octal4,device:Decimal,inode:Decimal,size:Decimal,nlink:1}
ManagedRegularFileTuple={kind:"regular",path:AbsolutePath,sha256:LowerHex64,
                         uid:Decimal,gid:Decimal,mode:Octal4,device:Decimal,
                         inode:Decimal,size:Decimal,nlink:1}
The wrapper and lock-drop-in paths are protected by the same CAS rule as the
hold files. Before `wrapper_install_requested` or
`lock_dropin_install_requested`, release captures each exact path as
`absent|managed_previous|user_drift`; an absent path may be created, a tuple
byte/metadata-identical to a prior managed tuple may be adopted, and any other
existing path stops at `failed_attention` without overwrite. The classification
and tuple are persisted in the release journal. Removal/restoration accepts
only the exact target or managed-previous tuple, never a name-only unlink, and
the effective `ExecStart`/drop-in is read back after every daemon reload.
HoldReleaseFailure="hold_not_complete"|"binding_unproved"|"file_tuple_changed"|
                   "unit_tuple_changed"|"container_tuple_changed"|
                   "catchup_failed"|"hermes_reverify_failed"|
                   "side_effect_failed"|"readback_failed"|"tuple_unknown"
UpdaterCatchup=
 {required:false,state:"not_required",attempts:[],
  successful_attempt_ordinal:null,successful_updater_run_id:null,
  successful_journal_sha256:null}
|{required:true,state:"pending"|"running"|"success",
  attempts:[UpdaterCatchupAttempt],
  successful_attempt_ordinal:Decimal|null,
  successful_updater_run_id:UUID|null,
  successful_journal_sha256:LowerHex64|null}
UpdaterCatchupAttempt={
 ordinal:Decimal,state:"planned"|"hermes_release_requested"|
  "hermes_released"|"operations_release_requested"|"operations_released"|
  "start_requested"|"started"|"terminal_observed"|
  "operations_reacquire_requested"|"operations_reacquired"|
  "hermes_reacquire_requested"|"hermes_reacquired"|"run_reconciled",
 retry_authority:"initial"|"abandoned_prior"|"operator_repair",
 invocation_id:LowerHex32|null,updater_run_id:UUID|null,
 updater_run_journal_sha256:LowerHex64|null,
 result:null|
  {kind:"committed",child:{kind:"exit",code:0..255}|
                           {kind:"signal",number:1..64}}|
  {kind:"abandoned_committed"},
 operator_repair_receipt_sha256:LowerHex64|null
}
```

The hold-release `hermes` E2E fields use the same closed lineage contract as
section 13.1: `boot_epochs` starts with the current boot's ordinal zero,
`e2e` is the base `for_phase:"hold_release"` row, and `recovery_e2e` is empty
until a reboot or accepted same-boot updater/container replacement occurs.
Recovery ordinals start at one, are contiguous, and each row supersedes only
the immediately prior effective row through its canonical receipt. The
`reverify_receipt_sha256` is the latest verified terminal receipt digest, never
an alternative proof or a substitute for the row lineage.

Catchup nullability is closed. Required `pending` has no attempt or one planned
attempt and all success fields null; `running` has a nonempty contiguous
ordinal array and null success fields. Within an attempt, InvocationID is null
through `start_requested`, then nonnull; run ID/hash/result are null before
`terminal_observed` and all nonnull at `run_reconciled`. An
`operator_repair` row alone has a nonnull repair receipt, while `initial` and
`abandoned_prior` require null. `success` requires the three success fields to
name exactly one `run_reconciled` committed/exit-zero attempt and forbids later
attempts. No-op has exactly the empty branch shown.

```text
prepared|hermes_binding_verified|wrapper_install_requested|wrapper_installed|
lock_dropin_install_requested|lock_dropin_installed|daemon_reload_requested|
daemon_reloaded|service_unmask_requested|service_unmasked_blocked|
service_hold_dropin_remove_requested|service_hold_dropin_removed|
service_hold_reload_requested|service_hold_reloaded|
catchup_authorized|catchup_running|catchup_succeeded|
hermes_recovery_start_requested|hermes_recovery_started|hermes_reverified|
hermes_restart_policy_restore_requested|hermes_restart_policy_restored|
timer_hold_dropin_remove_requested|timer_hold_dropin_removed|
timer_unmask_requested|timer_unmasked|
timer_restore_reload_requested|timer_restore_reloaded|
prior_timer_restore_requested|prior_timer_restored|
remask_disable_requested|remask_disabled_verified|
restored|failed_attention
```

Every requested phase is durable before its one side effect and every completed
phase requires exact file, effective-unit, mask, container, and process
readback. `hermes_binding_verified` is impossible until the v3 production
generation is active, both fresh keys and all eight profiles are proved, the
new downstream token digest/scopes match the live Hermes `.env` and DB row,
`active.json.bound_generation_id` matches the active pointer, and the initial
Hermes health plus real task E2E have passed. Until that phase, both persistent
hold drop-ins, the absent unblock path, and timer disabled state remain
untouched; the current boot's `HoldBootInhibitionJournal` must also still prove
both rematerialized runtime masks. The legacy app restart policy is permanently `no` and is
never restored.

The saved service `active` bit is never restored with a direct start while the
release process holds Hermes. The service hold drop-in and runtime mask are
removed only after the lock wrapper is installed/read back; the timer remains
disabled, masked, and persistently inhibited. If the saved service was inactive,
`updater_catchup.required=false`, the attempts array is empty, and catchup
phases are verified no-ops. If it was active, the journal sets `required=true`,
adds ordinal 1 with `retry_authority:"initial"`, and enters
`catchup_authorized|catchup_running`. Each nested attempt durably releases
Hermes followed by operations in reverse lock order, starts the static service
once through systemd, observes its exact InvocationID, and reacquires operations
then Hermes through the attempt's requested/completed states. Every other root
entrypoint refuses the nonterminal hold-release journal; the exact UpdaterRun
reconciler and updater-run recovery command are the sole exceptions. The new
InvocationID must differ from every earlier attempt and the stopped hold
invocation; effective ExecStart must be the wrapper. `started` precedes waiting,
and response loss is reconciled only from that InvocationID. `run_reconciled`
requires the exact terminal UpdaterRun ID and journal SHA-256.

An exact `committed` UpdaterRun with child exit zero makes the catchup
`success`, copies that attempt's ordinal/run/hash to the three nonnull success
fields, and permits `catchup_succeeded`. A recovered
`abandoned_committed` run proves binding safety but explicitly does not prove
the updater action succeeded: after exact cgroup quiescence and lock reacquire,
the release appends a fresh ordinal with
`retry_authority:"abandoned_prior"` and repeats the nested sequence. This is a
new InvocationID and UpdaterRun, never resend/reuse of the abandoned one. A
known committed nonzero/signalled child leaves the timer inhibited and requires
a closed operator repair receipt bound to that attempt and the repaired unit/
runtime tuple before a new `operator_repair` attempt may be appended. No attempt
is added while its predecessor is nonterminal or before locks are reacquired.
Thus crash recovery may require more than one service start, but each service
invocation is exactly-once and only an exact later zero-exit committed run
satisfies the hold-release gate.

After successful catchup/no-op, it holds operations first and then Hermes and
revalidates that no unrelated generation/admin mutation changed the
journaled tuple (drift leaves the timer held and fails attention), and re-reads
the active generation/binding/DB token and effective Compose files,
and accepts only the exact journaled original target or a new container tuple
created by that successful updater invocation. The latter is stored once as
`post_updater`; unrelated containers or file drift fail. If the exact accepted
Hermes target is stopped— including after a host reboot while restart policy is
still `no`—`hermes_recovery_start_requested` precedes starting that exact target.
`hermes_reverified` requires health, token/binding/file tuple equality, and the
greatest-ordinal effective `recovery_e2e` (or the base `e2e` when no recovery
occurred) after the latest boot/updater invocation. Each reboot or accepted
same-boot updater/container replacement appends one contiguous recovery row
with `for_phase:"hold_release"` and a supersession receipt naming the prior
effective row; no prior receipt is rewritten. Only the latest row, bound to the
greatest boot epoch/current container and carrying all six receipts, is accepted
as the fresh real task proof. Only then is
the saved non-legacy Hermes restart policy restored. Finally the timer hold
drop-in/mask is removed and its saved enabled plus active state is restored;
then `remask_disable_requested` durably precedes `systemctl disable` and
removal of the exact `multi-user.target.wants` link, a daemon-reload, and
`remask_disabled_verified` readback proving `is-enabled=disabled`, link absence,
and no effective boot trigger. A crash before that phase leaves the link
enabled and remasking fail-closed; a crash at the requested phase repeats only
the exact disable/readback. The static service is left inactive. A Persistent
catch-up after this point is safe because every future invocation uses the
wrapper.

A crash before `hermes_reverified` keeps the timer inhibited and replays only
the exact InvocationID/container branch above; it never trusts the earlier E2E.
A crash before restart-policy restoration may start and reverify only the exact
stopped target. A crash after timer restoration may only finish exact readback.
No phase restarts the legacy app or reuses a revoked credential.

Each daemon reload has its own write-ahead pair. The original
`daemon_reload_requested|daemon_reloaded` installs the wrapper/lock drop-in.
After service hold-drop-in removal,
`service_hold_reload_requested|service_hold_reloaded` surrounds a distinct
daemon-reload and proves the effective service has the lock wrapper, lacks only
that hold condition, remains timer-isolated, and has no process before catch-up.
After timer hold-drop-in removal and unmask,
`timer_restore_reload_requested|timer_restore_reloaded` surrounds the final
daemon-reload and proves the reviewed timer/service effective definitions,
Persistent value, wrapper ExecStart, masks/drop-ins, and zero service PID before
restoring the saved timer state. A crash repeats only the reload whose requested
phase is durable; no completed phase is reused for another manager reload.

The only pre-journal operations are `mkdir` of the absent 0700 state directory
and `O_CREAT|O_EXCL|O_NOFOLLOW` creation of absent zero-byte 0600 `/run/lock`
files. A crash leaves either absent or the exact valid object; rerun validates
and adopts it, and neither object is ever rolled back. No service/container/file
content change occurs before `prepared` is durable.

If the two `/run/lock` files are not yet installed at this preimplementation
gate, a root one-shot bootstrap creates each with exclusive-create semantics as
root:root 0600, validates regular/no-symlink/nlink-one, and thereafter never
unlinks or truncates it. The later tmpfiles unit adopts the same inode contract;
it does not replace an existing valid lock. The already observed
`/opt/nvidia-build-lb` parent is preserved; bootstrap creates `state` as
root:root 0700 if absent or validates that exact safe metadata if present. The
already observed
`runtime.env.lock` must be root-owned, regular, nonsymlink, nlink one, and mode
0644 before it is acquired with the legacy shared/exclusive `flock` protocol;
the hold does not create `/etc/nvidia-build-lb/.lock`.

## 2. Product axis, outcome, and non-goals

The product axis is **judgment -> action -> evidence**. Every owner screen says:
current state, what it means, one safest next action, and the confirmed result.
Raw counters, IDs, timestamps, and events never precede that answer. Within ten
seconds of authentication an operator must know whether traffic can flow, which
profiles are usable, whether exactly two slots are configured, what needs
attention, and what action is safe now.

The product is one NVIDIA hosted-API load balancer. It provides:

1. exactly two permanent routing slots with profile-local round-robin,
   cooldown, bounded sequential failover, and restart persistence;
2. OpenAI-compatible model, chat, embedding, image-generation, and speech
   routes plus one explicitly NVIDIA-native video inference extension;
3. all modality kinds available through the selected hosted NVIDIA surfaces:
   text, image, audio, and video inputs; text, vector, image, audio, and video
   outputs;
4. encrypted upstream-key custody, one-time downstream credentials, scoped
   authorization, PostgreSQL durability, backup, restore, and generation
   rollback;
5. a local-only Svelte owner console with responsive, accessible, explicit
   loading/empty/stale/degraded/error/recovery states;
6. a public API at `nvidia-lb.dongwontuna.net` through the existing Cloudflare
   tunnel while owner UI and admin API stay loopback-only;
7. Hermes using only `models:read` and `chat:write` with `z-ai/glm-5.2`; and
8. unchanged availability and ownership of `codex-lb` at port 2455.

This is not a general provider framework. It has no non-NVIDIA upstream,
OAuth/account bridge, speculative quota model, automatic provider discovery,
arbitrary URL proxy, public owner UI, browser CORS API, online vault-key
rotation, OpenAI Responses API, OpenAI-compatible video claim, or future-
provider abstraction. Provider-limit bypass and credential sharing are
forbidden.

## 3. Monorepo, toolchain, and dependency direction

The final repository topology is:

```text
apps/
  gateway/                    Rust binary and process composition
  admin/                      SvelteKit 2 / Svelte 5 prerendered owner console
crates/
  nblb-domain/                pure IDs, states, scheduler, terminal precedence
  nblb-contracts/             public/admin Serde DTOs and error contracts
  nblb-ports/                 async store, vault, upstream, clock, entropy ports
  nblb-vault/                 foundational secret types and AEAD implementation
  nblb-store/                 SQLx repositories, locks, migrations, projections
  nblb-h1/                    safe raw DNS/TLS/HTTP/1 serializer and decoder
  nblb-nvidia/                closed profile registry and HTTP transport
  nblb-service/               routing, supervision, admin use cases
  nblb-http/                  Actix Web boundary, auth, routes, embedded assets
packages/
  admin-contracts/            generated TS types, pure validators, JSON Schemas
  design-tokens/              semantic CSS tokens
  ui/                         shared Svelte primitives
contracts/
  nvidia/                     sanitized official source snapshots and manifest
  openai/                     pinned public compatibility schema subsets
  oracle/                     framework-neutral request/state/response fixtures
migrations/                   forward-only SQLx PostgreSQL migrations
scripts/
  build/ qa/ ops/ release/    named Bun/Rust/shell entry points
```

`nblb-domain` depends only on the standard library and narrowly justified pure
libraries. `nblb-vault` depends on domain IDs but implements no port.
`nblb-contracts` depends on domain. `nblb-ports` depends on domain and vault
secret-capability types. Store and NVIDIA adapters depend on ports plus vault.
`nblb-h1` depends only on the pinned Tokio/rustls/httparse/WebPKI closure and
pure domain error types; `nblb-nvidia` depends on `nblb-h1`, never the reverse.
Store and NVIDIA do not depend on each other, Actix Web, or Svelte. Service depends on ports.
HTTP depends on service/contracts. Only `apps/gateway` knows concrete adapters.
The graph is checked in CI and cycles are forbidden.

The root is a Cargo and Bun workspace. `Cargo.lock`, `rust-toolchain.toml`,
`bun.lock`, root `package.json`, `biome.json`, `knip.json`, and strict shared
`tsconfig.json` are committed. Rust is pinned to `1.96.0` and Bun to `1.3.14`.
Svelte 5 and SvelteKit 2 exact versions are lockfile-owned. Bun is never a
production server. Every local Rust crate uses `#![forbid(unsafe_code)]`; local
unsafe has zero exceptions. Workspace lints deny warnings, panic/unwrap/expect
outside tests, unchecked wire/storage arithmetic, and undocumented public API.

The HTTP/runtime and PostgreSQL client choices are user-required and pinned,
not interchangeable framework placeholders. `actix-web` is exact `=4.14.0`
(official `https://actix.rs`, tag `web-v4.14.0` at
`696b1fed9c5b0147c37c70e0808cae5f63a5a4a0`, crate SHA-256
`df09e2d9239703dd64056359c920c7f3fba6535ec61a0059e0f44e095ffe02b4`)
with `default-features=false`; compression, cookies, HTTP/2, WebSocket, Unicode
route expansion, TLS listeners, and codegen macros are not enabled. `sqlx` is
exact `=0.9.0`, whose requested source authority is
`https://github.com/transact-rs/sqlx`. The crates.io archive, not the divergent
repository tag, is dependency authority; its `.cargo_vcs_info.json` records
commit `003b698e99e024f3621b8043a2426fde5b741171`, and its crate SHA-256 is
`378620ccc25c62c89d8be1c819e76a88d59bdcc3304733330788948e619bfd71`.
It uses `default-features=false` and exactly `runtime-tokio,postgres,macros,
migrate,uuid,time,json`; MySQL, SQLite, Any, native TLS, and Rustls are absent.
Actix Web owns the Tokio runtime and SQLx uses that same pinned runtime. Both
crate archives, their embedded VCS provenance where present, MIT/Apache license files, resolved feature
trees, and `Cargo.lock` checksums are byte-verified in dependency preparation
and candidate CI; a registry/git redirect or checksum/feature drift fails.

The upstream client is the local `nblb-h1` safe-Rust crate, not Actix/AWC,
Reqwest, Hyper, or a platform client. It pins `tokio =1.53.0` (`net,io-util,
time,sync`, crate SHA-256
`d988bcd52dbe076d3d46903332f58c912b87a2c49b1428419a5845154762ffee`),
`rustls =0.23.37` (`default-features=false`, exactly `std,ring,tls12`,
`758025cb5fccfd3bc2fd74708fd4682be41d99e5dff73c377c0646c6012c73a4`),
`tokio-rustls =0.26.4` (`default-features=false`, exactly `ring,tls12`,
`1729aa945f29d91ba541258c8df89027d5792d85a8841fb65e8bf0f4ede4ef61`),
`httparse =1.10.1` (`std`,
`6dbf3de79e51f3d586ab4cb9d5c3e2c14aa28ed23d180cf89b4df0454a69cc87`),
and `webpki-roots =1.0.9`
(`7dcd9d09a39985f5344844e66b0c530a33843579125f23e21e9f0f220850f22a`).
The final image therefore needs no drifting system CA bundle. All local parser,
serializer, chunk decoder, and transport code remains
`#![forbid(unsafe_code)]`; resolved transitive features/checksums are frozen.

Rust DTOs are the wire authority. A pinned deterministic Rust exporter emits
`types.ts`, closed JSON Schemas, and `validators.ts`. The validators are pure
TypeScript generated as explicit `typeof`, key-set, array, enum, and recursive
checks; they do not use `eval`, `Function`, dynamic code generation, Ajv, or a
runtime schema compiler. Regeneration to a temporary tree and byte comparison
fails drift across all three artifacts. Admin `u64`/`i64` counters and
configuration generations are canonical decimal strings matching
`0|[1-9][0-9]*`; TypeScript validates then parses them with `BigInt`, never
`Number`. Public OpenAI epoch and usage integers remain JSON numbers only where
the public contract requires them and must be within the exact safe range.

The source authority is the public repository
`https://github.com/DongwonTTuna-Labs/nvidia-build-lb`. Completion evidence uses
an unauthenticated GitHub REST read with `private:false,visibility:"public"` and
unauthenticated `git ls-remote`; a logged-in UI badge is insufficient. The app
OCI repository is exactly
`ghcr.io/dongwonttuna-labs/nvidia-build-lb`. `.github/workflows/publish.yml`
runs only for a user-merged `main` commit, checks out exact `github.sha`, builds
the locked application and PostgreSQL-derived production Dockerfiles, pushes
both tags `sha-<40-lowercase-git-sha>`, and records both registry-returned
manifest digests. The immutable runtime reference
type `OCIRef` is exactly
`ghcr.io/dongwonttuna-labs/nvidia-build-lb@sha256:<64-lowerhex>`.
The derived database image type `PostgresOCIRef` is exactly
`ghcr.io/dongwonttuna-labs/nvidia-build-lb-postgres@sha256:<64-lowerhex>`;
the same merged source commit publishes and attests both repositories. The
tunnel-only `CloudflaredOCIRef` is exactly
`cloudflare/cloudflared@sha256:<64-lowerhex>`.

Both published images label `org.opencontainers.image.source` with the public repository
URL and `org.opencontainers.image.revision` with that merged 40-hex commit. The
publish receipt binds commit, source-manifest hash, workflow run/attempt, both
image config digests, both multi-arch/index digests (linux/amd64 is mandatory), labels, and
build-provenance attestation. Before infrastructure pinning, both GitHub package
visibilities are public and unauthenticated registry token/manifest/config pulls
must reproduce each digest and labels. `latest`, branch tags, authenticated-only
pulls, locally built images, and PR-head digests are never deployment authority.

## 4. Runtime topology and trust boundaries

Production is an independent Compose project named `nvidia-build-lb`:

- `db-init`: a one-shot candidate-bound derived PostgreSQL image whose only
  base is
  `postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609`
  (the observed PostgreSQL 17.9 Alpine immutable digest); it initializes or
  validates the named volume and exits before steady DB start;
- `db`: the same immutable derived PostgreSQL image in steady `serve` mode,
  fixed UID/GID 70, data network only;
- `migrate`: the same immutable Rust application image in one-shot SQLx
  migration mode, data network only; and
- `app`: Rust gateway plus embedded Svelte assets, data network plus NVIDIA
  egress, published only as `127.0.0.1:2456:2456`, with Compose
  `restart: unless-stopped`; every automatic restart enters the runtime-epoch
  startup reconciliation before readiness or intake.

The database has no host port. The app has no Docker socket. Containers use
read-only roots, declared tmpfs only, `no-new-privileges`, dropped capabilities,
bounded health checks, and no Watchtower label. The final app image has no
Python, FastAPI, SQLAlchemy, Alembic, Node/Bun runtime server, or retired JS
concatenator. `codex-lb`, its port 2455, containers, database, networks, config,
and updater path are never changed.

Secret-readable startup and steady process identity are distinct and tested.
The app/migrate image entrypoint is a small local Rust `nblb-init` binary with
`#![forbid(unsafe_code)]`; it runs briefly as UID/GID 0 with only `CHOWN`,
`SETUID`, `SETGID`, and `SETPCAP`, validates the fixed root-only bind sources,
copies only the mode-required secret set into its private
`/run/nvidia-build-lb/secrets` tmpfs as UID/GID 65532 mode 0400, revalidates
bytes in secret memory, clears ambient and inheritable capabilities, and then,
while the four required effective capabilities still exist, drops every entry
from the capability bounding set. Bounding-set removal does not remove the
current effective set. It then clears supplementary groups, calls
`setresgid(65532,65532,65532)`, then
`setresuid(65532,65532,65532)`, explicitly writes empty permitted/effective/
inheritable/ambient sets, disables keep-caps, sets `no_new_privs`, verifies the
complete `/proc/self/status` identity, and only then `exec`s the same static
Rust image in the requested
gateway/migrate/healthcheck mode. These operations use only safe APIs from
pinned `rustix`/capability wrappers; local source remains unsafe-free and the
final scratch-derived app image contains no shell or `setpriv`. The
gateway/migrator PID 1 is UID/GID 65532 with empty groups, `CapEff=0`,
`CapBnd=0`, and `NoNewPrivs=1`; it cannot traverse `/run/canonical-secrets`.
Prestart failure prints only `prestart_failed` and exits 70.

PostgreSQL root initialization and steady service are deliberately separate.
`images/postgres/` contains an audited `#![forbid(unsafe_code)]`
`nblb-pg-bootstrap` plus the minimal derived-image recipe; the candidate
manifest binds the recipe, binary, base digest, and final derived image digest.
`db-init` alone runs as root with only the capabilities needed for fresh-volume
ownership and UID/GID transition. It accepts the fixed password secret through
the canonical read-only mount, uses PostgreSQL 17.9 `initdb --pwfile` without
environment/argv disclosure, installs the reviewed `pg_hba.conf` and database/
role bootstrap, fsyncs the volume and a closed bootstrap sentinel, changes the
complete tree to UID/GID 70, then exits. On an existing volume it performs only
sentinel/version/ownership/config/password-verifier validation and never
reinitializes or recursively rewrites unknown content. Restore uses a new empty
volume and its own new password generation. `db` depends on successful
`db-init`, starts directly as Compose `user:70:70` with `cap_drop:ALL`, a
read-only root, `no-new-privileges`, no password mount, and execs PostgreSQL
without the stock root entrypoint. Its PID 1 must have empty supplementary
groups, `CapEff=0`, `CapBnd=0`, and `NoNewPrivs=1`. Candidate tests inspect both
the bounded one-shot phase and steady `/proc/1/status`, readability
from expected and adjacent UIDs, fixed mount metadata, and the absence of any
secret in environment/argv/logs. “Dropped capabilities” means this proven
steady state; the bounded prestart capability set is never available to the
Rust service or steady PostgreSQL process.

The only canonical source targets are
`/run/canonical-secrets/db_password`,
`/run/canonical-secrets/admin_token`, and
`/run/canonical-secrets/vault_master_key`. App mode requires all three; migrate and
`db-init` modes require only the PostgreSQL password; steady DB receives no
secret mount. Unknown
files, symlinks, non-root ownership, nlink other than one, or modes other than
0400/0600 fail before copying. The tmpfs is 64 KiB, root-created 0700 before
copy, then owned 65532:65532; each restart removes only its three fixed stale
destinations before recreating them atomically, and never modifies the
canonical bind sources.

Each generation owns a 32-byte CSPRNG PostgreSQL password encoded as 43
unpadded base64url characters plus LF in
`/opt/nvidia-build-lb/secrets/generations/<generation-uuid>/postgres.password`,
root:root 0600 under 0700 parents. Compose mounts it read-only as a Docker secret;
`db-init` consumes the root-only canonical mount and section 8.1 derives the
three SCRAM passwords through anonymous FDs; the stock
`POSTGRES_PASSWORD|POSTGRES_PASSWORD_FILE` entrypoint contract is not used.
App/migrate prestart reads the generation secret as root, derives only its
mode-specific app or migrator password under section 8.1, atomically copies that
derived password alone into container-private tmpfs, and zeroizes the base plus
other intermediate bytes before dropping privilege. The Rust process cannot
derive the other role's password. It never
appears in Compose environment, argv, manifest, logs, health, evidence, or
`runtime.env`. Restore creates a new DB password along with its new DB volume.

Trust boundaries are public downstream API, loopback owner administration,
closed NVIDIA upstreams, PostgreSQL, and root-owned host secrets. Client request
IDs are ignored; the server creates an opaque ID and returns it in
`x-request-id`. Request bodies, prompts, media, generated output, URL queries,
Authorization, cookies, ciphertext, nonce, database URLs, provider bodies, and
secret file paths never enter logs or evidence.

### 4.1 Authority, route, method, Origin, and authentication order

The only production loopback authority is `127.0.0.1:2456`; the only public
authority is `nvidia-lb.dongwontuna.net`. An unpublished candidate accepts only
`127.0.0.1:12456` and exposes the loopback management/readiness surface while
public/downstream intake is hard-disabled. Before body consumption, any other
authority returns public `403 host_forbidden` for a public-surface path and the
closed `403 admin_access_forbidden` envelope for a recognizable owner path.
Public requests to any owner/admin/asset path return
404 before authentication. Unknown visible paths return 404. The server first
creates one opaque request ID without reading any request-controlled bytes
other than the already parsed transport metadata; therefore even authority,
visibility, and Origin failures can use their closed error envelope. For a
known path, the order is:

```text
server request ID -> authority -> visibility -> exact Origin policy ->
resource authentication/scope transaction -> method/Allow -> bounded body ->
schema/profile validation -> use case
```

Actix Web implements the route/use-case service boundary, but its default H1
dispatcher is not the raw-parser authority. `apps/gateway` owns a
`tokio::net::TcpListener` plus the pinned `nblb-h1` front parser; it accepts
each socket, applies the complete raw framing/authority policy below, and only
then invokes the Actix service adapter with a product-owned request stream.
There is no `actix_web::HttpServer::bind` or Actix H1 dispatcher on the
production listener, so Actix cannot enqueue an implicit `100 Continue`,
consume a malformed head before the product sees it, or emit a default parser
error. Four Actix worker services receive clones of the same `Arc` service graph,
SQLx pool, immutable route/asset registry, terminal supervisors, and drain
state. Production binds `0.0.0.0:2456` inside the container and candidate mode
`0.0.0.0:12456`; Compose alone publishes the corresponding host loopback port.
The front parser accepts HTTP/1.1 only, fixes a 10-second header deadline,
30-second graceful shutdown, five-second keep-alive, and 1,024 connections per
worker. Only HTTP/1.1
origin-form request targets, one canonical Host, a head of at most 64 KiB/128
fields, one decimal `Content-Length` or no framing, and no `Transfer-Encoding`
are accepted. HTTP/1.0, an HTTP/2 preface, absolute-form targets, repeated or
comma-joined Content-Length, CL+TE, malformed decimal grammar, and any
`Expect` value (including `100-continue`) produce the closed `invalid_request`
or `request_too_large` response from the front parser without Actix's default
parser/100 response. Parser failures are mapped before Actix service invocation
and body polling. The front parser sets
`Connection: close` and closes the H1 connection whenever the head/body was not
fully consumed; only a fully framed, fully drained body may use keep-alive.
Before allocating its 64 KiB head/framing buffer, the front parser reserves
bytes from the process-wide `BodyBudget` `head_reserve` (16 MiB total, at most
one 64 KiB charge per parsing connection). When that reserve is exhausted, a
socket remains in the bounded accept queue without a head allocation and is
closed with the fixed overload response if its header deadline expires; it is
never allowed to allocate outside the budget. The 1,024-connection-per-worker
limit therefore describes sockets, not simultaneous 64 KiB parser buffers.
After the front parser hands off a fully validated head, the adapter preserves
the raw request ID/framing result in extensions and forbids any second
Actix-level parse or implicit `Expect` handling.
Raw TCP fixtures cover split heads, pipelining, partial/oversized bodies,
duplicate framing, HTTP/2 preface, absolute-form, and Expect. Per-route
body/deadline supervision below remains authoritative after headers.

The outbound boundary is equally product-owned. The Actix service returns a
typed `WireResponse` to the gateway adapter; it never writes a
`ServiceResponse` or `MessageBody` directly to the socket. A single
`nblb-h1::Writer` owns the accepted socket after handoff, serializes the exact
status/reason/header allowlist, and writes body frames with checked byte counts,
flush/half-close, and keep-alive/close decisions. Actix is forbidden from
adding Date/Server/100/diagnostic headers or selecting a transfer coding. The
writer is the only owner of the connection after the handoff; a body Drop
signals delivery state but cannot write a second response. Its raw-wire fixture
binds `WireResponse`, socket bytes, request ID, EOF/keep-alive, and the exact
chunk serializer receipt.

The front-to-Actix boundary is a closed product adapter, not an implementation
hint. The gateway constructs exactly:

```text
FrontHead={raw_request_id:UUID,method:ProductMethod,raw_target:Bytes,
           canonical_host:Bytes,headers:RawHeaders,framing:Framing,
           peer:PeerTuple,route_id:RouteId}
ProductBody=Stream<Item=Result<Bytes,BodyError>> + Send + 'static
WireBody=Buffered(Bytes)
        |Streaming(FramePump)
FramePump=Stream<Item=Result<WireFrame,AdapterError>> + Send + 'static
WireResponse={head:WireHead,body:WireBody}
WorkerHandle::call(FrontHead,ProductBody)
  -> Future<Output=Result<WireResponse,AdapterError>>
```

`ProductBody` is backed only by the front parser's read-half and has one
consumer; its `poll_next` uses the caller waker, propagates backpressure, and
after `body_complete|abandoned` returns `None` without another socket read.
The adapter owns one pinned `ServiceFactory` per worker. It constructs one
`actix_http::Request` and `Payload` from `FrontHead`/`ProductBody`, creates one
`ServiceRequest`, calls the worker service once, and never reparses bytes.
`ServiceResponse` and `MessageBody` are adapter-private. For a buffered
response the adapter drains the framework body to one checked `Bytes` value
before resolving. For a streaming response it creates a bounded, adapter-owned
`FramePump`; each `poll_next` performs at most one `MessageBody` poll, maps the
frame to a checked `WireFrame`, and returns it to the product-owned
`nblb-h1::Writer` immediately. The `WorkerHandle::call` future resolves only
after the typed head and body/pump are initialized; the writer then polls the
`FramePump` incrementally under its one-frame backpressure rule until the pump
emits terminal `None`. The pump retains no socket, request, or Actix worker
handle after handoff and cannot write on its own. No framework body may retain
the socket or write after the adapter future resolves.
Each call has an adapter-owned cancellation token and lifecycle mutex; Drop of
the request, payload, response body, or worker handle is idempotent and records
exactly one pre-terminal adapter-call `abandoned` transition. After a durable
terminal seal, a body Drop is delivery-only and records the separate
`downstream_delivery_abandoned` receipt; it never rewrites the request terminal.
Before the writer has handed off response headers, either a buffered conversion
error or a streaming head/pump initialization error emits the fixed internal
`WireResponse` and closes that attempt. After a streaming head or any body
frame has crossed `downstream_handoff`, a `FramePump` `AdapterError` is a
post-start error: the writer emits exactly the closed SSE `event:error` frame
and terminator/close (or, for non-SSE, stops the body and closes) and never
emits a second response header. The request terminal remains the already
sealed outcome; only delivery evidence records the post-start failure.
Shutdown first stops new accepts, then
closes each worker's call channel, drains active adapter futures, joins all
four worker tasks, and only then closes the listener/writer. A worker join
error fails readiness and emits the fixed internal `WireResponse` only before
response handoff; after handoff it follows the post-start delivery rule above.
The adapter fixture fixes request construction, one-poll
backpressure, frame order, body Drop, worker cancellation, and shutdown/join
receipts against raw socket bytes; an Actix upgrade cannot alter this contract
without a new design snapshot.

The generated registry matches the raw URI path byte-for-byte before any Actix
path extraction. It contains every literal route and every manifest asset path;
the finite asset paths are registered individually. UUID routes accept only
literal ASCII slash plus canonical UUID segments and reject `%`, backslash,
empty/repeated segments, dot segments, or decoded aliases. There is no
`NormalizePath`, `Files`, wildcard asset service, Unicode pattern expansion, or
redirect. Each recognizable resource is registered as one unguarded
`web::resource(...).to(dispatch_known_resource)` so every method first reaches
the same resource dispatcher. That dispatcher performs the exact auth/scope
transaction and only then switches on the literal HTTP method to success or the
closed 405/Allow. App `default_service` handles only registry-unknown paths and
never authenticates them; reaching it is a fail-stop conformance sensor.

One custom Actix `Transform` receives only the front-parser handoff. It uses the
preserved raw path/Host bytes to create the server request ID, classify the
public/owner/unknown visibility, and apply Origin policy before calling the
Actix router; it never reparses the socket or reads payload. The raw registry
returns a closed
`RouteId` only for an exact literal or canonical UUID pattern. If it returns
unknown—including any `%`, encoded slash or unreserved byte, backslash,
repeated/dot segment, or invalid UUID—the outer Transform constructs the closed
visibility-appropriate 404 immediately and never calls the inner router. For a
known path it stores the `RouteId` in request extensions;
`dispatch_known_resource` requires the same ID and exact raw path before auth.
Actix's partially percent-decoded match value is never authority. Thus an
encoded path cannot reach a dynamic resource even if `actix-router` would
otherwise match it. There is no CORS, Logger,
Compress, ErrorHandlers, session/cookie, form, or JSON middleware. Known route
dispatchers consume `web::Payload` through the product's bounded duplicate-key
parser; `web::Json`, `web::Form`, automatic decompression, and framework body
limits/error pages are forbidden. Every handler returns a closed
`HttpResponse<TrackedBody>` from one dispatcher-owned future. Inside that
future, the use case first returns a closed `HttpOutcome`; the response mapper
then constructs status/headers and the tracked body before the future returns.
It strips any framework detail and maps internal failures to section 5.2.
`ServiceResponse` is created later by Actix and is never a handler-owned type.
Release uses
`panic=abort`, so an impossible panic restarts the unready container rather than
rendering an Actix debug/default response.

Framework conformance fixtures enumerate every route with correct/wrong method,
authority, visibility, Origin, auth, media type, duplicate header, oversized/
slow body, raw/encoded path, and handler failure. A call-order sensor asserts
the exact sequence above and zero payload polls before its body phase. Another
fixture builds all four worker Apps and proves shared scheduler/admission state,
one logical counter update, exact custom 404/405/error bytes, no implicit
compression/cookie/CORS/redirect/HTTP2, and no `Server` or framework diagnostic
header/body. Actix upgrades cannot change this oracle without a new design.

Public routes reject every request containing `Origin`; there is no CORS.
Production loopback admin accepts no Origin or exactly
`http://127.0.0.1:2456`; candidate admin accepts no Origin or exactly
`http://127.0.0.1:12456`. A wrong
method on an authenticated public resource authenticates the resource's scope
then returns 405 with the exact `Allow`; it cannot be used as an auth bypass.
`OPTIONS` follows the same rule. `/health`, `/admin`, `/showcase`, and exact
static assets are intentionally unauthenticated on loopback and return their
normal 405 for a wrong method. Unknown paths never authenticate.

`HEAD` has an explicit product-owned wire exception. It follows the same raw
route, authority, Origin, authentication/scope/counter, and method decision and
constructs the same canonical public/admin 404 or 405 JSON representation, but
the `WireResponse` carries `method:HEAD` and `body_suppressed:true`; the
`nblb-h1::Writer` suppresses body frames itself. The response retains exact status, `Allow`
when 405, JSON `Content-Type`, and `Content-Length` equal to the canonical
would-be representation; it has no `Transfer-Encoding` and sends zero body
octets. No route treats HEAD as GET. Raw TCP fixtures cover authenticated and
unauthenticated known/unknown public/admin paths and assert the ordering,
headers, length, and EOF rather than using only Actix's in-process body object.

Every public/admin route with a JSON body requires exactly one parseable
`Content-Type` whose media type is case-insensitive `application/json` and whose
only optional parameter is case-insensitive `charset=utf-8`. Missing, repeated,
comma-joined, malformed, or another parameter/type is 415 before body read.
Any `Content-Encoding` is likewise unsupported; clients send identity JSON.
Header validation occurs after the authenticated method decision and before the
bounded streaming duplicate-key JSON parser. Bodyless GETs require either an
explicit `Content-Length: 0` or no framing; no framing is treated as zero only
until the first byte after the head, and any such byte is an invalid pipelined
body, producing `invalid_request` and `Connection: close`. A nonzero length or
any `Transfer-Encoding` is rejected before body read and likewise closes the
connection. The parser never reuses a connection after a bodyless-route
framing violation, so pipelined bytes cannot be mistaken for a second request.

The pre-parse body ceilings are 32 MiB for chat and embeddings, 256 KiB for
images, 64 KiB for speech, 1 MiB for native video inference, and 64 KiB for
every admin mutation; a declared or streamed excess is 413. Public JSON nesting
is at most 32 and admin nesting at most 16. After model selection the narrower
GLM 2 MiB, media/base64/decoded, scalar, and per-field limits still apply; a
larger route ceiling never broadens a selected schema.

All four workers share a process-wide 128 MiB `BodyBudget` semaphore. Sixteen
MiB is reserved as `head_reserve` for raw-parser buffers and four MiB as a
fixed `error_reserve` for pre-admission/parse error responses; the remaining
108 MiB is available to request leases. A public request reserves
a checked `allocation_envelope_bytes` before the first body poll, not just the
wire ceiling: it is the sum of the declared/raw bound, the bounded
duplicate-key token/node pool, the maximum JSON value tree, every base64/media
decode buffer, and the largest in-memory serializer scratch for that route; the
complete media output is explicitly disk-spooled as described below. The exact
per-route envelope constants are generated with the schema manifest. Post-
admission parser, decoder, validator, and serializer allocations draw from the
request lease; pre-admission/parser errors draw from `error_reserve`, and raw
head/framing buffers draw from `head_reserve`. An allocation without the
corresponding charge is a test failure. A request reserves its envelope before the first body poll and returns
the fixed 503 `service_unavailable` plus `Connection: close` if no budget is
available; a streamed overrun returns 413 and closes. Every timeout, cancel,
parser error, and Drop releases the exact lease once. A budget lease never
counts as an upstream attempt and cannot be reconstructed from a worker-local
counter. The multi-worker fixture saturates this budget and proves bounded
memory, fairness, one release, and no provider/DB side effect for rejected
leases.

The generated v3 envelope constants are fixed in the schema manifest, not
runtime configuration:

```text
BodyBudgetEnvelope {
 chat: 48 MiB, embeddings: 48 MiB, images: 4 MiB,
 speech: 2 MiB, native_video: 96 MiB, admin: 1 MiB,
 head_reserve: 16 MiB, error_reserve: 4 MiB,
 process_total: 128 MiB
}
```

Each value includes raw request bytes, duplicate-key parser tokens and tree,
decoded input media buffers, validation scratch, and at most one bounded
serializer chunk; it does not include the complete output representation held
on disk. It is charged before the first body poll and held through terminal
seal plus downstream delivery; it is released exactly once at
`body_complete|abandoned` (or immediately at no-attempt terminalization when
there is no body). The writer-frame sublease is returned before that parent
lease release; route constants are independent
of the smaller wire/schema ceilings above. Raw-parser head/framing allocations
draw only from `head_reserve`; that charge is released exactly once after the
validated head is copied into the product-owned request object, or when the
connection closes on a parser error. Pre-admission authority/auth errors and
parser failures reserve one 8 KiB token from `error_reserve`; the token is
released immediately after the fixed error bytes are handed to the wire writer
(or on writer failure), with an atomic once-guard. Post-admission parser,
decoder, validator, and serializer allocations draw only from the request
lease; a writer frame is a checked sublease of that same request lease capped at
1 MiB and is never additive to the 128 MiB process total. An allocation without a
`head_reserve`, `error_reserve`, request-lease, or writer-frame charge is a test
failure. A fixture charges every route constant and error token, forces one
parser/tree/decode/serializer allocation, rejects an uncharged allocation and
any constant above the 108 MiB body capacity, and proves bounded memory under
1,024 concurrent malformed/auth requests plus one-release races on head
handoff/error, future Drop, body Drop, timeout, cancellation, parser error, and
streamed overrun.

Media responses never require a full in-memory representation. Compose declares
the app-visible `/run/nvidia-build-lb/media-spool` tmpfs with a fixed 256 MiB
size, owner `root:65532`, mode `0730`, and no device/setuid bits; `nblb-init`
validates the mount and does not mount filesystems or require `CAP_SYS_ADMIN`.
The gateway may write only beneath this parent. For each attempt it creates an
`attempt-<lowercase-canonical-uuid>` child directory owned `65532:65532`, mode
`0700`, with no symlink and nlink one. Its only legal children are regular
files named `output-<ordinal>.part` or `output-<ordinal>.sealed`, where
`ordinal` is a zero-based decimal `0..9`; one basename per ordinal is allowed,
`.part` is allowed only while that attempt is nonterminal, and `.sealed` is
immutable after its hash/size receipt. Hidden files, any other basename,
subdirectory, symlink, hard link, or duplicate ordinal is an unknown spool
entry and fails readiness closed. Output is written to a same-directory
`output-<ordinal>.part` regular file (0600, nlink one), fsynced, then atomically
renamed to `output-<ordinal>.sealed` and the directory is fsynced. A
transactional spool reservation enforces a
192 MiB process aggregate, a maximum of 32 concurrent attempt directories, and
the per-output quota; reservation occurs before any upstream bytes. ENOSPC or
quota exhaustion maps to the closed `response_too_large` terminal class;
cleanup failure before terminal seal maps to the closed `application_failure`
terminal and keeps readiness closed; cleanup failure after a durable terminal
seal never rewrites the public result, instead records one
`asset_cleanup_pending` attention and keeps readiness closed until the worker
reconciles it. These internal reasons never become an alternate or partial
success. Startup scans only the
exact attempt-directory grammar before readiness, reconciles each orphan with
its DB attempt under the epoch lock, and removes it after a durable abandoned
receipt; an unremovable or tuple-drifted orphan keeps readiness closed. The
spool is never included in backups or public/admin responses, and every restart
receipt records aggregate bytes, directory count, each sealed-file
`{attempt_id,ordinal,size,sha256}`, an `unknown_entries` count (which must be
zero), and zero remaining orphan bytes. An orphan is closed only when its
directory name, file grammar, and attempt UUID match a durable DB attempt; the
worker writes an abandoned receipt, fsyncs the parent, and removes only that
exact directory. Any grammar violation, tuple drift, or unremovable entry
keeps readiness closed. FLUX JPEG is capped at
24,117,248 decoded bytes, Magpie WAV at 26,460,044 bytes (44,100 Hz, mono,
16-bit, 300 seconds plus the 44-byte header), and SVD MP4 at 128 MiB. The
response writer computes checked `Content-Length`/base64 expansion first, then
streams JSON base64 or WAV/MP4 bytes through at most one 1 MiB charged chunk;
no serializer builds the complete output string. Spool exhaustion is the
deterministic `response_too_large` terminal and is never an alternate
or partial success. The media spool directory is removed only after terminal
delivery/cleanup evidence is durable.

Payload time is bounded independently of Actix's request-head timeout. The
timer starts immediately before the first `web::Payload` poll. Chat/embeddings
have a 120-second total and 10-second idle deadline; images/speech/native video
have a 30-second total and 5-second idle deadline; admin mutations have a
15-second total and 5-second idle deadline. Idle time resets only after a
nonempty payload chunk; empty/pending polls do not. Total time never resets.
Expiry aborts and drains no more payload, zeroizes partial buffers, and occurs
before upstream/provider or mutation transaction dispatch. Public uses the
fixed 408 `request_body_timeout`; admin uses its 504 `request_timeout` and
leaves the exact intent prepared because execution/fingerprint commit never
began. Declared-length rejection still wins before polling. Fake-clock fixtures
cover first-byte stall, mid-body idle, total trickle, boundary equality,
cancellation, and zero provider/DB side effects.

Downstream tokens are `nblb_ds_` plus the unpadded base64url encoding of 32
CSPRNG bytes. Admin tokens are `nblb_admin_` plus the same payload. Exactly one
`Authorization` header with case-sensitive `Bearer ` and an exact token shape is
accepted; comma-joined, repeated, OWS-padded, control-bearing, or overlong values
are rejected. Comparisons are constant-time. Unknown, malformed, and revoked
downstream tokens share the same 401 response and
`WWW-Authenticate: Bearer realm="nvidia-build-lb"`. Insufficient scope is 403
with the same realm, `error="insufficient_scope"`, and the one required scope.
Admin auth uses the closed admin envelope and never accepts a downstream token.
A downstream credential request counter commits once after auth/scope success
and before method/body handling.

An upstream NVIDIA credential is an opaque ASCII value matching
`nvapi-[A-Za-z0-9_-]{32,192}` and at most 198 bytes. It is accepted only in the
write-only admin field, is rejected before persistence if it has whitespace,
control bytes, non-ASCII, or the wrong prefix/length, and is never normalized.
Human labels are valid UTF-8, normalized by the server to NFC, then required to
contain 1..128 Unicode scalars; only the normalized value is stored. They are
not secrets but are compacted only for L1 display.

## 5. Exact public and owner HTTP surface

| Method and route | Visibility | Required scope | Success media/result |
| --- | --- | --- | --- |
| `GET /health` | loopback + public | none | minimal JSON ready/degraded |
| `GET /v1/models` | loopback + public | `models:read` | OpenAI model list JSON |
| `POST /v1/chat/completions` | loopback + public | `chat:write` | OpenAI/NVIDIA JSON or SSE |
| `POST /v1/embeddings` | loopback + public | `embeddings:write` | OpenAI embedding JSON |
| `POST /v1/images/generations` | loopback + public | `images:write` | OpenAI image JSON |
| `POST /v1/videos/generations` | loopback + public | `media:write` | OpenAI-normalized video JSON |
| `POST /v1/audio/speech` | loopback + public | `audio:write` | `audio/wav` bytes |
| `POST /v1/audio/transcriptions` | loopback + public | `audio:write` | OpenAI transcription JSON |
| `POST /v1/nvidia/inference` | loopback + public | `media:write` | NVIDIA-native video JSON |
| `GET /admin` | loopback only | none | prerendered login shell |
| `GET /showcase` | loopback only | none | real UI primitive showcase |
| `GET /assets/<manifest-entry>` | loopback only | none | exact embedded asset |
| `/admin/api/v1/*` | loopback only | admin bearer | closed admin JSON |

`ProfileId` is the closed eight-value enum
`"z-ai/glm-5.2"|"microsoft/phi-4-multimodal-instruct"|"nvidia/vila"|"nvidia/nvclip"|"black-forest-labs/flux.1-kontext-dev"|"stabilityai/stable-video-diffusion"|"nvidia/magpie-tts-multilingual"|"nvidia/parakeet-ctc-1.1b"`.
The downstream scope allowlist is exactly `models:read`, `chat:write`,
`embeddings:write`, `images:write`, `audio:write`, and `media:write`. Hermes gets
only the first two. `/v1/models` returns `{object:"list",data:[...]}` and only
advertised profiles in this exact order: `z-ai/glm-5.2`,
`microsoft/phi-4-multimodal-instruct`, `nvidia/vila`, `nvidia/nvclip`,
`black-forest-labs/flux.1-kontext-dev`,
`stabilityai/stable-video-diffusion`, and
`nvidia/magpie-tts-multilingual`, and `nvidia/parakeet-ctc-1.1b`. Every object has exact `id`,
`object:"model"`, `created:1784332800` (2026-07-18 00:00:00 UTC), and
`owned_by:"nvidia"`; temporary eligibility never reorders the surviving rows.
The database CHECK calls the immutable helper
`nblb_is_canonical_scopes(text[])`, whose only accepted values are nonempty
subsets of that six-item list, with duplicates rejected and the stored array
equal to the fixed allowlist order. The helper is implemented without a
subquery in the CHECK expression and is covered by the SQLx scope fixture.

Health has no detail oracle. Ready is exactly HTTP 200
`{"status":"ok","ready":true}`; every dependency/configuration/drain failure is
HTTP 503 `{"status":"degraded","ready":false}`. Both have
`Cache-Control: no-store`; public and loopback shapes are identical. In
production, ready is exactly the `traffic_ready` structural predicate defined
below; temporary per-request eligibility does not flap health.

### 5.1 Frozen OpenAI compatibility subset

The source manifest is the exact UTF-8 returned on 2026-07-18 by the official
OpenAI OpenAPI endpoint tool for each URL. All report OpenAPI 3.1.0 and
`info.version=2.3.0`; raw bytes, not reparsed JSON, own the digest. The selected
request and response `$ref` values are frozen even though the per-endpoint
extract names an external component.

| Official endpoint snapshot | Bytes | SHA-256 | Selected schema references |
| --- | ---: | --- | --- |
| `https://api.openai.com/v1/chat/completions` | 40,938 | `0a320dbb317ec09b53b01f7f111bc0dba59c2cfab69e1b83b5210811692de029` | `#/components/schemas/CreateChatCompletionRequest`, `CreateChatCompletionResponse`, `CreateChatCompletionStreamResponse` |
| `https://api.openai.com/v1/embeddings` | 5,625 | `b4e64b103cd60418432f20ed2dce5b3aa29c9ac4109aa1f7118556e5bd15ee1f` | `#/components/schemas/CreateEmbeddingRequest`, `CreateEmbeddingResponse` |
| `https://api.openai.com/v1/models` | 3,982 | `8cb9f082ef79e4334f8f956df3a7888a58810b574801216e7b1f2422a3074d7f` | `#/components/schemas/ListModelsResponse` |
| `https://api.openai.com/v1/images/generations` | 7,826 | `90b2933b17e882aa488d415145bd9d852341037fb681e0e33ef589ef7ba3c865` | `#/components/schemas/CreateImageRequest`, `ImagesResponse`; streaming is excluded |
| `https://api.openai.com/v1/audio/speech` | 8,478 | `8ac167055dff0db2b33a996492aeeb52e5807c7b5db3904103769f4f16f4f6ea` | `#/components/schemas/CreateSpeechRequest`; binary response only |

The endpoint tool emits server-relative path keys, not the `/v1` URL prefix.
The five exact keys are `/chat/completions`, `/embeddings`, `/models`,
`/images/generations`, and `/audio/speech`. Chat, embeddings, images, and audio
request pointers are exactly
`/paths/<RFC6901-escaped emitted path>/post/requestBody/content/application~1json/schema/$ref`.
Chat JSON/SSE, embeddings JSON, and images JSON use their corresponding exact
200 media-type response pointer; the images SSE reference is intentionally not
selected. Models uses the `/models` GET JSON success pointer. The audio
snapshot is request-shape evidence only: its official extract exposes a
`text/event-stream` response reference, while this product's NVIDIA Magpie
route returns the separately frozen `audio/wav` contract and makes no OpenAI
binary-response `$ref` claim. Missing/broken references, an accidental `/v1`
path-key insertion, or byte drift fail contract generation; no tool-time
“latest” substitution exists.

Those sources are compatibility evidence; the narrower table below is the wire
authority. GLM accepts exactly these top-level members and no others:

| Field | Closed GLM request contract |
| --- | --- |
| `model` | required exact `z-ai/glm-5.2` |
| `messages` | required array of 1..1,024 closed messages; complete JSON body remains at most 2 MiB |
| `stream` | Boolean, default `false` |
| `max_tokens`, `max_completion_tokens` | absent or integer 1..8,192; mutually exclusive; either maps to upstream `max_tokens` |
| `temperature` | absent or finite JSON number 0..2 |
| `top_p` | absent or finite JSON number 0..1 |
| `stop` | absent, null, one 1..256-scalar string, or 1..4 such unique strings |
| `seed` | absent or integer in `[-9007199254740991,9007199254740991]` |
| `n` | absent or exact integer `1`; it is removed before upstream dispatch |
| `user` | absent or 1..256 scalars; it is neither forwarded nor stored |
| `tools` | absent or 1..128 OpenAI function tools as defined below |
| `tool_choice` | absent, `none`, `auto`, `required`, or exact `{type:"function",function:{name}}`; the named function must exist in `tools` |
| `response_format` | absent, `{type:"text"}`, or `{type:"json_object"}` |
| `chat_template_kwargs` | absent or exactly one of `{enable_thinking:true,clear_thinking:false}` and `{enable_thinking:false,clear_thinking:true}` |

A message has no unknown members. `system` and `user` require nonempty string
`content`; `assistant` requires string or null `content` and may carry
`tool_calls`; `tool` requires string `content` and a 1..128 ASCII
`tool_call_id`. Optional `name` matches `[A-Za-z0-9_-]{1,64}`. An assistant
message with null content must have at least one tool call. A function tool is
exactly `{type:"function",function:{name,description?,parameters}}`: `name`
uses the same regex, description is at most 1,024 scalars, and `parameters` is
a JSON-Schema object at most 64 KiB and depth 16 with no remote `$ref`. A tool
list has unique names. A tool call is exactly
`{id,type:"function",function:{name,arguments}}`; `id` matches
`[A-Za-z0-9_-]{1,128}` and is unique in the conversation, `name` exists in the
request's tools, and `arguments` is a UTF-8 string of at most 64 KiB containing
one complete duplicate-key-free JSON value at depth at most 16. Each `tool`
message references exactly one unmatched prior assistant call ID, each ID has
at most one result, and every call from an assistant tool-call turn is matched
before the next non-tool message. A tool message's optional `name`, when
present, equals that call's function name. Phi and VILA instead accept only
`model,messages,max_tokens,temperature,top_p,seed,stream`; their typed content
and bounds are section 6.2, and every GLM-only member is 422.

For Phi, absent `max_tokens,temperature,top_p,stream` become
`512,0.1,0.7,false`; explicit values are respectively integer 1..8,192, finite
0..2, finite `(0,1]`, and Boolean. For VILA the defaults are
`1024,0.2,0.7,false` and ranges are integer 1..2,048, finite 0..1, finite
`(0,1]`, and Boolean; gateway always sends `num_frames_per_inference:8` and does
not expose it. Both accept absent or JSON-safe integer `seed` (VILA default 50;
Phi omits it when absent). Null, frequency/presence penalties, stop, tools, and
unknown members are rejected by this narrower product contract.

A nonstream chat success is exactly `id`, `object:"chat.completion"`,
`created`, `model`, exactly one `choices` row at index zero, required safe-integer
`usage`, and optional string/null `system_fingerprint`. Each choice has only
`index`, one closed assistant `message`, and the selected profile manifest's
closed string/null `finish_reason`; a value outside that frozen enum is never a
generic string success.
Allowlisted assistant output is `role:"assistant"`, string/null `content`,
optional string/null `reasoning_content`, and optional function `tool_calls`.
Every returned tool-call ID is unique and disjoint from every tool-call ID in
the complete request history; every returned function name exists in the
request `tools`, and its arguments satisfy the same bounded complete-JSON rule.
A nonempty returned `tool_calls` array requires exact
`finish_reason:"tool_calls"`; that finish reason requires a nonempty array.
Every other finish reason requires no returned tool call. This applies before
the typed response is eligible for success.
An SSE JSON frame is the corresponding closed `chat.completion.chunk` shape
with closed `delta`; exactly one terminal `[DONE]` follows a committed success.
Unknown provider fields, invalid indices, unsafe integers, a tool result without
a prior ID, or invalid JSON/SSE is `upstream_protocol_error`, never pass-through.

NVCLIP embeddings accept exactly `model:"nvidia/nvclip"`, required `input` as
one string or an array of 1..64 strings, optional
`encoding_format:"float"`, and optional 1..256-scalar `user` that is removed.
Token arrays, `base64`, `dimensions`, empty input, and unknown members are 422.
The response is exactly `{object:"list",data,model,usage}`: each `data` row is
`{object:"embedding",index,embedding}` with contiguous zero-based indices and a
vector of exactly 1,024 finite values, each convertible to finite IEEE-754
binary32 by the defined round-to-nearest conversion without
overflow; `usage` contains only nonnegative safe-integer
`prompt_tokens,total_tokens`. Model, row count, and indices must match the
request or the upstream response is rejected.

`/v1/images/generations` accepts the OpenAI-compatible subset: exact model,
`prompt` 1..10,000 Unicode scalars, absent or `n:1`, absent or
`size:"1024x1024"`, absent or `response_format:"b64_json"`, absent or
`quality:"standard"`, and optional `user` up to 256 scalars which is neither
forwarded nor stored. The one size maps to FLUX `width:1024`, `height:1024`,
`aspect_ratio:"1:1"`, `image:null`, and `samples:1`. `stream`, partial images,
edits, URLs, and unknown fields are rejected. It maps the successful FLUX
artifact to `{created:<response-completion Unix seconds>,data:[{b64_json:<jpeg>}]}`.

`/v1/audio/speech` accepts exact model, `input` whose normalized form is
1..2,000 Unicode scalars, and a `voice` from the intersection of both keys'
current Magpie voice probes. Normalization is exact: validate UTF-8, NFC,
convert CRLF/CR to LF, reject C0/C1 controls except TAB/LF/U+0085, replace every
maximal Unicode `White_Space` run with one U+0020, then trim U+0020 and count
Unicode scalar values. Provider-side text normalization may expand the text;
an upstream 400 identified only by the selected TTS endpoint after this local
check is returned as 422 `invalid_request` without health penalty or failover.
Absent or
`response_format:"wav"`, absent or `speed:1.0`, and absent `instructions` are
accepted; any other format, speed, instructions, stream format, or unknown
field is 422. Voice metadata supplies NVIDIA `language`; upstream encoding is
`LINEAR_PCM` and sample rate is 44,100 Hz. The response is WAV and never
pretends to be MP3.

For this rule `White_Space` is the exact closed set U+0009..000D, U+0020,
U+0085, U+00A0, U+1680, U+2000..200A, U+2028, U+2029, U+202F, U+205F, and
U+3000; a runtime Unicode-version change cannot alter it.

`/v1/nvidia/inference` remains the canonical NVIDIA-native video adapter. The
gateway also exposes `/v1/videos/generations` as a strict OpenAI-compatible
alias for the same `stabilityai/stable-video-diffusion` profile. The alias
accepts only the closed `model,input_reference,seed,cfg_scale,motion_bucket_id`
shape below, translates `input_reference` to the native `input.image`, and
normalizes a successful native response to `{created,data:[{b64_json}],model}`.
It does not advertise any additional model or provider capability; both routes
share the same validation, timeout, failover, and response-contract rules.

The native route accepts only:

```json
{
  "model": "stabilityai/stable-video-diffusion",
  "input": {
    "image": "data:image/png;base64,...",
    "seed": 0,
    "cfg_scale": 1.8,
    "motion_bucket_id": 127
  }
}
```

The outer object has exactly `model,input`. `input` has required `image` and
optional `seed,cfg_scale,motion_bucket_id`, with no other member. `image` is one
canonical PNG/JPEG data URL under section 6.2; the adapter removes only its
validated prefix and forwards the canonical padded base64 payload. Absent
`seed` becomes integer `0`; an explicit seed is an integer satisfying
`0 <= seed < 4294967296`. Absent `cfg_scale` becomes exact JSON number `1.8`;
an explicit value is finite and satisfies `1 < cfg_scale <= 9`. Absent
`motion_bucket_id` becomes integer `127`, and the only explicit value is exact
integer `127`. Boolean, null, exponent-overflow, and numeric-string values never
coerce. Thus the normalized NVIDIA request has exactly `image,seed,cfg_scale,
motion_bucket_id` in that schema order.

The successful upstream object has exactly
`{video:String,finish_reason:"SUCCESS",seed:Integer}`. `seed` must equal the
normalized request seed and `video` is canonical padded base64 for the single
validated MP4, without a data-URL prefix. The gateway returns those exact three
semantic fields in order through typed canonical serialization. It performs no
OpenAI conversion and never passes through provider bytes. An upstream object
with a missing/extra member, different seed, unknown finish reason, or artifact
on `ERROR|CONTENT_FILTERED` is protocol error; valid `ERROR` and
`CONTENT_FILTERED` use section 5.2 and never return an artifact.

The closed owner API read surface is `GET /overview`, `/upstream-slots`,
`/downstream-credentials`, `/model-capabilities`, `/generation-readiness`,
the paginated `/operations`, `/operations/<id>`, and
`/attentions?before=<opaque>&limit=1..100`,
`/events?before=<opaque>&limit=1..100`, `/events/<id>`, and
`/evidence/<id>` under
`/admin/api/v1`. `generation-readiness` uses ordinary admin bearer
authentication and returns exactly
`{generation_id,tuple_matches,schema_ready,database_ready,vault_ready,
management_ready,traffic_ready}`, with the UUID and six Booleans; it performs
no mutation or provider call. `management_ready` is exactly tuple, schema, DB,
vault binding/decrypt-all, admin-token, and embedded-asset-manifest readiness,
independent of key count. `traffic_ready` is management readiness plus a
configured two-key pair, all eight current proof/advertisement contracts, and
no intake drain; it deliberately ignores temporary cooldown, in-flight
capacity, and manual disable, which are exposed separately as current
eligibility. Secret-free row
DTOs expose UUID, compact/full label in its allowed layer, short fingerprint,
lifecycle/slot/enabled/profile states, decimal-string counters/generation, and
RFC 3339 UTC timestamps. Downstream mutation is
`POST /downstream-credentials` with
`{operation_id,label,scopes,expected_configuration_generation}` returning
plaintext exactly once, and `POST /downstream-credentials/<id>/revoke` with
operation ID and expected generation.
No token/key read endpoint exists. Upstream mutation routes are section 7.1.

### 5.2 Public and admin error contracts

Every public JSON error is `application/json` with this closed OpenAI shape:

```json
{"error":{"message":"safe fixed text","type":"stable type","param":null,"code":"stable_code"}}
```

Every public message is the exact fixed English text below. Provider error
bodies have an empty classification allowlist. A status is body-absence-proved
for alternate only when parsed headers contain exactly one
`Content-Length: 0`, no `Transfer-Encoding`, and `Content-Encoding` is either
absent or exactly one field whose OWS-trimmed value is ASCII-case-insensitive
`identity`, and the application has not yielded a body item. Repeated fields,
comma-joined values (including `identity, identity`), an empty field, or any
other coding fail this proof and are protocol error with zero alternate. The
dedicated H1 connection is then closed without polling the body, regardless of
whether the kernel/parser read buffer also contains later bytes. Every other
framing is body-possible and forbids status-based alternate; for a terminal
status, at most 64 KiB is read then discarded. No error body is parsed to change
key state, relayed, logged, or retained. Thus 401 alone is credential-global and
every 403 is profile-local; no undocumented body code can cross the replay
boundary.

Successful downstream wire responses have one closed header matrix. Buffered
JSON (`/health`, models, nonstream chat/embeddings/images, and native video
metadata) uses exactly one
`Content-Type: application/json; charset=utf-8`, an exact `Content-Length`, no
`Transfer-Encoding` or `Content-Encoding`, `Cache-Control: no-store`,
`Connection: keep-alive`, and the opaque `x-request-id` matching the server
request ID. Speech WAV and any media download body use their fixed media type,
exact length, no encoding, `Cache-Control: no-store`, and the same
connection/request headers; the native video inference route itself returns
the typed JSON object, not an MP4 body. SSE uses exactly
`Content-Type: text/event-stream`,
`Cache-Control: no-cache`, `Transfer-Encoding: chunked`, no Content-Length or
Content-Encoding, `Connection: keep-alive`, and the same request ID; it emits no
application event bytes after `[DONE]`, then emits only the mandatory HTTP
chunked terminator `0\r\n\r\n`, and closes on a terminal error according to the
SSE contract. The custom response serializer explicitly suppresses framework
`Date`, `Server`, compression, and diagnostic defaults; `Date` is absent from
every success/error allowlist. No success may inherit Actix defaults. A client disconnect before a
complete response follows the closed body lifecycle and, when the inbound
request was not fully consumed, forces `Connection: close`. Raw-wire fixtures
assert every matrix row for JSON, binary, SSE, and HEAD.

| Condition | HTTP | Public `type` / `code` / exact `message` |
| --- | ---: | --- |
| wrong authority on a public-surface request | 403 | `permission_error` / `host_forbidden` / `The request host is not allowed.` |
| any public request carrying `Origin` | 403 | `permission_error` / `origin_forbidden` / `Cross-origin requests are not allowed.` |
| unknown visible public path | 404 | `invalid_request_error` / `not_found` / `The requested resource was not found.` |
| malformed JSON, duplicate key, invalid UTF-8/depth/number | 400 | `invalid_request_error` / `invalid_request` / `The request is invalid.` |
| missing/unsupported JSON media type or any content encoding | 415 | `invalid_request_error` / `unsupported_media_type` / `Request content type must be application/json.` |
| well-formed schema/type/range failure, including TTS normalized overflow | 422 | `invalid_request_error` / `invalid_request` / `The request is invalid.` |
| known but unsupported modality | 400 | `invalid_request_error` / `unsupported_modality` / `The requested modality is not supported.` |
| remote or otherwise unsupported media source | 400 | `invalid_request_error` / `unsupported_media_source` / `Only supported data URLs are accepted.` |
| body or decoded media over local bound | 413 | `invalid_request_error` / `request_too_large` / `The request exceeds the configured limit.` |
| request body total or idle deadline before dispatch | 408 | `invalid_request_error` / `request_body_timeout` / `The request body timed out.` |
| unknown model or route/model mismatch | 404 | `invalid_request_error` / `model_not_found` / `The requested model is not available.` |
| authenticated known route with wrong method | 405 | `invalid_request_error` / `method_not_allowed` / `The method is not allowed.` plus exact `Allow` |
| bad downstream credential | 401 | `authentication_error` / `invalid_api_key` / `Invalid API key.` |
| missing scope | 403 | `permission_error` / `insufficient_scope` / `The API key lacks the required scope.` |
| profile not configured/probed | 503 | `api_error` / `profile_unavailable` / `The requested model is temporarily unavailable.` |
| local in-flight capacity exhausted | 503 | `api_error` / `upstream_busy` / `The requested model is at local capacity.` |
| exhausted rate cooling / upstream 429 | 429 | `rate_limit_error` / `upstream_rate_limited` / `All eligible upstream keys are rate limited.` |
| exhausted upstream 401 | 502 | `api_error` / `upstream_auth_error` / `An upstream credential was rejected.` |
| exhausted upstream 403 | 502 | `api_error` / `upstream_permission_error` / `The upstream denied this model.` |
| exhausted upstream 402 | 503 | `api_error` / `upstream_credits_exhausted` / `The upstream has no available credits.` |
| connect/header/body/poll deadline | 504 | `api_error` / `upstream_timeout` / `The upstream request timed out.` |
| connect/TLS failure before the first request byte, not caused by a deadline | 503 | `api_error` / `upstream_unavailable` / `The upstream is unavailable.` |
| status-less transport failure after an upstream request byte and before a deadline | 503 | `api_error` / `upstream_unavailable` / `The upstream is unavailable.` |
| upstream 500/502 or success body `FinishReason.ERROR` | 502 | `api_error` / `upstream_error` / `The upstream request failed.` |
| upstream 408/503/504 | 503 | `api_error` / `upstream_unavailable` / `The upstream is unavailable.` |
| malformed/unexpected status, header, JSON, SSE, poll, or finish reason | 502 | `api_error` / `upstream_protocol_error` / `The upstream returned an invalid response.` |
| bounded upstream response exceeded | 502 | `api_error` / `upstream_response_too_large` / `The upstream response exceeded the limit.` |
| asset create/upload/reconciliation failure | 502 | `api_error` / `upstream_asset_error` / `Upstream media preparation failed.` |
| upstream 400/404/409/422 not otherwise selected | 400 | `invalid_request_error` / `upstream_rejected` / `The upstream rejected the request.` |
| FLUX `finishReason:CONTENT_FILTERED` or SVD `finish_reason:CONTENT_FILTERED` | 400 | `invalid_request_error` / `content_policy_violation` / `The upstream content policy rejected the request.` |
| DB, vault, reservation, or local dependency unavailable before upstream dispatch | 503 | `api_error` / `service_unavailable` / `The service is temporarily unavailable.` plus `Retry-After: 1` |
| other internal failure before upstream dispatch | 500 | `api_error` / `internal_error` / `The server could not complete the request.` |

FLUX accepts only `SUCCESS|ERROR|CONTENT_FILTERED` in `finishReason`; SVD
accepts the same values in `finish_reason`. `SUCCESS` additionally requires the
closed artifact, `ERROR` discards any artifact and maps above, and
`CONTENT_FILTERED` discards it and maps above. Chat/VILA/Phi finish reasons are
their frozen response enums; an unknown value is protocol error. Any status not
listed is protocol error. No provider `detail`, code, message, header, or
`FinishReason` reaches logs or a public message.

Provider 408 is always terminal `upstream_unavailable`, extends the selected
profile transient cooldown, and never alternates even when its body is
absence-proved. A validated success-status FLUX/SVD `FinishReason.ERROR` is
terminal `upstream_error`, extends the same transient cooldown, and cannot
alternate because its success body was received. `CONTENT_FILTERED` has no
health transition. These rows override no other deadline: a local logical
deadline still wins as `upstream_timeout`.

Every admin error is the same closed envelope
`{"error":{"code","message","request_id","retryable"}}`; `request_id` is the
server value created before the authority check, including for wrong-authority
and wrong-Origin failures. `retryable:true`
means no mutation transaction or provider call began and byte-identical retry
is safe; it never means “repeat an unknown mutation.” The complete admin table
is:

| Condition | HTTP / code / exact message / retryable | Required headers |
| --- | --- | --- |
| missing, malformed, or wrong admin token | 401 / `invalid_admin_token` / `Invalid admin token.` / false | `WWW-Authenticate: Bearer realm="nvidia-build-lb-admin"` |
| wrong authority or Origin | 403 / `admin_access_forbidden` / `Admin access is forbidden.` / false | none |
| malformed JSON | 400 / `invalid_request` / `The request is invalid.` / false | none |
| missing/unsupported JSON media type or any content encoding | 415 / `unsupported_media_type` / `Request content type must be application/json.` / false | none |
| admin identity body over 65,536 bytes | 413 / `request_too_large` / `The request exceeds the configured limit.` / false | none |
| closed-schema validation failure | 422 / `validation_failed` / `The request failed validation.` / false | none |
| unknown resource or operation | 404 / `resource_not_found` / `The requested resource was not found.` / false | none |
| stale expected generation | 409 / `stale_configuration` / `Refresh configuration before retrying.` / false | none |
| lifecycle, slot, transitional-row, or active-operation conflict | 409 / `resource_conflict` / `The requested change conflicts with current state.` / false | none |
| durable intake drain blocks a new owner mutation | 409 / `intake_draining` / `The service is draining accepted work.` / false | none |
| reused operation UUID with another fingerprint | 409 / `operation_id_conflict` / `The operation identifier was already used.` / false | none |
| DB unavailable before transaction | 503 / `database_unavailable` / `The database is temporarily unavailable.` / true | `Retry-After: 1` |
| local service unavailable before mutation | 503 / `service_unavailable` / `The service is temporarily unavailable.` / true | `Retry-After: 1` |
| synchronous boundary deadline before mutation | 504 / `request_timeout` / `The request timed out before it began.` / true | none |
| commit or dispatch outcome cannot be reconciled | 500 / `mutation_outcome_unknown` / `Reconcile the operation before another change.` / false | none |
| any other internal failure | 500 / `internal_error` / `The server could not complete the request.` / false | none |
| authenticated known route with wrong method | 405 / `method_not_allowed` / `The method is not allowed.` / false | exact `Allow` |

Admin 405 and every other admin error have an `application/json` canonical
representation. Except for the explicit HEAD wire rule in section 4.1, there is
no empty admin error body. A pending long operation is a successful 202
response, not an error.

When all candidates are rate cooling, `Retry-After` is the rounded-up seconds
to the earliest local expiry. No fabricated RPM or quota header is returned.
Before SSE response start, the table applies as HTTP. After start, a writable
tracked body yields one bounded `event: error` whose `data` is the same error
object plus no provider detail, then closes. If Actix drops the request future
or tracked `MessageBody`, it yields no further item. This is a handoff contract,
not a claim that an OS socket write succeeded or failed.

The custom SSE writer grammar is exact: each application event is serialized as
UTF-8 bytes `event: <name>\ndata: <single-line-json>\n\n`, with no BOM, CR, or
comment; `[DONE]` is the literal `data: [DONE]\n\n` application event. The H1
writer wraps each emitted application byte sequence as one or more HTTP/1.1
chunks `hex-lowercase-size\r\nbytes\r\n`, flushes after each complete SSE
event, and emits exactly `0\r\n\r\n` once after the terminal event. A post-start
error is exactly `event: error\ndata: <closed-error-json>\n\n` followed by the
same zero chunk and socket close; no `[DONE]` follows an error. Chunk coalescing
is forbidden, trailers are forbidden, and a serializer receipt records event
bytes, chunk boundaries, flush count, terminal marker, and EOF. The raw-wire
fixture compares those bytes rather than an Actix body abstraction.

The sole exception is a durability fail-stop after upstream network may have
started: if reservation/terminal readback is unknown, fingerprint-conflicting,
or DB unavailable after a provider side effect, the gateway emits zero new wire
bytes. Before headers it closes the connection without status/body; after SSE
start it closes without an error frame or `[DONE]`. It withdraws readiness and
requires reconciliation. Before upstream dispatch, the same DB/vault/local
failure uses the fixed `service_unavailable` JSON above, so clients never
confuse a safe no-side-effect retry with an unknown provider outcome.

### 5.3 Exact owner snapshot DTOs and freshness

Every owner read object is closed: unknown members, duplicate keys, noncanonical
UUIDs/decimals/timestamps, wrong order, or wrong nullability are server defects
and generated Rust/TypeScript validators reject them. UUID text is canonical
lowercase hyphenated; `Decimal` is `0|[1-9][0-9]*`; timestamps are UTC RFC 3339
with exactly six fractional digits and `Z`. Every array below has the stated
order. The seven paginated/list common-snapshot endpoints are exactly `/overview`,
`/upstream-slots`, `/downstream-credentials`, `/model-capabilities`,
`/attentions`, `/events`, and the paginated `/operations`; each runs one
`REPEATABLE READ READ ONLY` transaction and returns
`Cache-Control: no-store`. Overview samples its readiness inputs exactly once
after that snapshot opens; the other six derive every returned field from the
same snapshot and perform no hidden provider or mutation call.
`/generation-readiness`, `/operations/<id>`, `/events/<id>`, and
`/evidence/<id>` remain separately
closed DTOs defined in sections 5 and 7.1 and are not silently counted as
common-snapshot endpoints. Their common `Snapshot` is exactly:

```text
Snapshot = {
  id:UUID, request_id:UUID, observed_at:Timestamp,
  configuration_generation:Decimal, schema_sha256:LowerHex64,
  freshness_ms:"15000"
}
ResourceRef =
  {kind:"upstream_key"|"downstream_credential"|"operation"|"asset"|
        "backup"|"release"|"hermes",id:UUID}
| {kind:"public_route",id:"nvidia-lb.dongwontuna.net"}
| {kind:"slot",id:"1"|"2"}
| {kind:"profile",id:ProfileId}
DirectEvidenceTarget=
  {kind:"event",id:UUID}|{kind:"evidence",id:UUID}
Attention = {
  id:UUID,
  code:"asset_create_unknown"|"asset_cleanup_pending"|
       "operation_recovery_required"|"probe_failed"|
       "configuration_stale"|"backup_gate_missing"|"release_failed"|
       "hermes_failed"|"provider_contract_drift"|"public_route_degraded",
  resource:ResourceRef, operation_id:UUID|null,
  blocking:Boolean, first_seen_at:Timestamp, updated_at:Timestamp,
  next_retry_at:Timestamp|null, evidence_target:DirectEvidenceTarget
}
The physical `attentions` table is closed as
`(id UUID PRIMARY KEY, code TEXT, resource_kind TEXT, resource_uuid UUID NULL,
resource_literal TEXT NULL, resource_key TEXT NOT NULL, operation_id UUID NULL,
blocking BOOLEAN NOT NULL, first_seen_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
next_retry_at TIMESTAMPTZ NULL, evidence_event_id UUID NOT NULL REFERENCES
events(id), resolved_at TIMESTAMPTZ NULL, resolved_event_id UUID NULL
REFERENCES events(id))`. CHECKs close `code` to the Attention enum,
`resource_kind/resource_uuid/resource_literal` to the ResourceRef union,
`resource_key` to canonical kind-plus-ID bytes, and timestamp ordering;
The named partial unique index
`attentions_active_code_resource_idx ON nblb.attentions(code,resource_key)
WHERE resolved_at IS NULL` permits one active row.
An INSERT/UPDATE guard (`nblb_guard_attention_insert_update`) permits only the
supervisor's validated code/resource/evidence tuple, evidence replacement,
retry/observation updates, and the one resolved transition; DELETE is forbidden.
Opening/replacing an
Attention and its `attention_opened` Event, and resolving it with its
resolution Event, lock `events -> evidence -> attentions` in the global order and
commit atomically. Active/resolved Attention rows, their Event FKs, and
`resolved_at` are included in snapshot, backup, restore, and state-projection
digests.
KeyHandle = {
  id:UUID, label_compact:String, fingerprint_short:LowerHex12,
  lifecycle:"staged"|"assigned"|"retirement_pending"|"retired",
  slot_no:1|2|null, enabled:Boolean,
  global_health:"clear"|"invalid_credential",
  created_at:Timestamp, updated_at:Timestamp
}
```

Attention is a current-state projection, not an immutable event alias. Its DB
row also has nullable `resolved_at`; the nondeferrable partial unique index
`attentions_active_code_resource_idx` permits one row per `(code,resource_key)`
with `resolved_at IS NULL`. The DTO above
selects only those active rows. Opening a predicate creates one
`attention_opened` Event, stores
`evidence_target:{kind:"event",id:<that Event UUID>}`, and
preserves `id,first_seen_at` while later observations only advance
`updated_at`; an unchanged active predicate does not emit duplicate Events.
If the owning typed Evidence changes, one new `attention_opened` Event replaces
the active row's event target in the same transaction. Clearing the
predicate stamps `resolved_at` and emits one `attention_resolved` Event whose
resource and operation ID equal the resolved row and whose `outcome` is
`succeeded`; its `evidence_id` points to the final typed Evidence when one
exists and is otherwise null. An `attention_opened` Event has severity
`attention`, outcome `recovery_required`, and the active row's exact evidence
join; `attention_resolved` has severity `info`. These are the only Event kinds
created merely by Attention lifecycle changes. Resolved rows never appear in
Overview or `/attentions`, but every opening/replacement/resolution Event
remains in `/events` and direct Evidence. The transaction-level foreign keys
require an active row's `evidence_target` to name its latest
`attention_opened` Event; a resolution Event can never remain as an active
Attention target.

`label_compact` is the NFC label unchanged through 48 Unicode scalars; a longer
label is its first 47 scalars plus U+2026. `fingerprint_short` is the first 12
lowercase hex digits of the raw fingerprint. These display projections never
select or authenticate a row.

`GET /overview` returns exactly:

```text
{
  snapshot:Snapshot,
  readiness:{management_ready:Boolean,traffic_ready:Boolean,draining:Boolean,
             public_health:"unknown"|"ok"|"degraded",
             public_health_observed_at:Timestamp|null},
  judgments:{configured_slots:"0"|"1"|"2",advertised_profiles:Decimal,
             currently_eligible_key_profiles:Decimal,
             currently_available_profiles:Decimal,
             active_downstream_credentials:Decimal},
  staged:StagedDetail|null, retirement_pending_keys:[KeyHandle],
  routing_repair:RoutingRepair|null,
  open_mutation_intent:MutationIntent|null,
  active_operations:[AdminOperation],
  newest_attention:Attention|null, last_operation:AdminOperation|null,
  server_recommendation:ServerRecommendation
}
```

`open_mutation_intent` is the same unacknowledged singleton selected in this
repeatable-read transaction; the browser never has to race a second GET before
choosing the first action. `active_operations` contains exactly `queued|running|cancel_requested|
recovery_required`, sorted by `(created_at,id)` ascending. `last_operation` is
the greatest `(updated_at,id)` or null. Attention selection is blocking first,
then greatest `(updated_at,id)`. Counts are from the same DB snapshot.
`retirement_pending_keys` sorts by `(created_at,id)` and may contain more than
one cleanup-only key. The two additional closed projections are:

`currently_eligible_key_profiles` is the count 0..14 of assigned
`(key_id,profile_id)` rows whose `eligible_now` is true at `snapshot.observed_at`;
`currently_available_profiles` is the count 0..8 of profile rows with at least
one such eligible slot. Both use the same repeatable-read snapshot and exact
cooldown/capacity/manual/proof predicates as Models, so they cannot disagree
with its seven rows.

`public_health` is external reachability, not a second spelling of structural
`traffic_ready`. After the tunnel rollout reaches `dns_created`, one singleton
supervisor per process graph uses the separate fixed-host safe H1 client every
30 seconds to GET exactly
`https://nvidia-lb.dongwontuna.net/health` with a five-second total deadline,
normal WebPKI roots, no credential/cookie/redirect/proxy/retry, and the exact
public Host. It stores one bounded `public_route_observations` row only after a
complete status/body/content-type check: `ok` means exact 200 and the exact
section 5 health body; every definite DNS/TCP/TLS/HTTP/body mismatch is
`degraded`. Before DNS activation or with no observation it is `unknown`.
Rows contain only UUID, process epoch, result, observed time, latency and a
closed failure class; no address, certificate body, response headers/body, or
query is retained. The newest observation is current for 90 seconds. Overview
returns `unknown` with null time when none exists, otherwise its result and
nonnull time while current; an older row returns `unknown` plus its nonnull last
time. The monitor never feeds `/health`, readiness, routing, or release
authority, so self-probe failure cannot create recursion or flap local intake.
Release/public smoke remains the activation authority. The first Overview
judgment therefore presents three independent facts: structural proof,
currently available profile count, and last public reachability/time.

While the latest observation is current and `degraded`, the public-health
supervisor's separate write transaction atomically inserts the observation and
opens or updates one blocking Attention with code `public_route_degraded` and
resource `{kind:"public_route",id:"nvidia-lb.dongwontuna.net"}`. Its evidence
target is the immutable attention-opened Event and its safe action is
`open_evidence`/`GET /events/<id>`; the owner UI copy explicitly says the local
gateway may be healthy while the public tunnel/DNS path needs operator
verification. A current `ok` observation resolves that Attention once and
emits the normal resolution Event. The read-only Overview snapshot then reads
the committed observation/Attention/Event projection; it never writes while
constructing `server_recommendation`. `unknown` (before DNS or after the
90-second freshness window) does not claim a failure and does not open this
Attention.
Therefore a tunnel-only outage cannot produce `server_recommendation:"none"` or
the misleading “no action required” copy while structural readiness remains
green.

```text
StagedDetail = {
  key:KeyDetail,
  intent:"first"|"second"|"replacement", target_slot:1|2,
  latest_operation:AdminOperation|null,
  proof_summary:{verified_profiles:[ProfileId],all_per_key_current:Boolean,
                 all_required_pair_current:Boolean,pair_voice_count:Decimal|null}
}
RoutingRepair =
  {reason:"missing_proof"|"stale_proof",profile_id:ProfileId,slot_no:1|2,
   key_id:UUID,expires_at:null,next_action:"probe",
   evidence_target:DirectEvidenceTarget}
| {reason:"manual_disabled",profile_id:ProfileId,slot_no:1|2,key_id:UUID,
   expires_at:null,next_action:"enable",evidence_target:DirectEvidenceTarget}
| {reason:"invalid_credential",profile_id:ProfileId,slot_no:1|2,key_id:UUID,
   expires_at:null,next_action:"replace_slot"|"reset_upstream_pair",
   evidence_target:DirectEvidenceTarget}
| {reason:"not_entitled"|"credits_exhausted"|"protocol_quarantined",
   profile_id:ProfileId,slot_no:1|2,key_id:UUID,expires_at:null,
   next_action:"inspect_evidence",evidence_target:DirectEvidenceTarget}
| {reason:"rate_cooldown"|"transient_cooldown",profile_id:ProfileId,
   slot_no:1|2,key_id:UUID,expires_at:Timestamp,next_action:"wait_expiry",
   evidence_target:DirectEvidenceTarget}
| {reason:"capacity",profile_id:ProfileId,slot_no:1|2,key_id:UUID,
   expires_at:null,next_action:"wait_capacity",evidence_target:null}
ServerRecommendation=
  {action:"monitor_operation",operation_id:UUID}
| {action:"wait_system",reason:"draining"|"management_not_ready",
   retry_at:Timestamp|null}
| {action:"open_evidence",resource:ResourceRef,
   target:DirectEvidenceTarget}
| {action:"configure_slot",slot_no:1|2}
| {action:"start_probe",key_id:UUID,profiles:[ProfileId]}
| {action:"review_staged",key_id:UUID,target_slot:1|2,
   completion:"assign"|"replace"}
| {action:"staged_recovery",key_id:UUID,
   next_action:"wait_cleanup"|"delete_staged"|"quarantine_staged"|
               "retry_probe"|"refresh_then_retry"|"inspect_evidence",
   next_retry_at:Timestamp|null,evidence_target:DirectEvidenceTarget|null}
| {action:"routing_repair",repair:RoutingRepair}
| {action:"issue_first_client"}
| {action:"none"}
```

`StagedDetail.key.label_full`, intent, target slot, proof summary, and the
greatest resource-local `(updated_at,id)` operation are selected in this same
overview transaction; the target is never inferred in the browser. The intent
is `first` only when target Slot 1 is empty and Slot 2 is empty, `second` only
for target Slot 2 with Slot 1 assigned, and `replacement` only when the target
slot was explicitly bound at staged creation. `routing_repair` is the first
currently unsatisfied row by profile-manifest order then slot, using health
precedence auth, permission, credits, protocol, rate, transient, capacity,
manual-disabled, proof. It includes the exact cooldown expiry when waiting is
the only safe action. For invalid credential, `replace_slot` is emitted iff a
distinct assigned survivor has all current per-key proofs required to enter a
replacement pair probe; otherwise `reset_upstream_pair` is emitted. Capacity
means wait for a live-pin release and refresh—there is no invented expiry or
mutation or durable evidence row. Every other reason has the exact nonnull
evidence ownership defined by Models/KeyProfile below. No other reason/action/
nullability combination validates. Therefore priority 8 is computed from one atomic snapshot
and never races a separately fetched slot page.
Every `RoutingRepair.key_id` is the exact assigned identity in `slot_no` from
that snapshot. Its `probe` action always calls
`POST /upstream-keys/<key_id>/probe` with the one manifest-ordered profile; a
staged/replacement candidate is handled earlier by `StagedDetail` and can never
appear here. Enable/replace/reset actions verify the same key/slot identity
before preparing their intent.

`currently_available_profiles` is the count of Models rows with
`available_now:true` in this same snapshot and is 0..7. The server constructs
exactly one `server_recommendation` after every other Overview projection. Its
first-match order is: the self-excluded active/recovery operation; system hold
when `draining:true` or `management_ready:false` and no such controller;
blocking Attention; missing Slot 1; staged Slot 1; missing Slot 2; staged
second/replacement; routing repair; first downstream credential; newest
nonblocking Attention; none. `wait_system` contains no target ID and is the
only recommendation while a root release/restore drain or management-not-ready
condition blocks new mutations. Existing accepted reset/release/restore
operations may expose only their exact monitor/reconcile surface.
For the blocking `public_route_degraded` Attention, the selected recommendation
is exactly `open_evidence` with that Attention's immutable Event target; other
blocking Attention rows use their typed recovery action. A current public
degraded observation therefore always wins before any “none” recommendation.
When more than one active operation exists, the selected row is first by state
precedence `recovery_required`, `cancel_requested`, `running`, `queued`, then
by `(created_at,id)` ascending within that state. The full
`active_operations` array retains its chronological order; only the single
recommendation applies this total selection key. Distinct-key operations may
coexist, but two implementations cannot choose different targets.
Within a staged row, active operation maps only to `monitor_operation`; a
terminal error maps only to its exact `staged_recovery`; absent/currently stale
proof maps only to a full manifest-ordered `start_probe`; current complete proof
maps only to `review_staged`, whose `completion` is derived from staged intent.
A review screen then has one snapshot-bound Assign or Replace control; reload
returns to Review instead of inventing client-side review completion. Evidence
targets and retry times are copied from the same operation/Attention row.
`configure_slot:2` is valid only with Slot 1 assigned and no staged row.
Generated SQL projection tests enumerate every lifecycle/operation/proof/input
combination and reject two valid recommendations for one snapshot.

`GET /upstream-slots` returns exactly
`{snapshot,slots,staged,retirement_pending_keys}`. `slots` is always Slot 1 then
Slot 2; each row is `{slot_no:1|2,key:KeyDetail|null}`. `staged` is the same
closed `StagedDetail` as Overview; retirement rows use the same order as
Overview. Both may be nonempty because cleanup quarantine never consumes the
single staged-candidate position.

```text
KeyDetail = KeyHandle + {
  label_full:String,
  global_last_failure_at:Timestamp|null,
  profiles:[KeyProfile]
}
KeyProfile = {
  profile_id:ProfileId, proof_revision:Decimal,
  per_key_proof_current:Boolean, pair_proof_current:Boolean,
  entitlement:"unknown"|"ok"|"not_entitled"|"credits_exhausted",
  protocol_state:"clear"|"protocol_quarantined",
  cooldown_kind:"none"|"rate"|"transient"|"rate_and_transient",
  rate_cooldown_until:Timestamp|null,
  transient_cooldown_until:Timestamp|null,
  in_flight:Decimal, in_flight_limit:Decimal, eligible_now:Boolean,
  current_reason:"available"|"manual_disabled"|"invalid_credential"|
                 "not_entitled"|"credits_exhausted"|"protocol_quarantined"|
                 "rate_cooldown"|"transient_cooldown"|"capacity"|
                 "missing_proof"|"stale_proof",
  reason_evidence_target:DirectEvidenceTarget|null,
  last_proof_at:Timestamp|null
}
```

Every `profiles` array has all eight rows in manifest order, including staged
keys. Null cooldown timestamps correspond exactly to inactive cooldowns;
`cooldown_kind` is derived from unexpired timestamps at `observed_at`.

`GET /downstream-credentials` returns exactly
`{snapshot,credentials,active_count}`. Credentials sort active first, then
`(created_at,id)` descending and are:

```text
{
  id:UUID,label:String,scopes:[Scope],active:Boolean,
  request_count:Decimal,created_at:Timestamp,last_used_at:Timestamp|null,
  revoked_at:Timestamp|null
}
```

Scopes use the section 5 allowlist order. Active means `revoked_at:null`;
inactive means nonnull. No digest or plaintext field exists.

`GET /model-capabilities` returns exactly `{snapshot,models}` with eight rows in
manifest order:

```text
{
  id:ProfileId,
  route:"chat"|"embeddings"|"images"|"speech"|"nvidia_native",
  input_modalities:["text"|"image"|"audio"|"video"],
  output_modalities:["text"|"vector"|"image"|"audio"|"video"],
  advertised:Boolean,
  available_now:Boolean,eligible_slots:"0"|"1"|"2",
  current_reason:"available"|"unconfigured"|"manual_disabled"|
                 "invalid_credential"|"not_entitled"|"credits_exhausted"|
                 "protocol_quarantined"|"rate_cooldown"|
                 "transient_cooldown"|"capacity"|"missing_proof"|
                 "stale_proof",
  next_change_at:Timestamp|null,
  contract_evidence_target:DirectEvidenceTarget,
  slot_proofs:[{slot_no:1|2,key_id:UUID|null,current:Boolean,
                last_proof_at:Timestamp|null,
                proof_evidence_target:DirectEvidenceTarget|null,
                reason:<KeyProfile current_reason>|"unconfigured",
                reason_evidence_target:DirectEvidenceTarget|null}],
  current_reason_evidence_target:DirectEvidenceTarget|null,
  common_voices:null|[{voice:String,locale:String,
                       kind:"base"|"emotion"}]
}
```

Input modality arrays use fixed order text, image, audio, video; output arrays
use text, vector, image, audio, video, with only applicable members.
`slot_proofs` is Slot 1 then Slot 2.
`advertised` is structural two-key proof/publication state. `available_now` is
true iff `eligible_slots` is nonzero at the same snapshot. `current_reason` is
`available` when true and `unconfigured` when the exact two assigned identities
do not exist. Otherwise each slot contributes its first reason in auth,
permission, credits, protocol, rate, transient, capacity, manual-disabled,
stale-proof, missing-proof order, and the row takes the first present reason in
that same cross-slot order. `current_reason_evidence_target` is the chosen
slot's exact closed target. It and each slot reason target are null only for
`available|unconfigured|capacity`; health/quarantine/cooldown/manual reasons
link their immutable routing-state evidence, missing proof links the current
profile-contract evidence, and stale proof links the rejected prior proof
evidence. This is the owner-reason projection of section 7.2's
zero-attempt precedence, not a browser inference. `next_change_at` is nonnull exactly for rate/
transient cooldown and is the earliest relevant expiry; capacity has null.
Contract, proof, KeyProfile reason, slot reason, and current-reason targets are
always `{kind:"evidence",...}` because they name typed Evidence rows.
Attention and `RoutingRepair.evidence_target` are always
`{kind:"event",...}` because they name their immutable owning Event. These
kind constraints are generated union refinements, not browser wrapping rules;
an ID with the wrong kind fails the DTO before a trigger exists.
The Models page labels these separately as `검증·광고 상태` and `현재 요청 가능`
and never calls `advertised` current availability.
`common_voices` is nonnull only for Magpie, sorted by unsigned UTF-8 voice
bytes, and is the exact published DB map; Magpie returns `[]` when no singleton
is published, and every other profile returns null.

The route and modality arrays are not inferred at runtime; their exact rows are:

| Profile | Route | Exact input modalities | Exact output modalities |
| --- | --- | --- | --- |
| `z-ai/glm-5.2` | `chat` | `["text"]` | `["text"]` |
| `microsoft/phi-4-multimodal-instruct` | `chat` | `["text","image","audio"]` | `["text"]` |
| `nvidia/vila` | `chat` | `["text","image","video"]` | `["text"]` |
| `nvidia/nvclip` | `embeddings` | `["text","image"]` | `["vector"]` |
| `black-forest-labs/flux.1-kontext-dev` | `images` | `["text"]` | `["image"]` |
| `stabilityai/stable-video-diffusion` | `nvidia_native` + `videos` | `["image"]` | `["video"]` |
| `nvidia/magpie-tts-multilingual` | `speech` | `["text"]` | `["audio"]` |
| `nvidia/parakeet-ctc-1.1b` | `transcriptions` | `["audio"]` | `["text"]` |

`GET /events?before=<cursor>&limit=<n>` requires decimal `limit` 1..100 and an
optional cursor. Events sort by `(occurred_at,id)` descending. The cursor is
canonical unpadded base64url of exactly `0x01 || u64be(unix_microseconds) ||
event_uuid.network_bytes`; it names an exclusive upper bound and is not a DB
offset. Invalid/noncanonical cursors are admin `validation_failed`. The response
is exactly `{snapshot:Snapshot,events:[Event],next_before:String|null}`;
`next_before` encodes the last returned row only when another row exists,
otherwise null. Events are immutable, and the existence check for the next row
uses the same transaction as the page and its snapshot.

```text
Event = {
  id:UUID,occurred_at:Timestamp,
  kind:"legacy_imported"|"legacy_token_revoked"|"upstream_staged"|"probe_terminal"|"assigned"|
       "replaced"|"upstream_reset"|"retired"|"slot_state_changed"|"downstream_issued"|
       "downstream_revoked"|"request_terminal"|"cooldown_changed"|
       "quarantine_changed"|"asset_attention"|"asset_quarantined"|
       "operation_recovery"|"attention_opened"|"attention_resolved"|
       "backup_terminal"|"restore_terminal"|"release_terminal"|
       "hermes_terminal",
  severity:"info"|"attention",resource:ResourceRef,
  operation_id:UUID|null,profile_id:ProfileId|null,slot_no:1|2|null,
  outcome:"succeeded"|"failed"|"cancelled"|"recovery_required"|null,
  evidence_id:UUID|null
}
```

The physical `events` table is append-only and closed as
`(id UUID PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL, kind TEXT NOT NULL,
severity TEXT NOT NULL, resource_kind TEXT NOT NULL, resource_uuid UUID NULL,
resource_literal TEXT NULL, operation_id UUID NULL, profile_id TEXT NULL,
slot_no SMALLINT NULL, outcome TEXT NULL, evidence_id UUID NULL,
canonical_json BYTEA NOT NULL, payload_sha256 BYTEA NOT NULL CHECK
(octet_length(payload_sha256)=32))`. CHECKs close every enum/resource union,
slot/profile/outcome combination, canonical JCS bytes/hash, and
`occurred_at`; `nblb_guard_event_immutable` is an `ENABLE ALWAYS` trigger that
rejects UPDATE/DELETE/TRUNCATE and `nblb_guard_event_insert` validates the
resource/kind/evidence relationship. `nblb_app` may INSERT/SELECT only. The
single physical pair constraint is exactly
`FOREIGN KEY (events.evidence_id, events.id) REFERENCES
evidence(id,event_id) DEFERRABLE INITIALLY DEFERRED`, so the event/evidence pair
is committed atomically; the cycle is never visible half-linked. The named
`nblb_guard_event_evidence_pair` `CONSTRAINT TRIGGER` is attached to both
`events` and `evidence`, is `DEFERRABLE INITIALLY DEFERRED`, and runs at commit.
For every event it requires `evidence_id IS NULL` exactly for event kinds whose
canonical map says evidence is optional/forbidden, and requires exactly one
matching row `(evidence.id,evidence.event_id)=(events.evidence_id,events.id)`
when evidence is required or supplied. For every evidence row it requires one
event, the reverse pair to match byte-for-byte, and the event kind to permit
that Evidence variant; an orphan evidence row, an event/evidence ID mismatch,
a missing required pair, or a null required `evidence_id` is rejected. The
migration creates `events` without the reverse pair constraint, creates
`evidence` with its `UNIQUE(id,event_id)` and event FK, then adds the exact
composite FK and the deferred pair trigger in one forward migration; rollback
removes the trigger/FKs in reverse order. The exact physical DDL, indexes,
trigger names, function digests, and ACLs are in the catalog manifest and any
unknown event column/object is schema drift.

`GET /attentions?before=<cursor>&limit=<n>` requires the same limit grammar but
uses its own canonical cursor
`0x02 || blocking_byte || u64be(updated_at_microseconds) || id.network_bytes`.
`blocking_byte` is `0x01` for blocking and `0x00` otherwise. Rows order
`blocking DESC,updated_at DESC,id DESC`; the cursor names the exclusive tuple in
that order. The response is exactly
`{snapshot:Snapshot,attentions:[Attention],next_before:String|null}`, and the
same repeatable-read transaction performs active membership, page, and next-row
existence checks. It never returns a resolved row or filters one arbitrary
Event page in the browser. The ordinary `/events` index remains complete
history.

Evidence is a closed secret-free discriminated union:

```text
EvidenceCommon={id:UUID,event_id:UUID,recorded_at:Timestamp,
                resource:ResourceRef,operation_id:UUID|null}
RoutingStateProjection={global_health:"clear"|"invalid_credential",
 entitlement:"unknown"|"ok"|"not_entitled"|"credits_exhausted",
 protocol:"clear"|"protocol_quarantined",enabled:Boolean,
 proof_revision:Decimal,rate_cooldown_until:Timestamp|null,
 transient_cooldown_until:Timestamp|null}
EvidenceDetail = EvidenceCommon +
  {variant:"probe_case",key_id:UUID,profile_id:ProfileId,case_id:CaseId,
   status:"pass"|"failed"|"cancelled"|"abandoned_after_restart",
   latency_ms:Decimal,response_shape_sha256:LowerHex64,
   predicate_evaluator_sha256:LowerHex64,
   quality_projection_sha256:LowerHex64|null,
   predicate_count:Decimal,predicate_bits:LowerHex,
   cleanup_state:"none"|"complete"|"pending"|"unknown",
   next_retry_at:Timestamp|null}
| EvidenceCommon +
  {variant:"routing_state_transition",key_id:UUID,slot_no:1|2,
   profile_id:ProfileId,attempt_id:UUID|null,
   cause:"upstream_401"|"upstream_403"|"upstream_402"|"upstream_429"|
         "transient_failure"|"protocol_failure"|"probe_success"|
         "manual_disabled"|"manual_enabled",
   sequence:Decimal|null,applied:Boolean,
   before:RoutingStateProjection,after:RoutingStateProjection,
   remediation:"none"|"external_fix_then_probe"}
| EvidenceCommon +
  {variant:"asset_cleanup",attempt_id:UUID,profile_id:ProfileId,
   intent_count:Decimal,known_object_count:Decimal,
   state:"pending"|"unknown"|"complete"|"revoked_cleanup_closed",
   next_retry_at:Timestamp|null}
| EvidenceCommon +
  {variant:"spool_orphan",attempt_id:UUID,boot_id:UUID,
   directory_name:String,aggregate_bytes:Decimal,directory_count:Decimal,
   sealed_files:[{ordinal:Decimal,size:Decimal,sha256:LowerHex64}],
   unknown_entries:Decimal,orphan_bytes:Decimal,
   disposition:"abandoned_after_restart"|"attention",
   receipt_sha256:LowerHex64}
| EvidenceCommon +
  {variant:"profile_contract",profile_id:ProfileId,source_url:String,
   source_bytes:Decimal,source_sha256:LowerHex64,
   profile_contract_sha256:LowerHex64,probe_suite_sha256:LowerHex64}
| EvidenceCommon +
  {variant:"operation_terminal",kind:<AdminOperation kind>,
   state:"succeeded"|"failed"|"cancelled"|"recovery_required",
   error_code:<operation-error>|null}
| EvidenceCommon +
  {variant:"backup"|"restore"|"release"|"hermes",
   transaction_id:UUID,state:"succeeded"|"failed"|"attention",
   manifest_sha256:LowerHex64|null,receipt_sha256:LowerHex64}
```

The physical `evidence` table is append-only and closed as
`(id UUID PRIMARY KEY, event_id UUID NOT NULL, recorded_at TIMESTAMPTZ NOT
NULL, variant TEXT NOT NULL, resource_kind TEXT NOT NULL, resource_uuid UUID
NULL, resource_literal TEXT NULL, operation_id UUID NULL, canonical_json BYTEA
NOT NULL, payload_sha256 BYTEA NOT NULL CHECK (octet_length(payload_sha256)=32),
UNIQUE(event_id), UNIQUE(id,event_id), FOREIGN KEY(event_id) REFERENCES
events(id) DEFERRABLE INITIALLY DEFERRED)`. Its validator checks the exact
EvidenceDetail discriminated union, canonical JCS bytes/hash, secret-free
fields, resource union, and variant-specific foreign keys (key/profile/attempt/
operation/transaction) before commit. `nblb_app` may INSERT/SELECT only;
`nblb_guard_evidence_insert` performs that union/JCS/secret-free/FK validation;
`nblb_guard_evidence_immutable` rejects UPDATE/DELETE/TRUNCATE and both are included
in the function/catalog manifest. The scalar `evidence.event_id -> events.id`
FK is retained for ordinary parent existence; the only relationship that
binds the optional reverse pointer is the exact deferred pair constraint
`FOREIGN KEY (events.evidence_id, events.id) REFERENCES
evidence(id,event_id) DEFERRABLE INITIALLY DEFERRED` plus the named pair
constraint trigger described above; an event cannot point at a different
evidence row or a missing row. Evidence rows and hashes are included in
snapshot, backup, restore, and state-projection digests.

`GET /events/<id>` returns exactly
`{snapshot:Snapshot,event:Event,evidence:EvidenceDetail|null}`; when the event
has `evidence_id`, the joined evidence ID/event ID must match, otherwise the
endpoint fails readiness rather than returning a mismatched row. `GET
/evidence/<id>` returns exactly `{snapshot:Snapshot,evidence:EvidenceDetail}`.
Both use one repeatable-read transaction, ordinary owner auth, no provider call,
and the standard 404 for an unknown exact ID. Probe media/prompt/output, raw
provider body/header, asset UUID/description/raw digest, URL query, credentials,
ciphertext, nonce, token digest, host secret path, and free-form error text are
absent from every variant. Models links its contract and per-slot proof IDs
directly; health, quarantine, cooldown, and manual reasons link the exact
`routing_state_transition`; Routing/Overview operation errors link their exact event, so one click
always reaches the owning typed evidence without cursor searching.

Routing evidence nullability is closed. Manual enable/disable has null attempt/
sequence. Provider causes have both nonnull; probe success has null attempt,
nonnull common operation ID, and nonnull sequence. `applied:false` proves an
older health sequence was receipt-only and therefore `before==after`;
`applied:true` requires the exact table transition and cooldown expiry. Only
`upstream_402|upstream_403` has
`remediation:"external_fix_then_probe"`; every other cause is `none`.

`GET /operations?before=<cursor>&limit=<n>` is the closed paginated operation
index. It uses the same limit grammar and cursor bytes as `/events`, substituting
`updated_at` for `occurred_at`; rows sort `(updated_at,id)` descending and the
exclusive cursor, next-row existence check, `{snapshot,operations,next_before}`
envelope, and all `AdminOperation` rows come from one repeatable-read snapshot.
`GET /operations/<id>` remains the exact direct lookup. This index is owner
history/evidence, not a heuristic for replaying a mutation.

The browser records `performance.now()` immediately before request dispatch.
Snapshot age is `performance.now()-dispatch_mark`; it is fresh only while the
document is visible, online, no refresh is in flight, and age is at most 15,000
ms. Visible authenticated pages poll every 10,000 ms and refresh immediately
after any operation terminal. Hidden pages stop polling and force a refresh on
visibility before enabling mutation. Server `observed_at` is display/audit data
and never participates in client freshness, so clock skew cannot enable a stale
mutation. All mutation bodies use the generation from that exact fresh
snapshot.

## 6. Closed NVIDIA profile registry

Runtime never scrapes documentation. `contracts/nvidia/` contains sanitized
source snapshots, their byte lengths/SHA-256, derived closed schemas, fixtures,
and one generated profile manifest. Changing a profile requires design review,
fixture review, both-key live probe evidence, and a new immutable candidate.

| Profile | Official source observed 2026-07-18 | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| GLM-5.2 | `https://build.nvidia.com/z-ai/glm-5.2.md` | 9,407 | `77251187fd77cfd06cb049f6dd1d2b10aa1084c2f706e5fe2a2c1f74cb8b9077` |
| Phi-4 multimodal | `https://docs.api.nvidia.com/nim/reference/microsoft-phi-4-multimodal-instruct-infer.md` | 29,907 | `87248d562905403723c1cde95e181be92dabdc361748c389ae2ab9a0708bd8e7` |
| VILA | `https://docs.api.nvidia.com/nim/reference/nvidia-vila-infer.md` | 47,942 | `ff4f2c1db3ea052d4229f8b47fd92492f70bf4996fa4244f62b237daee362c8c` |
| NVCLIP | `https://docs.api.nvidia.com/nim/reference/nvidia-nvclip-infer.md` | 148,835 | `6e420a7c7e2d2c725eeaf8d4e0125e8cf555e7eeedc1f6f17f933ea20bd3b7f9` |
| FLUX.1 Kontext | `https://docs.api.nvidia.com/nim/reference/black-forest-labs-flux_1-kontext-dev-infer.md` | 15,188 | `454ae7befdbdcb51594a610573fd8672f92e252cd1ac2b57a43ca2d9d2ee85e4` |
| Stable Video Diffusion | `https://docs.api.nvidia.com/nim/reference/stabilityai-stable-video-diffusion-infer.md` | 11,173 | `7c518d188300d34cc8a9e205bb4a1d5a0c51838d2fa35c4c3fabd4a0ca4a3370` |
| Magpie TTS multilingual | sanitized extraction from `https://build.nvidia.com/nvidia/magpie-tts-multilingual` | 291-byte normalized JSON | `85ae147833bd56c08963f800567983101102c47240d6157ea6b149a35750e6de` |
| NVIDIA TTS HTTP contract | `https://docs.nvidia.com/nim/speech/latest/reference/api-references/tts/http-tts.html` | 84,739 | `bd9e5b2e84a36b83e3c4ead7aee4799653d0c6d0e9ad4e2e42442ed11c4a2904` |
| NVCF asset create | `https://docs.api.nvidia.com/cloud-functions/reference/createasset.md` | 3,858 | `10a083ef768f0dae6b0274b123244edb7ac26b682345c7ada37c54957b86a2b7` |
| NVCF asset list | `https://docs.api.nvidia.com/cloud-functions/reference/getassets.md` | 2,888 | `0f546d571ea67d3ec0e32ef333903bf7e0582c41462edd8e7e843f0290655df3` |
| NVCF asset delete | `https://docs.api.nvidia.com/cloud-functions/reference/deleteasset.md` | 1,987 | `7d9fdbbe91c71a6aefe3b37da90de7b6044be4bb22d108d6c16e460c3a4178ee` |

The Magpie build page is fetched by `tools/contracts/extract_magpie_profile`
using exact GET, final URL equality, `Accept: text/html`,
`Accept-Encoding: identity`, and `User-Agent: nblb-contract-fetch/1`; redirects
outside `build.nvidia.com` fail. The extractor decodes HTML entities and JSON
string escapes, requires exactly one unique canonical UUID value associated
with key `nvcfFunctionId`, exactly one `/v1/audio/list_voices` curl path and one
`/v1/audio/synthesize` curl path in the selected model payload, and the exact
multipart field/encoding/sample-rate literals below. Ambiguous/missing values
fail; timestamps and unrelated page data are discarded. A provenance receipt
stores retrieval time, final URL, status, content type, raw byte length/hash,
TLS peer summary, extractor source SHA-256, and normalized result hash. The raw
HTML and receipt are retained under `contracts/nvidia/provenance/` but their
time-varying hash is provenance, not wire authority. There is no `models.md`
size/hash claim.

The normalized snapshot is the exact minified UTF-8, no-final-newline JSON
below. Contract generation byte-checks it and also uses the separately frozen
NVIDIA TTS HTTP document for request/response semantics.

```json
{"function_id":"877104f7-e885-42b9-8de8-f6e4c6303969","list_path":"/v1/audio/list_voices","model":"nvidia/magpie-tts-multilingual","multipart_fields":["text","language","voice","encoding","sample_rate_hz"],"output_mime":"audio/wav","sample_rate_hz":44100,"synth_path":"/v1/audio/synthesize"}
```

Voice discovery is exact `GET` to
`https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com:443/v1/audio/list_voices`
with the key bearer, `Accept: application/json`, no body, a 10-second deadline,
and a 2 MiB response limit. Success is 200 and one JSON object with exactly one
key: a nonempty comma-separated list of unique BCP-47 locale codes; its value
is exactly `{voices:[...]}`. `voices` is a nonempty set of unique 1..128 ASCII
strings matching `Model.LOCALE.Speaker` or
`Model.LOCALE.Speaker.Emotion`; dot components match
`[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. The second component is normalized only for
comparison as lowercase language plus uppercase region and must occur in the
locale key. The stored public voice remains byte-exact. A byte-exact voice maps
to that locale; duplicate voices, conflicting mappings, extra keys, an empty
set, or any other shape fails the Magpie proof.

Per-key voice maps are sorted by unsigned UTF-8 bytes. Pair availability is the
byte-exact intersection whose locale also matches; the common map is replaced
atomically only after both current maps validate and the section 7 pair probe
passes on the same deterministic common base voice and, when present, common
emotion voice. The public voice allowlist is that sorted intersection. If it is
empty, has no three-component base voice, or its pair receipt is stale, Magpie
alone is unadvertised and new downstream issuance is blocked; other profiles
remain independently usable. A synth request uses the selected voice's stored
locale, never derives language by case conversion at request time.

### 6.1 Exact transport descriptors

Every request uses HTTPS port 443, a new HTTP/1.1 connection, the exact embedded
`webpki-roots` set above,
and exact `Host`. Environment/system proxy, redirect, cookie jar, HTTP/2,
compression, automatic transport retry, pooled stale-connection retry, and
provider URL override are disabled. The first outbound request byte is the
replay-unsafe transport boundary. A connect/TLS failure before that byte is
safe; a status-less failure after it is ambiguous and never alternates. A fully
received safe terminal status may use sections 5.2 and 7.2.

`nblb-h1` resolves only the manifest hostname with `tokio::net::lookup_host`,
canonicalizes/deduplicates addresses, and may try another address only after a
connect/TLS failure and before request dispatch. TLS fixes SNI to that hostname,
ALPN to exactly `http/1.1`, disables early data, and verifies through the pinned
root set. The serializer emits one precomputed request head with exact Host,
Content-Length, and allowlisted headers plus the complete bounded body; it never
uses request chunking. Immediately before the first `AsyncWrite` call that may
accept any serialized request byte, the attempt durably/atomically marks
`upstream_request_maybe_sent`. The marker is conservative even if that call
later returns zero/error. No address, key, or alternate is tried afterward.

Response head parsing uses `httparse` into an ordered fixed-capacity 128-header
array with a 64 KiB total head bound. The product validates every raw occurrence
before combining anything: obs-fold, invalid token/value bytes, duplicate
singleton fields, comma-smuggled framing, unsupported interim response, and
ambiguous Content-Length/Transfer-Encoding fail closed. A local safe state
machine owns the following closed framing grammar and reports the first decoded
upstream body byte before yielding any application item:

- `Content-Length` is exactly one OWS-trimmed ASCII decimal token, `0` or a
  nonzero digit followed by digits, with no sign/leading zero/list; parsed
  checked into `u64` and bounded by the selected route before allocation.
- `Transfer-Encoding` is exactly one raw field whose OWS-trimmed value is the
  ASCII-case-insensitive single token `chunked`. Lists, parameters, repeated
  fields, any other coding, and coexistence with Content-Length are rejected.
- Each chunk line is strict CRLF and contains 1..16 ASCII hexadecimal digits
  only. Chunk extensions (`;...`) are rejected. The checked chunk size and
  checked cumulative decoded size may not exceed the route bound. Each nonzero
  chunk has exact data length plus CRLF. The terminal is exactly
  `0\r\n\r\n`; all trailers, even otherwise valid fields, and bytes after that
  terminator are rejected. A split token/CR/LF is accepted only while the
  cumulative framing buffer remains at most 1,024 bytes.
- With neither framing header, a response is close-delimited only for a final
  non-HEAD response whose status semantics allow a body. EOF is the sole
  terminator and the decoded route bound still applies. HTTP/1.0 or HTTP/1.1 is
  accepted; an optional single `Connection: close` is allowed, while any other
  Connection token/list and every keep-alive reuse are rejected. A response
  whose status forbids a body must have zero decoded bytes and no transfer
  framing.

Bare LF, obs-fold, whitespace around chunk digits, more than 128 total headers,
more than 64 KiB head bytes, oversized framing, forbidden trailer, premature
EOF, surplus content-length bytes, short content-length EOF, and a second
response are protocol errors. For 401/402/403/429 alternate safety, body absence
is proved only by literal Content-Length zero or a status that forbids a body;
chunked or close-delimited framing is never an absence proof even if it later
decodes empty. Pooling, automatic retry, redirect, decompression, and error-
body interpretation do not exist in the crate. Differential raw-socket/TLS
fixtures cover split bytes, duplicate headers, malformed chunks/trailers,
close races, first-write marker races, every parser error class, DNS address
iteration before dispatch, and zero iteration after dispatch.

Every terminal 200 success response has exactly one media type and
`Content-Encoding` either absent or exactly one OWS-trimmed
ASCII-case-insensitive `identity` field; duplicate or comma-joined
Content-Type/Encoding fields, including repeated identities, are protocol
error. JSON uses `application/json` with absent or one
case-insensitive `charset=utf-8` parameter, SSE uses `text/event-stream` with
the same optional charset, and Magpie uses parameter-free `audio/wav`. Any
other parameter, charset, media type, or encoding fails before representation
parsing. Transfer framing may be length, valid chunked, or close-delimited for
terminal nonalternate success, but the configured decoded-body bound and
complete EOF/chunk terminator are mandatory.

| ID / public route | Exact upstream POST | Model handling | Accept | Async | deadline / response / in-flight per key-profile |
| --- | --- | --- | --- | --- | --- |
| `z-ai/glm-5.2` / chat | `https://integrate.api.nvidia.com:443/v1/chat/completions` | keep exact | JSON or SSE | no | 60 s or 30 min stream / 32 MiB or 256 MiB / 8 |
| `microsoft/phi-4-multimodal-instruct` / chat | `https://integrate.api.nvidia.com:443/v1/chat/completions` | keep exact | JSON or SSE | nonstream 202 only | 60 s total or 30 min stream / 32 MiB or 256 MiB / 4 |
| `nvidia/vila` / chat | `https://ai.api.nvidia.com:443/v1/vlm/nvidia/vila` | keep exact | JSON or SSE | nonstream 202 only | 60 s total or 30 min stream / 32 MiB or 256 MiB / 2 |
| `nvidia/nvclip` / embeddings | `https://integrate.api.nvidia.com:443/v1/embeddings` | keep exact | JSON | no | 60 s / 32 MiB / 4 |
| `black-forest-labs/flux.1-kontext-dev` / images | `https://ai.api.nvidia.com:443/v1/genai/black-forest-labs/flux.1-kontext-dev` | remove public model | JSON | no | 180 s / 32 MiB / 1 |
| `stabilityai/stable-video-diffusion` / native + OpenAI alias | `https://ai.api.nvidia.com:443/v1/genai/stabilityai/stable-video-diffusion` | remove public model; alias maps `input_reference` to native `image` | JSON | no | 180 s / 128 MiB / 1 |
| `nvidia/magpie-tts-multilingual` / speech | `https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com:443/v1/audio/synthesize` | multipart mapping | WAV | no | 60 s / 32 MiB / 2 |

Each table deadline is one monotonic total deadline starting immediately after
the durable attempt reservation commits and before DNS resolution. It includes
DNS, TCP, TLS, request serialization/write, headers, every response byte,
Phi/VILA polling delay/request, parsing, and terminal reconciliation. The
30-minute value replaces 60 seconds for an accepted stream; it does not start a
second clock at first chunk. Asset create/upload/delete reconciliation has the
separate section 6.3 bounds but cannot extend the selected attempt's wire
success deadline. Equality is expired. One stored `deadline_at_mono` is sampled
by every task; no task derives its own wall-clock timeout.

All POSTs send exact `Content-Type: application/json` except Magpie
`multipart/form-data` with a generated boundary. JSON routes send exact
`Accept: application/json` or `text/event-stream`. The SVD documentation also
shows a function-level pexec example with an unresolved environment function
ID; that unprovisioned path is not the selected hosted descriptor. Direct FLUX
and SVD OpenAPI list only 200/422, so 202 from those routes is protocol error.
GLM and Magpie have no approved 202 contract. Only nonstream Phi and VILA poll.

The Phi snapshot's OpenAPI path names
`/v1/microsoft/phi-4-multimodal-instruct`, while every frozen Python, Node, and
curl deployment template selects `/v1/chat/completions`. V3 resolves the source
conflict in favor of the deployed templates: it sends the exact public model in
the JSON body to `/v1/chat/completions`, applies the snapshot's
`NVCF-INPUT-ASSET-REFERENCES` header and JSON/SSE schemas to that operation,
and permits 202 only for `stream:false`. The model-specific path is never
called. Both fresh-key live probes must confirm this resolution; disagreement
is a design-contract failure, not runtime endpoint fallback.

### 6.2 Exact modality representations

GLM content is string text only. Public media is represented as structured
parts; HTML media tags and remote HTTP/HTTPS URLs are rejected. Data URLs use
canonical padded RFC 4648 base64 with no whitespace, decode fully, match the
declared media magic, and are never logged.

String/media union classification is prefix-first and common to every profile.
Before deciding that a union string is text, the detector examines the literal
ASCII prefix and a detection-only, single-pass `%HH` decoding of at most the
first 24 source bytes. The latter accepts only complete hexadecimal triplets,
does not treat `+` specially, never becomes the parsed value, and exists solely
to prevent an encoded URL/media scheme from falling through as text. A decoded
ASCII-case-insensitive `http://|https://` prefix is a remote-source lookalike;
a decoded ASCII-case-insensitive `data:` prefix is a media lookalike. Only a
literal exact lowercase `data:` then enters the actual data-URL parser:

| First matching lexical condition | Result before routing |
| --- | --- |
| structured media field or text/media union starts literal or detection-decoded `http://` or `https://` (ASCII case-insensitive) | 400 `unsupported_media_source` |
| string starts exact lowercase `data:` | media candidate; it is never reinterpreted as text after a parse failure |
| string has a mixed-case or detection-decoded `data:` lookalike but not literal lowercase `data:` | 400 `unsupported_modality`; never text |
| `data:` has unsupported/malformed type, parameters other than one lowercase `base64`, or a modality disallowed for that profile | 400 `unsupported_modality` |
| supported MIME/prefix but noncanonical/invalid base64, empty decode, declared MIME/magic mismatch, truncated/polyglot media, or media metadata out of range | 422 `invalid_request` |
| no URL/data prefix in a field whose union permits text | ordinary text, subject to its text bounds |
| no URL/data prefix in a media-only field | 400 `unsupported_media_source` |

Thus NVCLIP `data:image/png;base64,@@@@` is 422,
PNG-declared JPEG bytes are 422, `data:image/gif;base64,...` is 400
`unsupported_modality`, and `https://...` is 400
`unsupported_media_source`; `DATA:image/png...`, `d%61ta:image/png...`, and
`h%74tps://...` are also rejected before text validation. None can silently
become embedding text. Scheme, MIME, and parameter matching is byte-exact as
stated—mixed-case or percent-encoded variants are rejected lookalikes, not
aliases.

Phi/VILA messages are arrays of 1..64 exact `{role,content}` objects with roles
`user|assistant` only. They start with `user`, strictly alternate, and end with
`user`; a single user message is valid. `system`, consecutive equal roles, a
leading assistant, or a trailing assistant is a local 422 and is never
transformed. Assistant content is a nonempty string, and media parts occur only
in user content. User content is either a nonempty string or an array of 1..32
exact parts:
`{type:"text",text}`, `{type:"image_url",image_url:{url}}`, Phi-only
`{type:"input_audio",input_audio:{data,format}}` or
`{type:"audio_url",audio_url:{url}}`, and VILA-only
`{type:"video_url",video_url:{url}}`. Text is 1..100,000 scalars and at least
one text part is required. No `detail`, name, tool, or unknown member is
accepted. The adapter serializes only the validated representation required by
the selected NVIDIA schema; clients can never inject provider HTML tags.

- Phi image part is `image_url.url` with `data:image/png;base64,...` or
  `data:image/jpeg;base64,...`. Public audio accepts either
  `input_audio:{data,format:"wav"|"mp3"}` or
  `audio_url.url` with `data:audio/wav;base64,...` or
  `data:audio/mpeg;base64,...`. `input_audio.data` is raw canonical padded RFC
  4648 base64 with no prefix, whitespace, URL-safe alphabet, or empty payload;
  format `wav` requires RIFF/WAVE PCM magic and the WAV validator, while `mp3`
  requires complete MPEG Layer III frames and the MP3 validator. The adapter
  normalizes it to exact `audio_url.url` using respectively
  `data:audio/wav;base64,` or `data:audio/mpeg;base64,` plus the unchanged
  canonical payload. The URL form passes the same bytes/format validators. This
  explicitly resolves the source-description mention of `input_audio` against
  the OpenAPI union, which contains `audio_url` only.
- VILA accepts the same image data URLs and `video_url.url` containing only
  `data:video/mp4;base64,...`; it never accepts a general URL. Before both the
  inline aggregate check and the asset-plan check, one request has exactly one
  of these cardinalities: one video, or one through ten images, never a mix;
  eleven images, two videos, zero media, or video-plus-image is local 422 and
  cannot enter an asset reservation. MP4 is parsed before routing:
  one unencrypted H.264/AVC `avc1|avc3` video track, dimensions 16..4,096 in
  each axis, duration greater than zero and at most 120 seconds, and at most one
  optional AAC-LC audio track are accepted. H.265/HEVC and every other codec
  are rejected in v3.
- NVCLIP input is one string or up to 64 strings; each is plain text or a PNG/
  JPEG data URL. Mixed text and image entries are allowed.
- FLUX v3 advertises text-to-image only. Its preview schema requires
  `image:null`; arbitrary image editing and the documented `example_id` preview
  samples are not public features. The gateway sends `image:null`, `samples:1`,
  and a validated prompt/size mapping.
- SVD accepts one PNG/JPEG data URL whose base64 payload is at most 200,000
  characters and whose decoded image is 256..2,048 pixels on each axis with at
  most 2,097,152 pixels. Larger input is 413 before key selection because the
  selected direct OpenAPI has no closed asset-header contract. A successful
  returned MP4 has exactly one unencrypted `avc1|avc3|hvc1|hev1` video track,
  no audio or other timed track, and the closed output limits below; final live
  QA decodes first and last frames with the pinned verifier. No codec conversion
  occurs in gateway.
- Magpie accepts text and a live-probed common voice and returns WAV.

Generated media is validated before terminal success. FLUX must contain exactly
one schema-selected artifact whose canonical padded base64 decodes to a
nonanimated JPEG of exactly 1024 by 1024 pixels satisfying the same
dimension/pixel/polyglot checks and at
most 24,117,248 decoded bytes; the gateway re-encodes those exact bytes as
canonical padded base64 in the OpenAI row. SVD must contain exactly the closed
single video artifact and the MP4 track/frame/duration contract above. Width and
height are each 256..2,048, pixels per frame are at most 2,097,152, decoded
frame count is 14..300, exact rational frame rate is in `[1,120]`, exact
duration is 500..30,000 milliseconds, and checked
`width * height * frame_count <= 268435456`. Every product and comparison uses
checked `u128`; overflow is protocol error before allocation. Track duration is
the positive rational `duration_ticks/timescale`, frame rate is
`frame_count*timescale/duration_ticks`, and range checks use cross
multiplication before display rounding. The projected positive integer values
are ties-up roundings
`duration_ms=floor((2000*duration_ticks+timescale)/(2*timescale))` and
`milli_fps=floor((2000*frame_count*timescale+duration_ticks)/
(2*duration_ticks))`. Zero timescale/ticks, contradictory edit/sample tables,
or a parsed and decoded frame-count mismatch fails. Its
typed `VideoResponse` is reserialized in schema field order rather than raw
provider byte order. Magpie must be `audio/wav` and pass the closed RIFF/PCM,
44,100-Hz/one-channel/16-bit integer PCM and complete-body checks with duration
greater than zero and at most 300 seconds. A mismatched
MIME, extra artifact, noncanonical base64, trailing data, oversized decode, or
invalid media is `upstream_protocol_error` or
`upstream_response_too_large` according to the already selected bound and is
never returned partially.

The fixed live Magpie proof sentence must additionally produce at least 250
milliseconds of PCM, absolute peak sample at least 256, and integer RMS over
all samples at least 64; shorter or effectively silent proof audio fails the
case. General user speech retains the positive-duration rule because valid
short input may be shorter than the proof phrase. Product SVD minima are a
conservative output-quality gate, not a claim that the source OpenAPI declares
them. The frozen NVCLIP schema caps vectors at 1,024 and its official response
template contains two 1,024-element vectors; v3 deliberately closes that
model-specific output to exactly 1,024 and both-key live proof must confirm it.

For Phi, VILA, and NVCLIP the conservative inline budget is common and exact:
the sum of base64 payload characters, excluding every `data:<mime>;base64,`
prefix, is at most 179,999. Phi `input_audio.data` contributes its entire raw
base64 string; its normalized prefix contributes zero. Every item also
contributes its fully decoded byte length once to the decoded aggregate, so the
same audio cannot evade either bound by representation choice. Encounter order
is message index then user-part index for Phi/VILA and input-array index for
NVCLIP; a scalar NVCLIP input is index zero. If the aggregate is at most
179,999, every media item remains
inline. If it is 180,000 or more, every media item in that encounter order is
assetized—there is no subset/size heuristic—and receives zero-based ordinal in
that order. If this all-asset plan has more than ten media items, any decoded
item exceeds 16 MiB, or aggregate decoded inline-plus-asset bytes exceed 23 MiB
(24,117,248 bytes), the whole request is 413 before reservation and creates no
asset. This deliberately selects the lower official 180,000 guidance even
where a schema permits 200,000 or 204,800. The complete
pre-rewrite JSON body is at most 32 MiB (33,554,432 bytes), so base64 expansion
and JSON overhead cannot claim a 32 MiB decoded payload. GLM remains 2 MiB;
nesting is at most 32. Image magic and dimensions, WAV RIFF/PCM or complete MP3
frames, MP4 boxes/codecs, aggregate counts, and both raw-body and decoded limits
are validated before reservation. No numeric coercion, duplicate keys,
nonfinite value, invalid Unicode, or overlong string is accepted.

The fake oracle fixes aggregate payload boundaries 179,999/180,000, one/ten/
eleven media items, mixed text/media ordering, and permutations with equal
sizes. It asserts the exact ordinal, description, body replacement, and header
bytes; two valid implementations cannot choose different asset subsets.

PNG/JPEG are nonanimated, 1..8,192 pixels per axis, and at most 33,554,432
pixels after header/decode validation. WAV is uncompressed integer PCM, one or
two channels, 8,000..48,000 Hz, 16 or 24 bit; MP3 is complete MPEG Layer III,
one or two channels, 8,000..48,000 Hz. Input-audio duration is greater than zero
and at most 60 seconds. Decoder allocation is charged to the decoded aggregate
before full allocation; truncated, trailing-polyglot, contradictory header,
encrypted, or unsupported-profile media is rejected. Magpie output uses the
separate 300-second rule above.

### 6.3 NVCF asset lifecycle

Asset use is limited to Phi, VILA, and NVCLIP. The already reserved key remains
pinned for the entire create/upload/infer/delete lifecycle.

Persistence is split so cleanup discoveries cannot violate media cardinality.
`upstream_asset_intents` has exactly one row per `(attempt_uuid,ordinal)` with a
zero-based `ordinal` (`0..9`, independent of public attempt ordinal `1|2`),
preallocated row UUID, key UUID, profile ID, canonical MIME, decoded byte count,
raw-media SHA-256, unique description `nblb/<attempt-uuid>/<ordinal>`, section 8
fingerprint, and exact state `planned|create_dispatched|create_unknown|
created_ephemeral|upload_dispatched|uploaded|cleanup_pending|deleted|
revoked_cleanup_closed`. The last is permitted only by the explicit revocation
branch below; it is terminal for pin/count purposes but remains distinguishable
from provider absence-proved `deleted`.
The physical `fingerprint` is raw `BYTEA` SHA-256 and is immutable; its
`asset_intent_fingerprint` formula is stored/read back rather than recomputed
from mutable cleanup fields.
Every later phrase `nondeleted intent count` means states other than these two
terminal states; object rows still require literal `deleted`.
`upstream_asset_objects` has zero or more rows per intent, one per distinct
provider asset UUID, with provenance `response|reconciliation`, section 8
fingerprint, and state `known|cleanup_pending|deleted`. The provider UUID is
stored as PostgreSQL `uuid` and is unique within the key through
`UNIQUE(key_id,provider_asset_id)` plus the composite intent/key FK. The same
named `ENABLE ALWAYS` row guard is installed for both `INSERT` and `UPDATE`;
its insert path rejects noncanonical/unknown provider identity, wrong key,
duplicate provenance, or a mismatched 32-byte fingerprint. The upload URL and
media bytes are never stored.
The raw-media digest is internal ambiguous-commit material and never appears in
DTOs, logs, receipts, or external evidence.

One monotonic logical deadline starts immediately before reservation and covers
reservation, every foreground asset call, inference origin, and any poll: 60
seconds for Phi/VILA/NVCLIP. Each DNS+connect+TLS is clipped to five seconds and
the remaining logical deadline; response headers are clipped to ten seconds and
the remaining deadline. Create response body is at most 64 KiB, list is 32 MiB,
and DELETE response is at most 2 KiB and discarded. Upload connect is five
seconds and its complete send+response uses at most 30 seconds and the remaining
logical deadline. Cleanup after the public terminal uses its worker budget below
and never extends or reverses that terminal. Foreground intents run strictly in
ordinal encounter order with at most one create or upload request in flight;
the next ordinal cannot dispatch until the prior ordinal is durably `uploaded`.

The state machine is exact:

The only database transitions accepted by `nblb_guard_asset_intent_update`
are `planned->create_dispatched`, `create_dispatched->create_unknown`,
`create_dispatched->created_ephemeral`, `create_unknown->create_unknown`,
`created_ephemeral->upload_dispatched`, `upload_dispatched->uploaded`,
`created_ephemeral->cleanup_pending`, `upload_dispatched->cleanup_pending`,
`uploaded->cleanup_pending`, `create_unknown->cleanup_pending`,
`cleanup_pending->deleted`, `planned->deleted`, and
`create_unknown->revoked_cleanup_closed`; the guard also permits an idempotent
write of the same row bytes and rejects every other edge. The only object
transitions accepted by `nblb_guard_asset_object_update` are
`known->cleanup_pending`, `cleanup_pending->deleted`, and an idempotent same-
bytes write. Object `INSERT` is accepted only for a canonical intent/key pair,
provider UUID, provenance, fingerprint, and initial state `known` or
`cleanup_pending`; intent `INSERT` is accepted only for a preallocated
`planned` row with the exact description, ordinal, MIME, digest, and key.
These guards are `ENABLE ALWAYS` and are exercised by the migration fixture
for every listed edge and every rejected cross-edge before any provider call.

1. Before any create network, the intent is committed `planned`, then a second
   durable transaction commits `create_dispatched`. Network is forbidden until
   that readback is exact. Create sends one POST to
   `https://api.nvcf.nvidia.com:443/v2/nvcf/assets` with the same bearer, exact
   JSON `{"contentType":"<canonical MIME>","description":"<unique description>"}`,
   and `Accept: application/json`. A connect/TLS failure proven before the
   first request byte changes that intent directly to `deleted` with zero object
   rows and may contribute to the aggregate alternate proof below. Once any
   request byte can have been sent, every
   status-less result, non-200, oversized body, invalid body, or crash remains
   side-effect-ambiguous and goes through reconciliation; no create POST is
   ever resent.
2. A valid 200 contains UUID `assetId`, identical `contentType`, and one
   presigned `uploadUrl`. The URL is accepted only when its total ASCII length is
   1..8,192, scheme is `https`, port is absent or 443, host is a non-IP DNS name
   of at most 253 ASCII bytes, and userinfo and fragment are absent. On each
   connect, all A/AAAA answers must be globally routable unicast; private,
   loopback, link-local, multicast, documentation, benchmark, unspecified, and
   metadata ranges fail. Only after response fields and URL validate is its
   response object inserted `known` and the intent committed
   `created_ephemeral`, while that request task alone retains the URL in memory.
   Commit ambiguity is read back by intent fingerprint and object fingerprint;
   an absent row may commit only those already observed values and never
   repeats network. Before PUT, the intent durably commits
   `upload_dispatched`. DNS answers are pinned for that one TLS connection.
   The URL can drive only one PUT; clients cannot supply it. A fresh no-proxy/
   no-redirect/no-cookie connection sends no Authorization and only exact
   `content-type` and `x-amz-meta-nvcf-asset-description`. Decoded bytes stream
   once. Upload success is status 200 with exactly one `Content-Length: 0`, no
   `Transfer-Encoding`, `Content-Encoding` absent or exactly one OWS-trimmed
   ASCII-case-insensitive `identity` field, and no yielded body. Repeated,
   comma-joined, empty, or other encoding fields are not success;
   the dedicated connection is closed and the intent commits `uploaded`.
   Chunked, absent-length, nonempty, oversized, incomplete, or encoded upload
   responses are not success. Every other upload outcome,
   including a pre-byte failure, moves the intent and all known objects to
   `cleanup_pending`; the PUT is never retried because its URL is not durable.
3. Only when every intent is `uploaded` and has exactly one response-proven
   `known` object does inference use `data:<mime>;asset_id,<uuid>` and
   `NVCF-INPUT-ASSET-REFERENCES`. VILA also sends
   `NVCF-FUNCTION-ASSET-IDS`; Phi/NVCLIP do not. Each UUID is canonical
   lowercase text. Both headers, where applicable, are the same comma-separated
   ordinal-order UUID list with no OWS; every body reference uses its
   corresponding ordinal UUID. Startup never resumes inference
   from `created_ephemeral`, `upload_dispatched`, or `uploaded`; an old-epoch
   attempt is abandoned and all of its objects become cleanup-only.
4. Cleanup first commits each object `cleanup_pending`, then sends DELETE to
   `https://api.nvcf.nvidia.com:443/v2/nvcf/assets/<uuid>` with the same bearer.
   204 and 404 prove absence and commit that object `deleted`. The parent intent
   becomes `deleted` only when every child is deleted and create was never
   ambiguous. A `create_unknown` parent never transitions to `deleted` from
   list absence.

Reconciliation applies to `create_dispatched|create_unknown` and first changes
`create_dispatched` to `create_unknown`. It performs bounded GETs to
`https://api.nvcf.nvidia.com:443/v2/nvcf/assets` at 0, 1, 2, and 4 seconds with
the same key, a 32 MiB limit, and the frozen strict
`{assets:[{assetId,description,contentType}]}` schema. UUID salvage from an
invalid create 200 is deliberately narrow: only a fully received body within
64 KiB that is valid UTF-8, one complete duplicate-key-free JSON object, and
has exactly one top-level `assetId` member whose value is a canonical lowercase
UUID contributes that UUID. Nested members are ignored; duplicate keys,
trailing bytes, a nonobject, a noncanonical UUID, or more than one top-level
candidate contributes none. A salvaged UUID is first inserted as a
`reconciliation` cleanup object; the list result is then unioned by UUID with
it. Every list row matching both unique description and MIME becomes one
`reconciliation` object in `cleanup_pending`, whether there is one or many;
list recovery never becomes `created_ephemeral` and never attempts upload
because no upload URL exists. All matches are deleted. A zero-match list is only
an observation: the frozen source gives no create-to-list visibility bound, so
zero never changes `create_unknown`, never authorizes alternate, and never
permits key retirement. The worker repeats one four-observation cycle at most
once per 15 minutes and never sends POST. A late match, including one first
visible after earlier zero cycles, becomes cleanup work; the parent remains
`create_unknown` owner attention because a still-later duplicate cannot be
excluded. More than one match raises attention and all discovered UUIDs are
deleted, but absence is never fabricated.

For a create whose request byte was sent, valid-200-plus-valid-upload is the
only path that may continue preparation. Every non-200, malformed/oversized
200, invalid upload URL, or status-less result ultimately returns
`upstream_asset_error` after the bounded foreground observation/cleanup cycle;
durable `create_unknown` watching may continue. Its provider status never
maps to an ordinary inference error and never directly changes key health. An
ambiguous create never alternates. A valid create whose later upload failed may
alternate only under the same aggregate rule. On any foreground preparation
failure, every later still-`planned` intent is durably changed to `deleted`
without network. Alternate is allowed only when every intent for the attempt is
`deleted`, no intent is or ever was `create_unknown`, every object created by an
earlier ordinal has a received DELETE 204/404 absence proof, zero cleanup work
remains, and the same logical deadline still has time for one alternate
attempt. Thus a pre-byte failure on ordinal two does not erase ordinal one's
uploaded object. Otherwise the durable cleanup worker finishes independently
and the request returns `upstream_asset_error` without alternate.

When preparation reached `uploaded` and inference later returns an otherwise
alternate-eligible body-absence-proved 401, 402, 403, or 429, alternate remains
conditional on synchronous cleanup. The supervisor commits every object
`cleanup_pending`, sends DELETE strictly in ordinal order within the remaining
logical deadline, and may reserve the other key only after every object has a
received 204/404 and every parent is `deleted`. The first attempt retains its
original auth/credits/permission/rate terminal class for health evidence. If
any DELETE response is lost, invalid, non-204/404, or misses the deadline,
cleanup safety takes terminal precedence: no alternate occurs, the public
result is `upstream_asset_error`, and the worker continues. Therefore the
fixtures are exact: `two uploaded -> empty 401 -> two absence-proved deletes`
may alternate; `two uploaded -> empty 429 -> one delete unknown` cannot and
returns asset error; `ordinal 0 uploaded -> ordinal 1 pre-request-byte create
failure` deletes ordinal 0, marks ordinal 1 deleted-without-object, and may
alternate only after those facts and remaining deadline are proved.

A lost DELETE response for a known UUID is not inferred from list absence: the
worker repeats the idempotent DELETE until a received 204/404 proves absence.
Cancellation, crash, and cleanup failure are resumed by startup and a bounded
worker with the same key. Each run claims at most 16 objects, has two concurrent
calls, and lasts at most 30 seconds. Per-object retry `n` starts at zero and
schedules full jitter uniformly in
`[0,min(900s,2s*2^min(n,9))]`; retries have no count limit, stay durable, and
never exceed one call per object per run. Old-epoch `planned` proves
network was forbidden and becomes deleted-without-object; old-epoch
`create_dispatched` never makes that assumption and reconciles. Alternate
preparation is permanently forbidden for `create_unknown`. An asset is never
shared between credentials. A key with a live
pin or any nondeleted intent/object stays `retirement_pending` with ciphertext
intact. Cleanup failure after successful inference does not rewrite public
success; it creates owner attention and blocks retirement.

A `create_unknown` watcher is not allowed to monopolize onboarding forever.
After its bounded foreground cycle, the probe terminal is `failed` with
`cleanup_state:"unknown"`, `next_action:"quarantine_staged"`, exact next retry,
and owning evidence event; the durable watcher continues independently. The
operator's idempotent staged DELETE may atomically move that key to
`retirement_pending` instead of fabricating a `retired` tombstone, retain its
ciphertext solely for authenticated list/delete cleanup, release the operation
pin and the single staged position, and record `asset_quarantined`. It never
changes an assigned key or asserts absence. Later discoveries remain bound to
the same key/description; only received DELETE 204/404 for every known object
plus an explicit provider credential revocation receipt can terminalize an
unbounded unknown parent as `revoked_cleanup_closed`. That terminal means no
further authenticated discovery is possible because the key is revoked, not
that list absence proved no historical object. The audit rows remain, the key
may then tombstone, and fresh onboarding is independent throughout.

The explicit closure command is
`nblb-ops confirm-key-revocation <internal-key-uuid> <receipt.json>` under the
operations lock. Its receipt is the same canonical timestamp/state/evidence-
digest discipline as section 1.1 but exactly
`{"version":1,"key_id":UUID,"state":"revoked"|"deleted",
"observed_at":Timestamp,"evidence_sha256":LowerHex64}` and contains no
credential value or free text. It is accepted only for the exact
retirement-pending key after all discovered object rows are literal `deleted`;
it writes the revocation event and `revoked_cleanup_closed` parent in one
transaction. Receipt loss is reconciled by key/event/digest and never repeats a
provider action. This is a manual NVIDIA-control-plane evidence gate, not a
provider client hidden in cleanup.

### 6.4 Phi/VILA 202 polling

An origin 202 must contain exactly one `NVCF-REQID`. After parser OWS trimming,
the value is exactly 36 ASCII bytes matching canonical lowercase UUID text
`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`, matching the
frozen `format:uuid,maxLength:36` contract. Repeated, comma-joined, uppercase,
whitespace-bearing, slash/query/percent/control values fail. A duplicate header
line fails even when values are byte-identical. Every 202 requires
`Content-Encoding` absent or exactly one OWS-trimmed
ASCII-case-insensitive `identity`; repeated, comma-joined, empty, or any other
value is protocol error. Its body is bounded to 2 KiB and must
be empty or a JSON object with zero members; an empty body permits absent
`Content-Type` or exact `application/json` with optional case-insensitive
`charset=utf-8`, while a JSON body requires that JSON media type. Duplicate or
other parameters, a non-UTF-8 charset, and every other media type fail. Poll URL is
constructed only as:

```text
https://api.nvcf.nvidia.com:443/v2/nvcf/pexec/status/<validated-id>
```

An origin 202 is valid only when the public request had `stream:false`; a 202
for `stream:true` is `upstream_protocol_error`, is never polled, and never
alternates. Poll GET uses the same key, `Accept: application/json`, a new H1
connection, and no redirects,
cookies, proxy, retries, or alternate. Every poll 202 body has the same 2 KiB
empty-or-exact-empty-object contract as the origin 202. It may omit the ID or
repeat the one byte-identical origin ID; any other ID fails. Poll index `n` starts at zero,
`cap=min(2s,0.25s*2^n)`, and equal jitter is uniform in `[cap/2,cap]`, yielding
caps 0.25, 0.5, 1, then 2 seconds. One monotonic 60-second deadline starts
before origin connect and covers origin send/body close, sleeps, every poll,
and terminal body close. Poll 200 must be the closed JSON success within the
profile's 32 MiB response bound, with one `Content-Type: application/json`
using the same optional UTF-8 parameter and the same exact absent/single-
identity content-encoding rule;
all other
terminal statuses map without failover. A direct origin 200 SSE path uses the
normal 30-minute stream limit and is never converted to polling. The complete
nonstream origin-plus-poll path stays inside 60 seconds. The 302/result and SSE
poll allowlists are empty.

The origin attempt is durable `started` before POST. Before accepting a valid
202 as progress, one transaction commits exact
`poll_planned{request_id,poll_index:0,deadline_wall,origin_receipt_hash}` and
reads it back; failure emits no wire and never repeats origin. Immediately
before each GET another transaction commits `poll_dispatched(index)`. A live
process may issue that one GET only after readback, then commits its 202/terminal
receipt before sleeping or writing. Startup never resumes provider work for a
client connection that was lost: `started` without `poll_planned`,
`poll_planned`, and `poll_dispatched` without terminal all become
`abandoned_after_restart`, emit no public wire, apply no success/health clear,
release the live pin after asset reconciliation, and never redispatch POST or
GET. The fake matrix injects crashes immediately after origin 202 receipt,
after `poll_planned`, and after `poll_dispatched`, and requires those exact
rows and zero replay.

### 6.5 SSE supervision and terminal precedence

SSE accepts LF/CRLF and complete frames up to 1 MiB. Data lines join with one
LF per the SSE algorithm. Comments are validated UTF-8 then discarded. The last
`event`/`id`/`retry` field wins upstream; event is 0..64 printable ASCII bytes,
id is 0..128 UTF-8 bytes without NUL, and retry is 1..6 decimal digits. Those
three fields are never forwarded. A frame without data emits nothing. A data
payload is either exact `[DONE]` or one closed chunk JSON object. The gateway
serializes each validated chunk from its typed struct in schema field order as
exact UTF-8 `data: <minified-json>\n\n`; it never relays provider whitespace,
field order, comments, or metadata. An upstream `[DONE]` only proposes success;
the gateway emits its own exact `data: [DONE]\n\n` after durable success commit.
A tracked supervisor buffers exactly one complete canonical frame before
response start, then applies backpressure with at most one frame pending.
For SSE specifically, receiving any upstream body byte closes replay
eligibility. Actix Web does not acknowledge socket writes to `MessageBody`; the
separately named conservative visibility boundary is therefore
`downstream_handoff`, when the tracked body returns a canonical frame as
`Poll::Ready(Some(Ok(Bytes)))` to the product-owned adapter/Writer. It is treated as visible even if the
kernel later rejects the write, so it can never broaden replay/failover.

The cross-frame grammar is closed. At least one JSON chunk is required. Every
chunk has the same nonempty `id`, exact requested `model`, safe-integer
`created`, `object:"chat.completion.chunk"`, and exactly one choice at index
zero. The first semantic delta contains only `role:"assistant"` plus permitted
initial content/reasoning/tool-call fields; role occurs exactly once and no
content-bearing delta precedes it. Empty deltas are allowed only after that
role. Exactly one chunk has nonnull allowlisted `finish_reason`; it is the last
JSON chunk, its delta is empty, and no JSON/content follows it. EOF before that
chunk, `[DONE]` before it, `[DONE]` with zero chunks, missing `[DONE]`, a second
`[DONE]`, or any byte/frame after `[DONE]` is protocol error. The gateway never
forwards a proposed `[DONE]` until the complete grammar and durable success
commit pass.

Streaming tool calls are assembled by zero-based choice-local index. Indices
first appear contiguously. The first fragment for each index contains canonical
ID, `type:"function"`, and a function name present in the request; later
fragments may contain only the same index and argument text and may not repeat
or change ID/type/name. Argument fragments append in arrival order to at most
64 KiB and at terminal form one complete duplicate-key-free JSON value of depth
at most 16. IDs are unique across the response and disjoint from every
assistant tool-call ID in the request history.
`finish_reason:"tool_calls"` requires at least one complete call and every
complete returned call requires that exact reason; every other reason requires
zero returned calls, not merely no incomplete call. A fragment gap,
mutation, overflow, invalid final JSON, terminal content, or reason/call
mismatch is protocol error.

JSON, binary WAV, generated image/video, assetized inference, and 202/poll use
the same supervised terminal candidates and priority below. JSON/poll
representations are parsed to completion; binary/media outputs are validated to
their exact spool quota and streamed from the sealed spool. In both cases
success is committed before any downstream header/body. An Actix request-future guard or tracked body
Drop that still owns cancellation before seal stops further provider/poll work, forbids alternate
after dispatch, completes required asset reconciliation, commits cancellation,
and emits nothing. A raw socket close that Actix does not surface is never
invented as an event: the detached supervisor continues to a durable terminal
and later body Drop is delivery-only. A committed
buffered success is not rewritten by a later Actix body drop; that becomes a
separate `downstream_delivery_abandoned` receipt. SSE adds only its
bounded frame-delivery grammar to this common arbitration.

Request lifecycle is closed even when no provider attempt exists:

```text
RequestLifecycle=
 {state:"pre_admission",request_id:UUID,admission_id:null,attempt_id:null,
  body_budget_lease:null,owner:"handler_owned"|"body_owned"}
|{state:"admitted_no_attempt",request_id:UUID,admission_id:UUID,
  attempt_id:null,body_budget_lease:UUID|null,
  terminal:"method"|"media"|"framing"|"schema"|"body_timeout"|"budget"|"cancelled",
  owner:"handler_owned"|"body_owned"}
|{state:"attempt_owned",request_id:UUID,admission_id:UUID,
  attempt_id:UUID,body_budget_lease:UUID,owner:"handler_owned"|"body_owned"}
|{state:"detached_no_attempt",request_id:UUID,admission_id:UUID,
  attempt_id:null,body_budget_lease:UUID|null,owner:"detached",
  terminal:"method"|"media"|"framing"|"schema"|"body_timeout"|"budget"|"cancelled"}
|{state:"detached",request_id:UUID,admission_id:UUID,attempt_id:UUID,
  body_budget_lease:UUID,owner:"detached"}
TrackedBody=
 {kind:"no_attempt",lifecycle:RequestLifecycle,
  delivery:"not_applicable"|"body_complete"|"abandoned",
  terminal_receipt_sha256:LowerHex64|null}
|{kind:"attempt",lifecycle:RequestLifecycle,delivery:AttemptDelivery}
```

The no-attempt receipt is durable in the append-only `request_terminals` table:
`(id UUID PRIMARY KEY, request_id UUID UNIQUE, admission_id UUID NOT NULL,
runtime_epoch_id UUID NOT NULL, class TEXT, lifecycle_state TEXT,
status_code INTEGER, response_shape_sha256 BYTEA NOT NULL,
terminal_receipt_sha256 BYTEA NOT NULL, budget_lease_id UUID NULL,
delivery_state TEXT NOT NULL, handoff_at TIMESTAMPTZ NULL,
completed_at TIMESTAMPTZ NULL, abandoned_at TIMESTAMPTZ NULL,
admission_released_at TIMESTAMPTZ NOT NULL, committed_at TIMESTAMPTZ NOT NULL,
event_id UUID NULL REFERENCES events(id) DEFERRABLE INITIALLY DEFERRED)`. Its
closed class is exactly `method|media|framing|schema|body_timeout|budget|
cancelled`; `budget_lease_id IS NULL` exactly for `class='budget'`, and
`attempt_id` is structurally absent. Status/shape are fixed to the mapped
no-attempt result; delivery timestamps obey the same closed
`not_applicable|body_complete|abandoned` union and the lease/admission release
timestamp is written in the same transaction. INSERT is the only mutation; the
immutable guard and catalog manifest reject UPDATE/DELETE. A no-attempt body
can be `body_complete|abandoned` only after this row and its receipt are
durable; `terminal_receipt_sha256` is null only for the ephemeral
`pre_admission` branch and nonnull for every admitted no-attempt branch.
Pre-admission failures have no row, method/media/schema/framing/body failures
have a nonnull budget lease, and budget-unavailable has null. A
`detached_no_attempt` owner is allowed only for an already durable row and
never creates an upstream attempt. Restart reconciliation verifies the row
rather than reconstructing it from a counter.

`pre_admission` covers visibility/authority/auth failures and owns no admission,
counter, body budget, or upstream attempt. After auth/scope the credential
counter and an admission row are committed; method, media, framing, schema,
budget, and body failures therefore become `admitted_no_attempt`, release that
exact admission/budget lease once, and write the fixed no-attempt receipt without
health/cursor/provider effects. Only selection creates `attempt_owned` and an
`AttemptDelivery`; a supervisor transfer creates `detached`. No-attempt body
Drop is delivery-only and cannot publish a consumer-drop candidate or alternate.
The handler/response fixtures enumerate every union branch, including admin
202 and timeout, and assert one counter/admission/lease release.

Handler completion is not consumer cancellation. Each dispatcher future starts
with a `RequestLifecycleGuard` whose closed owner field is `handler_owned`; the
only legal transitions are `handler_owned -> body_owned` at the single
non-awaiting handoff, or `handler_owned -> detached` when the supervisor takes
the request. A body Drop may transition `body_owned -> detached` only once;
there is no implicit owner state outside the union above. Each dispatcher
After mapping the
`HttpOutcome`, after constructing the complete `HttpResponse<TrackedBody>`, and
immediately before that `HttpResponse` is returned from the handler future, it
performs under one non-awaiting `request_lifecycle_mutex` an ownership
transition `handler_owned -> body_owned`; the mutex is admission-scoped for
`pre_admission|admitted_no_attempt` and attempt-scoped for
`attempt_owned|detached`. This arms the body's Drop hook and disarms the
handler guard atomically. Normal Actix handler-future destruction after return
therefore does nothing, including while SSE remains unsealed. Cancellation or
future Drop before that transfer drops a still-handler-owned guard: it publishes
the consumer-drop candidate only for `attempt_owned`, while
`pre_admission|admitted_no_attempt` records the closed no-attempt delivery and
releases its lease. Cancellation during/after transfer either sees the old guard
owner or drops the newly armed body, never both and never neither.
Buffered responses make the same transfer after their terminal commit, so their
body Drop is delivery-only. `downstream_handoff` is the first canonical frame
returned by the product-owned adapter/`nblb-h1::Writer` poll to the socket
writer; it is not an Actix H1 dispatcher acknowledgement and is not the
lifecycle-owner transfer point. A raw Writer fixture binds that first frame to
the handoff receipt. Concurrency fixtures force every side of this transition and a
normal handler return with a long-lived SSE body.

An aborting panic is not a Drop path: `panic=abort` runs no guard destructor and
the design claims no cancellation candidate from it. The process dies; on the
next start, the durable runtime epoch and attempt/asset/poll reconciliation
below classify every nonterminal row without replay and release counters only
after that terminal commit. Tests separately cover ordinary future/body Drop
and a subprocess abort; they never expect stack-unwind behavior from the latter.

Exactly one durable terminal is chosen in this priority:

```text
deadline > downstream consumer dropped > ancestor cancellation >
response-too-large > protocol error >
upstream application failure > upstream network failure > upstream success
```

`upstream application failure` is a validated terminal error/content-policy
finish reason; `protocol error` includes malformed UTF-8/SSE/JSON/chunk/order or
unknown finish reason. Selection has an atomic pre-commit **terminal seal**;
the database transaction is never the mechanism that closes candidate intake.
The per-attempt arbiter is `open | sealed{seal_uuid,class,
candidate_digest}` behind one nonpoisoning mutex. Every deadline, tracked Actix
currently owning request/body-drop hook, ancestor cancellation, provider reader/poller, size/parser, and
asset supervisor registers as a producer before dispatch and may publish a
candidate only while `open`. No contract claims an unavailable OS-write-success
or OS-error callback from Actix.

`ancestor cancellation` has only two producers: the process-wide graceful-
shutdown token created before Actix workers, and the owning long-operation
cancel token for an admin probe case. Durable intake drain does not cancel an
already admitted public request, and handler/body loss uses the separate
consumer-drop producer. Shutdown publishes ancestor cancellation at its first
monotonic signal observation, stops new admission, and allows the 30-second
grace to persist terminals/cleanup; a second termination signal exits only
after marking dispatched unsealed attempts for restart reconciliation. Probe
Cancel publishes its operation token only after the cancel transaction commits.
No other call site may create this candidate.

Sealing is permitted only after the provider request/body or poll task has been
half-closed in the attempt direction (no further read/poll/request is possible),
required asset reconciliation has reached its selected terminal precondition,
and every data-producing parser/provider/asset task has joined. Edge producers
(deadline, consumer drop, and ancestor cancellation) remain registered
through the critical section. Under the one arbiter lock, sealing samples their
monotonic deadline and flags, drains already queued candidates, chooses the
first class in the priority above, creates one UUID/digest, changes directly to
`sealed`, and disarms producer registrations. There is no await or database I/O
inside that critical section. No later event can change the sealed class.
The terminal insert/update binds the seal UUID, class, candidate digest, attempt
fingerprint, and immutable response-shape evidence. Exact absent readback may
retry only this database terminal write while the same live sealed supervisor
exists; exact committed readback adopts it. Conflict/unknown withdraws readiness
and emits nothing. A process loss before commit follows the existing
`abandoned_after_restart` rule and never reconstructs a seal or provider result.

Buffered JSON/binary/poll paths emit no downstream header or body before seal.
They close the upstream response direction, seal, commit, and only then create
the downstream Actix body. A buffered body drop is therefore always post-seal
delivery-only. SSE may hand off validated nonterminal frames; at most one frame
is returned per Actix poll and the framework's next poll is its only
backpressure signal. The tracked `MessageBody` owns a nonblocking Drop hook: if
dropped before seal, it publishes `downstream consumer dropped`; if dropped after
seal, the supervisor records `downstream_delivery_abandoned` after terminal
commit. The upstream SSE body is closed after the validated terminal chunk and
upstream `[DONE]`; the tracked body returns the gateway's `event:error` or
`[DONE]` only after the sealed DB terminal commit. There is no socket-delivery
settlement oracle. Client receipt in the live harness is the only
delivery-success evidence.

Success, consumer drop, deadline, ancestor cancellation, protocol/application/
network failure, 256 MiB stream limit, and 30-minute limit all commit their
sealed terminal receipt before any terminal wire action. Limits persist as
`upstream_response_too_large` or `upstream_timeout`. `[DONE]` is delivered only
after a committed success; commit ambiguity sends no `[DONE]` and withdraws
readiness. A candidate observed before seal deterministically beats a lower
priority candidate; an event after seal cannot reopen arbitration. A post-seal
body Drop is the separate durable `downstream_delivery_abandoned` receipt and
never rewrites the provider terminal. The tracked supervisor, not Drop itself,
owns both upstream/body lifecycle closures, asset cleanup, live-pin release,
sealing, receipt persistence, and finalization.
The fake gate runs semantically identical streams with different comments,
metadata, CRLF, whitespace, and JSON key order twice and requires identical
canonical downstream bytes and terminal receipts; it also races every adjacent
priority pair ten times.

## 7. Exactly-two-key lifecycle and profile-local routing

Permanent slots are 1 and 2. Upstream key lifecycle is exactly
`staged | assigned | retirement_pending | retired`:

- `staged`: no slot, ciphertext present, disabled, at most one row;
- `assigned`: unique slot 1 or 2, ciphertext present, admin enabled or disabled;
- `retirement_pending`: no slot, ciphertext retained until referencing
  nonterminal/recovery operations, live pins, and asset cleanup counts are
  zero, disabled; more than one cleanup-only row is allowed; and
- `retired`: no slot, ciphertext/nonce/tag absent, disabled, fingerprint-only
  tombstone.

Database checks enforce `(lifecycle='assigned') = (slot_no IS NOT NULL)` and
assigned slots in `1|2`. Fingerprints have an ordinary unique constraint, and
`CONSTRAINT upstream_keys_id_fingerprint_key UNIQUE (id,fingerprint)` exists
even though `id` is the primary key because proof identity uses that exact
composite foreign-key target. Slot
ownership is exactly `CONSTRAINT upstream_keys_slot_no_key UNIQUE (slot_no)
DEFERRABLE INITIALLY IMMEDIATE`, not a partial index; PostgreSQL permits
multiple nulls. One nondeferrable partial unique index on constant `true` for
`lifecycle = 'staged'` permits exactly one candidate at most. Retirement rows
do not consume that position, so an ambiguous provider cleanup cannot deadlock
future onboarding. Replacement locks both rows in UUID network-byte order, executes
`SET CONSTRAINTS upstream_keys_slot_no_key DEFERRED`, updates the candidate from
staged to assigned with the occupied slot first, then updates the old assigned
row to retirement-pending with null slot, and verifies both constraints before
commit. The deferred slot constraint makes the intentional intermediate
duplicate legal; the staged row leaves its unique index before commit. A
retired fingerprint cannot be registered again. All
seven v3 profiles are permanently enabled; there is no profile-enable column or
mutation. `configured_pair` means
both slots reference distinct decryptable assigned keys. A key can be
configured while temporarily disabled or cooling. `eligible_now(key,profile)`
requires assigned, admin enabled, current per-key and pair proof where required,
no credential-global block, no profile quarantine/cooldown, and profile
in-flight below the descriptor limit.

The key-envelope DDL is not left to application convention. The physical column
names are `envelope_version,key_generation_id,ciphertext,nonce,tag`. Named CHECK
`upstream_keys_envelope_all_or_none` is exactly the disjunction
`(all five are NULL) OR (envelope_version=2 AND the other four are NOT NULL)`;
partial envelopes and a null version with bytes cannot exist. Separate named
checks make all-null legal only for `retired`, require
`octet_length(fingerprint)=32`, `octet_length(nonce)=12`,
`octet_length(tag)=16`, and ciphertext length 38..198 when nonnull, and enforce
this exact lifecycle truth table:

```text
staged:            slot null, staged_intent first|second|replacement,
                   target_slot 1|2, enabled false,
                   envelope_version/key_generation/ciphertext/nonce/tag nonnull
assigned:          slot 1|2, staged_intent/target_slot null, enabled true|false,
                   envelope_version/key_generation/ciphertext/nonce/tag nonnull
retirement_pending:slot/staged_intent/target_slot null, enabled false,
                   envelope_version/key_generation/ciphertext/nonce/tag nonnull
retired:           slot/staged_intent/target_slot null, enabled false,
                   envelope_version/key_generation/ciphertext/nonce/tag all null
```

Fingerprint remains nonnull in every lifecycle and unique forever;
`enabled => lifecycle='assigned'`. Downstream credentials require
`octet_length(digest)=32`, `UNIQUE(digest_version,digest)`, version exactly
`downstream_v2|legacy_sha256_v1`, and `(active AND revoked_at IS NULL) OR
(NOT active AND revoked_at IS NOT NULL)`. Legacy version rows are always inactive.
`staged_intent='first'` iff target is 1 and both slots were empty at create;
`second` iff target is 2 and Slot 1 was assigned; `replacement` carries the
literal occupied slot from its creation path. Those immutable fields are bound
by the create operation fingerprint and cleared only on assignment/quarantine;
the server never reconstructs them from current slot count.

Configuration tables are:

```text
routing_configuration(singleton, generation)
vault_key_generations(id UUID PRIMARY KEY, state active|retired,
                      created_at, retired_at nullable, manifest_digest)
vault_key_binding(singleton=true, key_generation_id UUID nullable,
                  salt bytea nullable, verifier bytea nullable,
                  generation FK, all-or-none CHECK, byte lengths)
runtime_epochs(id, boot_id, process_pid, process_start_ticks, started_at,
               ended_at)
profile_routing_state(profile_id, generation, next_slot)
upstream_keys(id, lifecycle, slot_no, staged_intent, target_slot_no, enabled,
              ciphertext envelope, fingerprint,
              global_health_state, next_global_health_seq,
              last_applied_global_health_seq, global_last_failure_at)
key_profile_state(key_id, profile_id, current_proof_revision nullable, entitlement,
                  protocol_state,
                  rate_cooldown_until, transient_cooldown_until,
                  rate_streak, transient_streak, next_health_seq,
                  last_applied_health_seq, in_flight projection)
key_profile_proofs(key_id, profile_id, proof_revision,
                   credential_fingerprint, profile_contract_digest, probe_suite_digest,
                   voice_map_digest nullable, completed_at)
pair_proofs(profile_id, pair_proof_revision,
            slot1_key_id, slot1_fingerprint, slot1_key_proof_revision,
            slot2_key_id, slot2_fingerprint, slot2_key_proof_revision,
            profile_contract_digest,
            probe_suite_digest, completed_at)
magpie_key_voice_maps(key_id, profile_id, key_proof_revision,
                      canonical_json, map_digest)
magpie_key_voices(key_id, profile_id, key_proof_revision, voice, locale, kind)
magpie_pair_voice_maps(profile_id, pair_proof_revision,
                       canonical_json, map_digest)
magpie_pair_sources(profile_id, pair_proof_revision, slot_no, key_id,
                    key_profile_id, key_proof_revision, key_map_digest)
magpie_pair_voices(profile_id, pair_proof_revision, voice, locale, kind)
published_magpie_map(singleton, profile_id, pair_proof_revision, map_digest)
upstream_attempts(id, fingerprint, origin, runtime_epoch_id, logical owner,
                  key/profile/slot, immutable generations/proofs/digests,
                  state, transport markers, seal/terminal, delivery)
public_route_observations(id, runtime_epoch_id, result, failure_class,
                          latency_ms, observed_at)
admin_operation_keys(operation_id, key_id, role)
live_qa_fault_permits(permit_id, run_id, key_id, profile_id, fault,
                      state, expires_at, consumed_attempt_id)
vault_nonce_allocations(key_generation_id FK vault_key_generations, nonce,
                        row_id, allocation_id, purpose, allocated_at)
```

The physical SQL column is canonically `target_slot_no`; every owner DTO and
canonical intent body calls the same value `target_slot`. Generated SQLx rows
must alias `target_slot_no AS target_slot` on read and bind only the physical
name on write. There is no second `target_slot` column, and migration/JSON
schema fixtures assert this mapping for staged first/second/replacement rows.

`vault_key_generations` is an append-only registry, not a deletable key table.
Its exact constraints are `id UUID PRIMARY KEY`, `state` in `active|retired`,
`manifest_digest NOT NULL` with `octet_length(manifest_digest)=32` and a
global unique constraint, `created_at NOT NULL`, and
`retired_at IS NULL` exactly when `state='active'` (nonnull exactly when
`state='retired'`). A partial unique index on `state` where `state='active'`
permits at most one active generation. A deferred constraint trigger rejects
`retired -> active`, a second retirement, retirement of the generation named
by `vault_key_binding`, or any binding to a retired/unknown generation; the
same trigger verifies at commit that a nonnull singleton binding points to the
one active row and that its salt/verifier all-or-none check holds. The registry
row is never deleted: `retire-generation` records a tombstone/receipt by
setting `state='retired',retired_at`, while the nonce ledger's RESTRICT FK and
historical evidence remain intact. The exact registry rows (state, timestamps,
manifest digest) and binding tuple are included in every state projection,
backup/restore all-before/all-after digest, and catalog/readiness check.

`runtime_epochs` has one UUID row for the complete gateway process graph, not
one per Actix worker. Startup first exclusively acquires the host lock
`/run/lock/nvidia-build-lb-ops.lock` and then opens a dedicated
`epoch_lock_conn` for the database advisory lock. The unprivileged gateway never
opens that root:root 0600 path itself: the root-only `nblb-epoch-broker` opens,
validates, and `flock`s it, passes the already-held descriptor once over a
root-owned Unix socket using `SCM_RIGHTS`, and waits for the gateway's startup
ack. The gateway validates the received descriptor with `fstat` and peer
credentials, uses it only for this startup transaction, and closes it after
the epoch/catalog readback; the broker then closes its copy. A broker/FD loss
before the epoch commit fails readiness and intake closed. No steady-state
application path inherits the descriptor. The broker is the root-owned
`nblb-epoch-broker.service`, creates `/run/nvidia-build-lb/epoch-broker.sock`
as a runtime socket owned `root:65532` mode `0660`, and removes/recreates it
only on boot. The app container bind-mounts the host runtime directory at
`/run/nvidia-build-lb` read-only; UID/GID 65532 may connect but cannot create,
replace, or unlink the socket. The broker request is one exactly 32-byte
little-endian frame: `magic[15]` is the literal ASCII bytes
`NBLB_EPOCH_FD1\0`, followed by `version:u8=1`, `flags:u16le=0`,
`gateway_pid:u32le`, `process_start_ticks:u64le`, and `reserved:u16le=0`.
No padding or trailing bytes are accepted; PID must be nonzero and start ticks
must match a fresh `/proc/<pid>/stat` read. Peer credentials must be UID/GID
65532. Within two seconds the broker replies with one `SCM_RIGHTS` descriptor
and a fixed 16-byte nonce frame (`nonce[16]`, generated per request); the
gateway sends a 16-byte acknowledgement that is an exact byte-for-byte echo
of that nonce after `fstat`/advisory-lock/readback. The broker accepts one
matching ACK only, then closes its copy. A timeout, malformed frame, peer
mismatch, duplicate or wrong nonce, broker restart, or lost ACK fails
readiness closed and never falls back to an unbrokered lock. The broker
unit is ordered before the app and restarts with the same runtime directory; a
fixture exercises app restart, FD loss, duplicate ACK, and socket tuple drift.
`epoch_lock_conn` is a dedicated
non-pooled session used only for the advisory lock and its epoch/catalog
readback; it is never used for application or pooled queries. Its fixed signed 64-bit key is
`bigendian_i64(SHA-256(b"nblb:runtime-epoch-startup:v1\0"||schema_name)[0..8])`
with `schema_name` exactly the ASCII string `nblb`;
the connection is never used for unrelated queries or pooled work. The process captures
the canonical kernel boot UUID, gateway PID and `/proc/<pid>/stat` start ticks
before intake, inserts the epoch while holding both locks, and all four workers
share it. The physical table has a unique constraint on
`(boot_id,process_pid,process_start_ticks)` and a partial unique index on the
constant `true` with `WHERE ended_at IS NULL`, permitting exactly one
open/current epoch. Startup reconciliation closes any stale crashed row under
the same locks before inserting the new one. Lock loss, connection loss, or failed
advisory-lock reacquisition fails intake and readiness closed; reconnect may
resume only after the same key is reacquired and the epoch/catalog projection
is read back on the same dedicated connection. A graceful stop sets `ended_at`; a
crash leaves it null. An epoch is current iff its ID/boot/PID/start-ticks equal
the live process. “Old epoch” nowhere means wall-clock age. A concurrent
gateway-startup fixture starts two processes with a barrier and requires one
epoch row, one current partial-index winner, and a loser that never reaches
readiness.

The durable attempt row is this closed tagged union; every field named
`immutable` is byte-equal to the attempt fingerprint inputs in section 8 and
cannot change after insert:

```text
AttemptCommon={
 id:UUID,fingerprint:LowerHex64,origin:"public"|"probe",
 runtime_epoch_id:UUID,logical_request_id:UUID,
 operation_id:UUID|null,downstream_credential_id:UUID|null,
 key_id:UUID,profile_id:ProfileId,slot_no:1|2|null,ordinal:Decimal,
 configuration_generation:Decimal,profile_generation:Decimal,
 global_health_seq:Decimal,profile_health_seq:Decimal,
 key_proof_revision:Decimal,pair_proof_revision:Decimal,
 magpie_key_map_digest:LowerHex64|null,
 magpie_pair_map_digest:LowerHex64|null,
 request_digest:LowerHex64,started_at:Timestamp
}
AttemptDelivery=
 {state:"not_handed_off",handoff_at:null,completed_at:null,abandoned_at:null}
|{state:"handed_off",handoff_at:Timestamp,completed_at:null,
  abandoned_at:null}
|{state:"body_complete",handoff_at:Timestamp,completed_at:Timestamp,
  abandoned_at:null}
|{state:"abandoned",handoff_at:Timestamp|null,completed_at:null,
  abandoned_at:Timestamp}
UpstreamAttempt=
 AttemptCommon+{
  state:"started",upstream_request_maybe_sent:Boolean,
  upstream_first_body_byte_observed:Boolean,
  seal:null,terminal:null,delivery:AttemptDelivery}
|AttemptCommon+{
  state:"terminal",upstream_request_maybe_sent:Boolean,
  upstream_first_body_byte_observed:Boolean,
  seal:{id:UUID,class:AttemptTerminalClass,
        projection_sha256:LowerHex64},
  terminal:{class:AttemptTerminalClass,status_code:Decimal|null,
            response_shape_sha256:LowerHex64|null,
            predicate_bits:LowerHex|null,committed_at:Timestamp,
            health_sequence_applied:Boolean},
  delivery:AttemptDelivery}
AttemptTerminalClass=
 "success"|"cancelled"|"deadline"|"ancestor_cancelled"|
 "downstream_consumer_dropped"|"response_too_large"|"protocol_error"|
 "application_failure"|"network_failure"|"rate_limited"|
 "auth_rejected"|"permission_rejected"|"credits_exhausted"|
 "abandoned_after_restart"
```

The app's workflow UPDATE privilege cannot rewrite evidence. PostgreSQL has
named `BEFORE UPDATE` guards on `upstream_attempts`,
`upstream_asset_intents`, and `upstream_asset_objects`. They compare every
fingerprint, request/logical ID, runtime/config/profile/key/proof/map digest,
ordinal, immutable timestamp, and provenance field to `OLD`, reject a changed
identity or a backwards timestamp, and allow only the enumerated forward
transport/delivery/terminal transitions, asset upload/cleanup transitions,
and the single `abandoned_after_restart` reconciliation. Terminal attempt
identity and sealed projection are immutable; asset provider IDs and raw-media
digests are immutable after first observation; cleanup may change only its
closed cleanup state and receipt fields. The guards run under the same
transaction as counter/health release, have fixed function bodies/owners/ACLs,
and their catalog definitions/digests are part of readiness. Real PostgreSQL
tests exercise every legal transition plus identity, timestamp, terminal,
cross-attempt, and DELETE attempts as explicit denials.

The marker implication is a CHECK:
`upstream_first_body_byte_observed => upstream_request_maybe_sent`.
`started` has both seal/terminal null; there is intentionally no durable
`sealed-but-not-terminal` branch because the seal is an in-memory atomic
linearization immediately consumed by one DB terminal transaction. That
transaction writes the seal and terminal together, applies counters/health,
and releases the live-pin/admission atomically. Commit ambiguity accepts only
the exact still-started row or exact terminal fingerprint/seal/projection.
Transport updates `upstream_request_maybe_sent=true` in a committed transaction
immediately before its first possibly accepting request write, and commits the
first-body marker before publishing that decoded body byte to a parser/
candidate. Neither flag can revert.

Delivery is independent of provider terminal. `downstream_handoff` changes
`not_handed_off -> handed_off` at the first body item yielded to the
product-owned adapter/Writer;
successful end-of-body changes it to `body_complete`. Tracked-body Drop changes
an unfinished delivery to `abandoned`; before terminal seal it also submits the
consumer-drop candidate, after terminal it never changes `terminal`. A body
that drops before first poll may be `abandoned` with null handoff. Delivery
updates never change counters, health, cursor, seal, or provider result.

On startup, any `started` row whose epoch is not current is not replayed. The
reconciler first closes its poll/assets, then commits
`abandoned_after_restart`, counter/in-flight/admission release, and immutable
evidence in the normal lock order. A live/current started row belongs to its
supervisor and cannot be adopted by another worker. CHECKs/FKs enforce origin-
specific operation/downstream nullability, public ordinal `1|2`, probe ordinal
equal to its durable manifest case ordinal, slot, proof/map digests, and
delivery timestamps; generated Rust/SQL fixtures enumerate every union branch.

Proof keys are closed DDL, not naming convention. A per-key revision is a
positive `BIGINT`, strictly increasing within `(key_id,profile_id)`, with
primary key `(key_id,profile_id,proof_revision)` and a unique referenced tuple
that additionally includes `credential_fingerprint`. The fingerprint-inclusive
four-column UNIQUE exists solely for the two `pair_proofs` identity foreign
keys. Every proof additionally has
`FOREIGN KEY (key_id,credential_fingerprint) REFERENCES
upstream_keys(id,fingerprint) ON DELETE RESTRICT`, so a credential identity
cannot be fabricated independently of its key tombstone. The nullable
`key_profile_state.current_proof_revision` instead has this
exact immediate foreign key:

```sql
FOREIGN KEY (key_id, profile_id, current_proof_revision)
REFERENCES key_profile_proofs (key_id, profile_id, proof_revision)
MATCH SIMPLE ON DELETE RESTRICT
```

`MATCH SIMPLE` permits the intentional null revision with nonnull key/profile;
the owner DTO serializes null as revision `"0"` with
`per_key_proof_current:false`. A pair revision is a positive `BIGINT`, strictly
increasing within `profile_id`, with primary key
`(profile_id,pair_proof_revision)`. Its Slot 1 and Slot 2 identity tuples each
foreign-key the exact corresponding
`(key_id,profile_id,proof_revision,credential_fingerprint)` proof tuple. For a
pair, named CHECKs require `slot1_key_id <> slot2_key_id` and
`slot1_fingerprint <> slot2_fingerprint`; each slot number is represented by its
fixed column and cannot be swapped by row order. For a
target ordered pair, the current pair proof is the greatest revision whose
complete identities, current per-key revisions, contract/suite digests, and
Magpie map digests match; a staged-candidate proof therefore cannot displace
the still-current assigned-pair proof.

Every Magpie table has CHECK `profile_id =
'nvidia/magpie-tts-multilingual'`; `key_profile_id` has the same CHECK and must
equal `profile_id`. Key map primary key is
`(key_id,profile_id,key_proof_revision)` and foreign-keys the exact per-key
proof; it also has explicit
`UNIQUE(key_id,profile_id,key_proof_revision,map_digest)`, and key voices add
`voice` to the primary key. Pair map primary key is
`(profile_id,pair_proof_revision)`, has explicit
`UNIQUE(profile_id,pair_proof_revision,map_digest)`, and foreign-keys the exact
pair proof; pair sources and voices add respectively `slot_no` and `voice`.
Each source has a composite foreign key including `key_map_digest` to the exact
key map and a composite unique
`(profile_id,pair_proof_revision,key_id)`. The published singleton includes the
fixed profile ID and composite-foreign-keys the exact pair map including
`map_digest`. These keys make every claimed FK executable in PostgreSQL and
prevent cross-profile revision aliasing. A named PostgreSQL constraint trigger
`magpie_pair_sources_complete` runs `DEFERRABLE INITIALLY DEFERRED` after INSERT,
UPDATE, or DELETE of a Magpie pair proof, pair map, or source. At transaction
end it requires exactly two source rows with slot set exactly `{1,2}`; Slot 1's
`key_id,key_proof_revision,key_map_digest` must equal the pair proof's Slot 1
identity and exact referenced key map, and Slot 2 must equal its Slot 2 identity
and map. It also requires one pair map and rejects a source for a non-Magpie
profile. Assignment/publication explicitly `SET CONSTRAINTS
magpie_pair_sources_complete IMMEDIATE` before pointer update, then defers it
again only if more work remains. Thus neither missing cardinality nor a
cross-slot but otherwise valid key-map FK can commit.

`vault_nonce_allocations` has primary key `(key_generation_id,nonce)` and a
nondeferrable foreign key to the append-only `vault_key_generations(id)` registry,
`octet_length(nonce)=12`, unique `allocation_id`, `purpose` exactly
`create|restore|migration`, and a deferred FK from `row_id` to
`upstream_keys(id)`. A transaction preallocates the row UUID, draws a 12-byte
CSPRNG nonce, and attempts the ledger insert with `ON CONFLICT DO NOTHING`.
Only an inserted nonce may be used for AES-GCM. Collision draws again, capped at
32 attempts; exhaustion aborts without ciphertext. The allocation and envelope
commit atomically, and committed allocation rows are never updated or deleted,
including after retirement, backup, or restore. Restore and legacy import use
the same ledger and cap for every re-encrypted row, so no `(generation,nonce)`
pair can be reused by a different ciphertext.

The ledger table is owned by `nblb_ledger_owner`, not `nblb_owner`; bootstrap
grants that role `USAGE,CREATE` on schema `nblb` and its migration executes an
explicit `SET ROLE nblb_ledger_owner`, then returns to `nblb_owner`. PUBLIC,
`nblb_app`, and `nblb_owner` have no UPDATE, DELETE,
TRUNCATE, REFERENCES, TRIGGER, or ownership privilege. `nblb_app` receives only
SELECT and INSERT; `nblb_owner` receives SELECT only so a source snapshot can
include the ledger while it remains unable to mutate or change ownership.
`ALTER DEFAULT PRIVILEGES FOR ROLE nblb_ledger_owner IN SCHEMA nblb` revokes
PUBLIC and grants the app only SELECT/INSERT and ordinary owner only SELECT;
the ordinary `nblb_owner` defaults therefore cannot broaden a ledger-owned
object. `nblb_migrator` can enter the ledger role only through its direct
`SET TRUE,INHERIT FALSE` membership; it has no inherited ledger privilege
before that explicit transition. An `ENABLE ALWAYS` row trigger rejects UPDATE/DELETE
and a statement trigger rejects TRUNCATE with fixed SQLSTATE/message; only a
reviewed migration running as the dedicated owner can replace schema/trigger
bytes, never mutate committed allocation rows. Trigger functions are owned by
`nblb_ledger_owner`, have fixed `pg_get_functiondef`/language/volatility/
security/owner/prosrc digests, and have PUBLIC/APP EXECUTE revoked. The
steady-state function inventory is exactly the trigger functions listed below,
all with `RETURNS trigger`, `LANGUAGE plpgsql`, `VOLATILE`, `SECURITY INVOKER`,
and no PUBLIC/APP EXECUTE. Scalar/check helpers are declared separately in the
catalog manifest with their boolean/text return types and the minimum EXECUTE
ACL required for table CHECK evaluation:
`nblb_guard_vault_generation_transition`,
`nblb_guard_vault_binding_transition`, `nblb_guard_ledger_immutable`,
`nblb_guard_attempt_update`, `nblb_guard_asset_intent_update`,
`nblb_guard_asset_object_update`, `nblb_guard_intake_admission_delete`, and
`nblb_magpie_pair_sources_complete`, `nblb_guard_admin_operation_update`,
`nblb_guard_mutation_intent_update`, `nblb_guard_live_qa_fault_update`,
`nblb_guard_runtime_epoch_update`, `nblb_guard_public_route_observation_insert`,
`nblb_guard_attention_insert_update`, `nblb_guard_evidence_immutable`,
`nblb_guard_request_terminal_immutable`, `nblb_guard_event_immutable`,
`nblb_guard_event_insert`, `nblb_guard_event_evidence_pair`,
`nblb_guard_intake_control_update`, `nblb_guard_upstream_key_update`,
`nblb_guard_downstream_credential_update`, and
`nblb_guard_key_profile_proof_immutable`,
`nblb_guard_magpie_key_voice_map_immutable`,
`nblb_guard_magpie_key_voice_immutable`,
`nblb_guard_magpie_pair_voice_map_immutable`,
`nblb_guard_magpie_pair_voice_immutable`,
`nblb_guard_published_magpie_map_immutable`,
`nblb_guard_admin_operation_key_immutable`, and
`nblb_guard_legacy_migration_audit_immutable`, and
`nblb_guard_evidence_insert`. Their exact argument signatures,
`pg_get_functiondef` digests, owners, ACLs, attached table/constraint-trigger
names, and enabled states are in the generated catalog manifest; the
operation-function allowlist is exactly empty because application DML uses the
closed table ACL above. The bootstrap-only `pg_temp.nblb_set_login_scram` is
allowed only during root initialization and must be absent from the steady
catalog. Startup reads
`pg_class`, `pg_proc`, ACLs, role membership, owner, trigger definitions/enabled
state, policies/rules/index expressions/constraints, and hashes them into
readiness. Unknown rows or changed function bodies fail readiness. Real-PostgreSQL tests as app/ordinary owner attempt UPDATE, DELETE,
TRUNCATE, DROP, ALTER, trigger disable, ownership change, `SET ROLE
nblb_ledger_owner`, and membership/default-ACL changes and require denial plus
unchanged row/projection. A separate migrator test proves no ledger privilege
before its explicit role transition and verifies only the fixed DDL path after
it. Backup/restore retain every allocation and the same owner/membership/
default-ACL/trigger contract.

`global_health_state` is exactly `clear|invalid_credential`; `entitlement` is
`unknown|ok|not_entitled|credits_exhausted`; `protocol_state` is
`clear|protocol_quarantined`. Request reservation locks the key then profile,
allocates one strictly increasing key-global sequence and one profile sequence,
and stores both on the attempt. Terminal application locks the same key then
profile and applies each projection only when its sequence is greater than the
corresponding last-applied value. A full seven-profile probe terminal allocates
a new global sequence after all case attempts; a selected-profile probe terminal
allocates a new profile sequence after its cases. Thus a later-started 401 can
override a probe clear while an older 401 cannot.

The complete health transition table is:

| Terminal class | Global transition | Selected profile transition |
| --- | --- | --- |
| fully received 401 | newer sequence sets `invalid_credential` | receipt only |
| fully received 403 | none | newer sequence sets `not_entitled` |
| fully received 402 | none | newer sequence sets `credits_exhausted` |
| protocol error or upstream response too large | none | newer sequence sets `protocol_quarantined` |
| 429 | none | newer sequence extends rate cooldown/streak |
| nondeadline transport failure, local deadline, provider 408, 500/502/503/504, or validated FLUX/SVD `ERROR` | none | newer sequence extends transient cooldown/streak |
| ordinary 400/404/409/422, content policy, asset preparation/cleanup, tracked downstream consumer drop, cancellation | none | none |
| ordinary valid upstream success | none | resets both cooldowns/streaks only; persistent states remain |
| successful selected-profile probe | none | sets entitlement `ok`, protocol `clear`, resets cooldowns/streaks |
| successful full seven-profile probe | sets global `clear` | applies the selected-profile probe transition to all seven rows |

No body text changes this table. A profile is ineligible when global state is
invalid, entitlement is not-entitled/credits-exhausted, protocol is quarantined,
either cooldown is live, it is manually disabled, proof is stale, or capacity
is full. Only the stated probe transitions clear persistent states.

Each profile owns its cursor, health, cooldown, entitlement, probe, counters,
and eligibility. A failure in VILA cannot cool or recover GLM. Proof validity is
identity-based, not configuration-generation-based. A per-key proof is current
iff its key UUID and credential fingerprint still match, its profile-contract
and probe-suite digests equal the immutable running manifest, and every required
case is PASS. A pair proof is current iff its ordered key UUIDs/fingerprints,
both current per-key proof revisions, profile-contract/probe-suite digests, and,
for Magpie, ordered voice-map digests all match. Assignment, cursor reset,
enable/disable, an unrelated key probe, and a global/profile snapshot generation
increment do not rebind or stale those identities. Schema/profile/suite drift,
credential replacement, or a changed Magpie map does. A profile is advertised
only after both assigned identities have current per-key proofs and every
required current pair proof passes. Temporary cooldown/disable does not remove
an already verified model, but a replacement cannot advertise until the
candidate and required pair proof pass.

Magpie maps are durable data, not reconstructible digests. Each per-key proof
revision owns normalized rows with primary key
`(key_id,profile_id,key_proof_revision,voice)`, foreign key to that exact proof
revision, `COLLATE "C"`, one locale, and `kind=base|emotion`; CHECKs enforce
section 6's ASCII/component grammar and kind/component count. Its
`canonical_json` is exact UTF-8 with no LF:
`{"voices":[{"kind":"base|emotion","locale":"<locale>",
"voice":"<voice>"},...]}`, object keys in that order and rows sorted by
unsigned voice UTF-8 bytes. `map_digest` is raw SHA-256 of those bytes, unique
with `(key_id,profile_id,key_proof_revision)`, and rows/bytes/digest are
recomputed and compared in the proof commit.

A pair proof plan durably preallocates the target revision, ordered source
identities/maps, deterministic intersection, and applicable same-voice case
IDs and attempt UUIDs before any provider request. For each case, `started` and
`case_dispatched` are separately committed and read back before the sequential
HTTP call; no DB row/advisory lock is held across network. Its terminal receipt
and any asset cleanup state are durably committed before the next case. The
successful final transaction locks and rereads the
two exact per-key revisions and all case receipts, recomputes the byte-exact
locale-consistent intersection, writes the analogous pair tables, stores its
ordered identities/revisions and canonical bytes/digest, and commits an
unpublished candidate pair map only if every applicable case is PASS. Its
voice rows have primary key
`(profile_id,pair_proof_revision,voice)`. `magpie_pair_sources` has exactly Slot
1 and Slot 2 rows, primary key
`(profile_id,pair_proof_revision,slot_no)`, unique
`(profile_id,pair_proof_revision,key_id)`, and the exact composite foreign keys
defined above. A pair probe compare-and-swaps `published_magpie_map` only when
its ordered identities are already the two currently assigned slots. A staged
Slot 2 or replacement probe never changes the public singleton; first/second
assignment or replacement commit publishes its already-complete target map in
the same transaction that installs the target ordered pair. This preserves the
old assigned pair's voice allowlist throughout replacement and prevents an
unassigned candidate from changing traffic. Public validation and owner model
DTOs read only the singleton-target rows; onboarding reads its candidate count
from the probe operation result.
Startup/restore recomputes every canonical map/digest and the intersection;
missing rows, FK drift, or a singleton mismatch fails readiness. The prior
published pointer remains unchanged on any partial/failed probe.

Profile and evaluator identity are byte-defined. For profile `p`,
`contracts/nvidia/profile-contracts/<manifest-ordinal>-<safe-id>.json` is RFC
8785 JCS UTF-8 without LF and contains the exact profile row, public/provider
schemas, adapter limits, source snapshot paths/sizes/hashes, ordered predicate
IDs/thresholds, and `predicate_evaluator_digest`. It also contains one ordered
`files` array of every schema/parser/adapter/evaluator source or fixture that
can change acceptance or a positive predicate. Paths are normalized relative
POSIX paths, unique, and sorted by unsigned UTF-8 bytes. Let `C_p` be those JCS
bytes and let each listed file have exact bytes `F_i`. The stored
`profile_contract_digest` is exactly:

```text
SHA-256(b"nblb:profile-contract:v1\0" ||
        u16be(len(profile_id_utf8)) || profile_id_utf8 ||
        u32be(len(C_p)) || C_p || u32be(file_count) ||
        for each listed file:
          u16be(len(relative_path_utf8)) || relative_path_utf8 ||
          u64be(len(F_i)) || F_i)
```

The evaluator itself has a generated JCS
`contracts/nvidia/predicate-evaluator.json` with no LF. It lists the exact
Rust evaluator source/lockfile closure, compiler/target flags, built evaluator
binary size/SHA-256, and the OCI manifest/config/layer digests plus decoder
binary size/SHA-256 of the network-disabled, read-only SVD frame-decoder image.
That image has no provider credential, network, writable root, or production
mount; it receives one bounded MP4 on stdin and returns only fixed RGB frame
records. Let `E` be the JCS bytes and `B_i` every listed source, lockfile,
binary, and OCI manifest/config byte sequence in manifest order. The immutable
`predicate_evaluator_digest` is:

```text
SHA-256(b"nblb:predicate-evaluator:v1\0" || u32be(len(E)) || E ||
        u32be(file_count) ||
        for each listed byte object:
          u16be(len(object_id_ascii)) || object_id_ascii ||
          u64be(len(B_i)) || B_i)
```

Candidate freeze resolves every size/digest to a literal and rehashes every
byte; placeholders, build paths, timestamps, host CPU dispatch, downloads, or
an unlisted decoder fail before a probe. The evaluator uses pinned software
`libm`, disables FMA/CPU-native features, accumulates vector dot products and
squared norms with ordered Neumaier binary64 summation, and rounds exposed
`*_micros` values to nearest ties-to-even. Both per-key and pair proof rows bind
the profile contract; each case receipt additionally carries the evaluator
digest. Changing any threshold, semantic fixture, decoder, source, build flag,
or binary changes a digest and stales the proof.

Positive live quality is a closed oracle rather than a status/shape proxy.
Fixture prompts ask for the observed values but never contain the expected
answer tokens; expected tokens live only in the reviewed manifest. JSON answers
must have exactly the named keys/strings after NFC and no explanatory text.
The required semantic predicates are:

| Profile/cases | Exact consumption and quality oracle |
| --- | --- |
| Phi PNG+WAV variants | The image visibly encodes `lime-7` and the WAV speaks `tuna-4`; the returned exact object is `{"image_code":"lime-7","audio_code":"tuna-4"}`. |
| Phi JPEG+MP3 variants | The JPEG visibly encodes `amber-3` and the MP3 speaks `river-9`; the returned exact object is `{"image_code":"amber-3","audio_code":"river-9"}`. Direct/asset and JSON/SSE variants use the same expected values, proving both modalities rather than merely accepting their bytes. |
| VILA PNG variants | The image contains code `north-2` and three high-contrast objects; exact answer `{"visual_code":"north-2","object_count":"3"}`. |
| VILA JPEG variants | The image contains code `east-5` and two objects; exact answer `{"visual_code":"east-5","object_count":"2"}`. |
| VILA MP4 variants | First/middle/last frames contain respectively `north-2`,`east-5`,`south-8`, while one marker moves left-to-right; exact answer `{"first":"north-2","middle":"east-5","last":"south-8","motion":"left_to_right"}`. This proves ordered video consumption, not a first-frame image shortcut. |
| NVCLIP every inline/asset PNG/JPEG case | Input order is matching caption, deliberately mismatched control caption, then image. Every 1,024-vector value is finite, each L2 norm is at least `0.000001`, matched caption/image cosine is at least `0.10` and exceeds control/image cosine by at least `0.02`, and matched/control vectors differ in at least one component by `0.000001`. |
| FLUX | The fixed prompt requests one bright green triangle centered on a matte black square; the control caption describes a crowded blue ocean photograph. The returned JPEG then enters one forced-same-key NVCLIP quality attempt with prompt, control, image. Prompt/image cosine is at least `0.10` and exceeds control/image by at least `0.02`; additionally the central 40% has green-channel mean at least 20 code points above red and blue and the outer 10% border mean luma is at most 96. |
| SVD | The orange-ball-on-dark input and fixed subject/control captions are manifest bytes. First and last decoded frames each have subject-caption cosine at least `0.10` and margin at least `0.02` over control; first/last mutual cosine is at least `0.20`. At least 1% and at most 60% of same-position pixels change luma by at least 8/255, and the orange-mask centroid moves at least 2 pixels but at most half the frame diagonal while mask area remains 0.5%..35% in both frames. This proves subject preservation plus nonstatic motion. |
| Magpie base/emotion cases | The existing signal thresholds pass, then the WAV enters one forced-same-key Phi audio transcription attempt. After NFC, lowercase, removal only of Unicode punctuation, and collapse of Unicode whitespace, the exact result is `안녕하세요 음성 경로 확인입니다`; any missing/substituted word fails. |

Quality sub-attempts are planned before their parent provider dispatch, use the
same forced credential, have their own durable attempt UUID/fingerprint and
normal cleanup, and never fail over. Probe order ensures Phi and NVCLIP have
already passed on that key before another profile may use them as an oracle.
The parent cannot PASS until every sub-attempt commits success and its exact
predicate projection. A sub-attempt 401/402/403/429, timeout, protocol error,
cleanup ambiguity, or contract-digest mismatch fails the parent case; no
cached result or other key substitutes. This cross-model check is evidence,
not a public recursive API call, and its bounded input/output never enters
client responses.

Each quality case hashes one closed secret-free projection containing only
expected-token match booleans, vector norms/cosines in signed millionths,
channel/luma/component counts, SVD mask/change/centroid metrics, or normalized
transcript match plus character count. It contains no prompt, expected token,
media, transcription text, vector, provider body, or asset ID. The receipt's
`quality_projection_sha256` is null exactly for structural-only cases and
nonnull for every row in the table; PASS requires it and the evaluator digest
as well as all predicate bits.

The exact live proof set, run independently with each assigned or staged
candidate key forced, is:

| Profile | Required case IDs for each forced key |
| --- | --- |
| GLM | `glm_ko_json`, `glm_ko_sse_done`, `glm_tool_call_result_json`, `glm_reasoning_json`, `glm_json_object` |
| Phi | `phi_inline_png_wav_input_audio_json`, `phi_inline_jpeg_mp3_audio_url_sse`, `phi_asset_png_wav_json`, `phi_asset_jpeg_mp3_json`, `phi_nonstream_json` |
| VILA | `vila_inline_png_json`, `vila_inline_jpeg_sse`, `vila_asset_png_json`, `vila_asset_jpeg_json`, `vila_inline_mp4_h264_json`, `vila_asset_mp4_h264_json`, `vila_nonstream_json` |
| NVCLIP | `nvclip_text_inline_png_float`, `nvclip_text_inline_jpeg_float`, `nvclip_asset_png_float`, `nvclip_asset_jpeg_float` |
| FLUX | `flux_text_jpeg_success` through `/v1/images/generations` |
| SVD | `svd_png_mp4_success`, `svd_jpeg_mp4_success` through the native route |
| Magpie | `magpie_voice_list`, `magpie_base_voice_wav`, `magpie_emotion_voice_wav` when an emotion voice exists; absence is a recorded not-applicable proof, not a fabricated pass |

Probe receipts contain the observed configuration/profile generation, immutable
profile-contract and probe-suite digests, safe case ID, key internal ID and
credential fingerprint, status class, latency, response-shape hash, and asset
cleanup state. They also contain the exact predicate-evaluator digest and the
nullable quality-projection hash defined above, never media, semantic answer,
vector, transcription, prompt, expected token, or provider body. A proof revision advances only
when the selected profile's entire required case set passes. Case progress and
partial sweeps are visible evidence but cannot advertise. Whenever two target
identities exist and the other identity has a current per-key proof, the same
successful terminal transaction creates a new exact pair proof for every
selected profile from the candidate/new and surviving proof revisions.
Non-Magpie pair proof creation needs no extra provider request; it binds the two
complete forced-key case suites. Magpie's same-voice provider cases follow the
durable outside-transaction sequence above; the final transaction only
revalidates their receipts and atomically commits current per-key pointers,
pair proof/map/source rows, and the conditional published singleton. Failure
retains receipts but changes none of those pointers/publication. It publishes only under the
already-assigned identity rule above and otherwise leaves the prior singleton
byte-for-byte unchanged. A pair-case failure commits case/cleanup receipts but
neither the new per-key proof pointer nor target pair proof/publication, so one
operation cannot expose a half-current target.

The deterministic fake-provider oracle case
`glm_tool_call_sse_fragmented_multi` requests two named functions and returns
two new response IDs. Each name/ID appears only in its first fragment; their
argument fragments interleave by indices 0 and 1, both assemble to distinct
valid JSON values, the terminal reason is exactly `tool_calls`, and `[DONE]`
follows durable success. It is the mandatory positive counterpart to malformed
fragment fixtures; an implementation that accepts only one unfragmented call
cannot pass the candidate gate. It is deliberately not a live proof case
because provider chunk boundaries and multi-call selection are not a
deterministic oracle.

`contracts/nvidia/live-fixtures.json` is a reviewed JCS source file, not a
runtime-generated receipt. It contains every case ID in manifest order and the
literal fixture relative path, byte size, SHA-256, MIME, representation
(`inline|asset`), expected request metadata, and closed positive output
predicate. Build rehashes each immutable fixture and refuses a missing or
different byte before any live request. PNG/JPEG/WAV/MP3/MP4 variants are real
nontrivial media; asset cases use deterministic padding outside semantic media
to cross the exact 180,000-character plan only where the parser still accepts
the file. Let `M` be the exact RFC 8785 JCS UTF-8 manifest bytes with no final
LF and let cases remain in manifest order. The reviewed suite digest is exactly:

```text
SHA-256(b"nblb:probe-suite:v1\0" || u32be(len(M)) || M ||
        u32be(case_count) ||
        for each case:
          u16be(len(case_id_utf8)) || case_id_utf8 ||
          u16be(len(relative_path_utf8)) || relative_path_utf8 ||
          u64be(fixture_byte_len) || exact_fixture_bytes)
```

Paths are normalized relative POSIX paths with no empty/dot/dot-dot component;
case/path UTF-8 lengths fit `u16`, each fixture fits `u64`, and all concatenation
uses checked arithmetic. The manifest's declared size/hash is independently
compared with each exact byte sequence before the digest is accepted.

`response_shape_hash` is exactly SHA-256 of RFC 8785 JCS for one member of this
closed typed projection union, never provider JSON or generated media bytes:

```text
ChatFinishReason="stop"|"length"|"tool_calls"|"content_filter"
{variant:"chat",model:ProfileId,row_count:Decimal,finish_reason:ChatFinishReason,
 tool_call_count:Decimal,usage_present:Boolean,
 stream:null|{chunk_count:Decimal,terminal:"finish_then_done"}}
{variant:"nvclip",row_count:Decimal,dimensions:[Decimal]}
{variant:"flux",mime:"image/jpeg",width:1024,height:1024,
 artifact_count:"1",finish_reason:"SUCCESS"}
{variant:"svd",mime:"video/mp4",codec:"avc1"|"avc3"|"hvc1"|"hev1",
 width:Decimal,height:Decimal,frame_count:Decimal,duration_ms:Decimal,
 milli_fps:Decimal,artifact_count:"1",finish_reason:"SUCCESS",seed:Decimal}
{variant:"magpie",mime:"audio/wav",sample_format:"pcm_s16le",
 sample_rate:"44100",channels:"1",bits:"16",duration_ms:Decimal,
 absolute_peak:Decimal,rms:Decimal}
```

`dimensions` has one entry per returned NVCLIP row and every entry is literal
`"1024"`; arrays retain request row order. All `Decimal` values are canonical
decimal strings. SVD integer projections use section 6.2's rational formula.
For each case the manifest additionally owns a nonempty ordered list of unique
ASCII predicate IDs. Predicate index `i` maps to byte `floor(i/8)`, bit
`7-(i mod 8)` (most-significant bit first). Unused low bits in the last byte are
zero; the exact `ceil(predicate_count/8)` bytes are encoded as two lowercase hex
digits per byte with no prefix. `predicate_count`, bytes, and hex length must
agree. Case PASS requires every defined bit one; a response-shape hash never
substitutes for this bitmap.

Negative fake fixtures isolate one invariant at a time while all other
predicates remain valid: NVCLIP vectors of exactly 1,023 and 1,025 elements;
FLUX JPEGs 1,023x1,024, 1,025x1,024, 1,024x1,023, and 1,024x1,025; SVD outputs
with only one of width, height, pixel count, frame count (13 or 301), duration
(499 or 30,001 ms), frame rate (below 1 or above 120), or decode-work over the
cap invalid; and Magpie proof WAVs with only duration, absolute peak, or RMS
below its threshold. Separate fixtures cover every malformed data-URL row,
invalid SSE ordering/tool fragments, and the three multi-asset cleanup/failover
cases. Each binds expected public wire, attempt terminal, health transition,
cleanup rows, and zero/one alternate. No combined-invalid fixture is credited
as an independent boundary proof.

For each live `phi_nonstream_json` and `vila_nonstream_json` case, direct 200 is
PASS and a naturally returned 202 followed by the exact section 6.4 poll path
to 200 is also PASS; the receipt records `direct_200|polled_202_200`. There is
no input claimed to force 202. The deterministic fake-provider gate separately
forces every 202, malformed 202, changed request ID, deadline, restart, and poll
terminal branch.

The empty installation assigns Slot 1 first and Slot 2 second; an assign body
requesting the other order is `resource_conflict`. Pair proof ordering is always
target Slot 1 identity then target Slot 2 identity, including a staged Slot 2 or
a replacement candidate whose target slot is known from its resource. Slot 1
assignment requires every per-key case, including Magpie discovery
and synthesis using a voice from that key; it does not require a nonexistent
second-key intersection. Nothing is advertised and no downstream token is
issued with one slot. Second-slot assignment requires its per-key cases plus a
pair proof against slot 1; replacement requires the same pair proof against the
surviving assigned key. Pair proof computes the byte-exact intersection, then
requires at least one common base voice (exactly three dot components), selects
the unsigned-UTF-8-lowest common base voice, and synthesizes the same fixed
Korean probe text `안녕하세요. 음성 경로 확인입니다.` with that exact voice
through both forced keys. If any common
emotion voice exists, it likewise selects the unsigned-UTF-8-lowest one and
synthesizes the same text through both keys; otherwise the emotion pair case is
recorded not-applicable. Both WAVs in each applicable pair case must pass the
same closed validator. The pair receipt binds the ordered key IDs/fingerprints,
both voice-map digests, chosen common voice digests, profile-contract digest,
and response-shape hashes. Only then is the full common map published. Thus
pair proof never blocks first-key custody and never permits a two-key pair whose
advertised common TTS path was not exercised on both credentials.

### 7.1 Admin creation, assignment, replacement, and deletion

Every owner mutation is preceded by a durable nonsecret server intent. `POST
/admin/api/v1/mutation-intents` accepts exactly
`{operation_id:UUIDv4,kind:<AdminOperation kind>,method:<table method>,
path:<exact table path>,expected_configuration_generation:Decimal,
prepared_request:null|{format:"canonical-json-v1",body_base64url:Base64Url,
body_sha256:LowerHex64}}`. The nested request is required for every
`input_class:"nonsecret"` kind and is a canonical, schema-validated body with
all common fields; it is null for `upstream_create|replacement_create` and can
never contain a credential. The server recomputes its digest and stores the
exact bytes before any secret field is read or cleared. The same exact body is idempotent;
another body for that UUID is `operation_id_conflict`. A nondeferrable partial
unique index on constant true permits exactly one intent with
`acknowledged_at IS NULL`, so all tabs share one mutation authority.

```text
MutationIntent={
 id:UUID,kind:<AdminOperation kind>,method:String,path:String,
 input_class:"secret"|"nonsecret",
 expected_configuration_generation:Decimal,
 prepared_request:null|{format:"canonical-json-v1",body_base64url:Base64Url,
                        body_sha256:LowerHex64},
 state:"prepared"|"executing"|"terminal",
 terminal:null|
   {outcome:"succeeded",operation:AdminOperation}|
   {outcome:"rejected_stable",error_code:<admin-error-code>}|
   {outcome:"abandoned",error_code:null},
 fingerprint_state:"not_computed"|"stored",
 request_fingerprint:LowerHex64|null,
 prepared_at:Timestamp,updated_at:Timestamp,acknowledged_at:Timestamp|null
}
```

Intent nullability is exact. A nonsecret intent has a nonnull
`prepared_request` whose decoded canonical body and `body_sha256` remain
byte-identical through `prepared`, `executing`, and every terminal state until
acknowledgement; its `fingerprint_state` is `stored` from prepare onward and
`request_fingerprint == prepared_request.body_sha256`. A secret intent has a
null `prepared_request` and `fingerprint_state:"not_computed"` only while
prepared; it becomes `stored` with the exact dispatch-body digest at executing.
 A secret-class `terminal/rejected_stable` is `not_computed` only for
`unsupported_media_type|request_too_large`, whose body is deliberately not
consumed; nonsecret intents always retain their prepared canonical body and
stored fingerprint. Every bounded identity-JSON rejection is `stored`, including malformed
JSON because its raw bytes are streamed into the digest before parse. Any
terminal may have null acknowledgement (open) or one
timestamp not earlier than `updated_at`; a nonterminal can never be
acknowledged. The open-intent partial index is therefore exactly
`acknowledged_at IS NULL`, including executing and unacknowledged terminal.
`input_class:"secret"` is exact only for `upstream_create|replacement_create`;
every other kind is `nonsecret`. It is immutable at prepare and checked against
kind/method/path on every readback.

`GET /mutation-intents/open` returns `{intent:MutationIntent|null}` and
`GET /mutation-intents/<id>` returns the exact row. Login reads `open` before
enabling any mutation, making this endpoint—not browser storage—the cross-tab,
reload, and browser-restart authority. `POST /mutation-intents/<id>/abandon`
may change only `prepared -> terminal/abandoned`; it takes the intent row lock,
so either abandon wins and every later mutation request is rejected without
effect, or execution already won and its result is returned. `POST
/mutation-intents/<id>/acknowledge` stamps a terminal row exactly once and frees
the singleton position; history retains the row. Neither control route carries
a provider secret or performs provider work.

A terminal successful `downstream_issue` intent cannot use the generic empty
acknowledgement blindly. If this document received the one-time plaintext and
completed section 12.3 custody, it acknowledges with exact
`{disposition:"received_and_cleared"}`; every other intent uses an empty object.
If plaintext was not received or its disposition is unknown, the only route is
`POST /mutation-intents/<issue-intent-id>/lost-secret-revoke-intent` with
`{operation_id:<fresh UUIDv4>,expected_configuration_generation:<fresh Decimal>}`.
This is a non-provider control transition, not a second open-intent request.

The transition locks configuration, the old intent, its exact succeeded issue
operation/result, and issued active credential. It requires kind
`downstream_issue`, `secret_available:false`, null acknowledgement, and the
credential ID produced by that operation. In one transaction it stamps the old
intent acknowledged and inserts the new exact `prepared` intent with the supplied
ID, kind `downstream_revoke`, method `POST`, path
`/admin/api/v1/downstream-credentials/<issued-id>/revoke`, and fresh expected
generation. The partial singleton constraint is checked only after that
old-to-new order. It returns `{intent:<new revoke intent>}` and performs no
revoke itself. Response loss has only two valid readbacks: all-before leaves the
old issue intent open and permits the byte-identical control request; all-after
has the exact new revoke intent open and returns it. “Old acknowledged + no new
intent,” both open, another target, or another UUID is corruption and locks
mutation. Thus an active lost credential is never orphaned between intents.
Browser custody escape and Hermes issue recovery both use this same transition,
read back the revoke intent, execute its ordinary mutation once, reconcile its
operation, and acknowledge only the terminal revoke intent. Fault fixtures stop
after every statement/commit/response boundary and require exactly one open
remediation authority until revoke is proved.

The prepare request itself has no external side effect. If its response is
lost, the client may resend its byte-identical nonsecret body; UUID uniqueness
serializes it, and no actual mutation may dispatch until an exact prepared row
has been read back. Consequently 404 before a successful prepare readback is
not interpreted as mutation absence. After readback, that UUID can never 404.
If the document dies before actual dispatch, the durable state remains
`prepared`. A nonsecret intent's canonical `prepared_request` is the complete
server-owned input, so the next fresh login offers Continue as the sole primary
action and Abandon as the secondary action after exact readback; a secret-bearing
action must be abandoned and then re-entered under a new UUID because plaintext
was never persisted.

Every actual mutation carries exact header `NBLB-Operation-Id: <same UUID>` in
addition to the body field. After owner auth, the server locks that prepared
intent before parsing the body, verifies method/path/kind/generation, and
transitions it to `executing` in the same transaction that stores the request
fingerprint and applies or accepts the operation. Closed-schema, stale-
generation, resource-conflict, DB-precondition, and every other known
pre-provider rejection terminalize the intent as `rejected_stable`; they never
leave a 404/unknown hole. A crash before commit rolls the intent back to
`prepared`, proving no synchronous mutation or probe dispatch committed. A
commit/readback ambiguity keeps `executing`, blocks new mutation, and is
resolved only to the fingerprint-matching operation/rejection. This is the
single mutation recovery state machine for all tabs.

Every mutation body carries client-generated canonical lowercase UUIDv4
`operation_id` plus decimal-string `expected_configuration_generation`.
Secret fields are write-only and absent from every response. After identity
JSON media/framing and the raw-size bound pass, but before the duplicate-key
parser, secret persistence, or any side effect, the server re-canonicalizes a
nonsecret prepared body and requires byte/digest equality; for a secret body it
streams the exact raw bytes into this idempotency fingerprint using the already
matched intent/header operation UUID. It stores only the digest for secret
input. The parsed body must later contain that same UUID and generation. A
malformed bounded JSON body can therefore
terminalize its intent without retaining body bytes; unsupported media/encoding
or an over-bound body is not consumed and uses the explicit not-computed
rejection branch:

```text
SHA-256(b"nblb:admin-operation:v1\0" || operation_uuid.network_bytes ||
        u16be(method_ascii_len) || method_ascii ||
        u16be(path_ascii_len) || path_ascii ||
        u32be(raw_body_len) || exact_raw_body_bytes)
```

The raw body is bounded while hashing and is never stored. Same UUID plus the
same stored fingerprint reads the original operation or stable intent
rejection; another fingerprint is `operation_id_conflict`. A not-computed
terminal rejection is returned from the intent without consuming a later body
and cannot be repurposed. This applies before side effects and makes a lost
create response discoverable without retaining its secret.

`admin_operations` terminal payloads are validated by immutable SQLx helpers.
`nblb_is_admin_operation_result(bytea)` accepts only canonical, secret-free JCS
object bytes with exactly `{operation_id,kind,state,snapshot,result}` where
`operation_id` is the row UUID, `kind` is the row kind, `state:"succeeded"`,
`snapshot` is the configuration snapshot number, and `result` is the closed
kind-specific DTO with no credential/ciphertext/token fields. The paired
`nblb_is_admin_operation_error(bytea,text)` accepts only canonical, secret-free
JCS `{operation_id,kind,state:"failed"|"cancelled"|"recovery_required",
error_code,message_key}` with `error_code` equal to the row code and
`message_key` drawn from the owner copy map; arbitrary JSON, free-form error
text, unknown keys, or secret-shaped values are rejected. These helpers are
the physical CHECK authority and have mutation/terminal fixtures for every
kind and error branch.

| Mutation | Closed body after common fields | Success |
| --- | --- | --- |
| `POST /upstream-keys` | `label,credential` | 201 plus key summary; creates the only staged row |
| `POST /upstream-keys/<id>/probe` | `profiles` is a nonempty unique manifest-ordered subset; full validation is the exact seven-ID list | 202 operation; never assigns |
| `POST /upstream-keys/<id>/assign` | `slot_no:1|2`; empty installation permits 1, one-key installation permits the empty slot 2 | 200 operation result after the applicable first/second-slot rules |
| `POST /upstream-slots/<slot>/replacement` | `label,credential` | 201 plus staged replacement summary |
| `POST /upstream-replacements/<id>/probe` | the same ordered subset contract | 202 operation; never swaps |
| `POST /upstream-replacements/<id>/commit` | no extra member | 200 operation result; atomically swaps and begins old-key retirement |
| `DELETE /upstream-keys/<id>` | no extra member | 200 retired-or-cleanup-quarantined result; only a staged row is accepted |
| `POST /upstream-slots/<slot>/disable` | no extra member | 200 result; excludes traffic without unassigning |
| `POST /upstream-slots/<slot>/enable` | no extra member | 200 result; requires current proofs and clears manual disable only |
| `POST /upstream-configuration/reset` | exact `confirmation:"RESET UPSTREAM PAIR"` | 202 queued reset; durable drain then atomically returns to zero assigned slots |
| `POST /downstream-credentials` | `label,scopes` | 201 result with plaintext exactly once |
| `POST /downstream-credentials/<id>/revoke` | no extra member | 200 revoked summary |
| `POST /operations/<target-id>/cancel` | new cancel operation ID plus expected generation | 202 cancel-operation snapshot; `Location` names the new cancel ID |

All paths in the table are under `/admin/api/v1`. An individual healthy
assigned key is removed only by replacement; the explicit all-slot reset is the
closed recovery for an unrecoverable one-key/two-key configuration. A pending
retirement does not block staged creation/replacement.
The replacement commit requires a fresh all-profile candidate proof and pair
proof, changes staged to assigned and old assigned to retirement-pending in one
transaction using the exact deferred-constraint update order above, and resets
every profile cursor to slot 1. Staged delete, replacement commit, enable/
disable, and automatic retirement conflict while any operation referencing the
affected key is `queued|running|cancel_requested|recovery_required`. Automatic
tombstone occurs only after those operation counts, live pins, and nondeleted
asset intent/object counts are all zero. Terminal proof receipts remain as
secret-free audit rows.

First and second assignment set the new slot `enabled=true`. Replacement copies
the occupied slot's enabled Boolean to the candidate in the same transaction;
a disabled slot therefore remains disabled until the explicit enable mutation.
The slot operation result, all seven eligibility rows, and cursor reset reflect
that committed value—no lifecycle default may decide it.

Reset is a long operation. Acceptance requires no other active operation, the
typed confirmation, and a fresh generation; it does not require the operator to
manually disable slots or clean a terminal staged candidate. Its worker owns the
durable intake transition in section 8, blocks new work, and waits for exact
zero admissions/live pins/in-flight attempts excluding only its own operation
and intent. Under the global lock order its one configuration transaction
moves any staged candidate and both assigned keys to `retired` or
`retirement_pending` according to asset cleanup, clears the published Magpie
singleton, resets
all cursors to Slot 1, and increments global/all-profile generations once. A
key with zero nondeleted assets becomes a `retired` tombstone immediately; a
key with known/unknown cleanup becomes `retirement_pending` with ciphertext and
worker authority retained. Proof/attempt/event rows remain audit evidence and
no downstream token is silently revoked. The result is management-ready,
traffic-unready, zero configured slots; fresh onboarding starts at Slot 1.
Before that configuration commit, failure/cancel restores the exact prior
intake mode and leaves keys/slots unchanged. After a readback-proved reset
commit, recovery can only reopen management intake and finish success; it never
recreates the old pair. Exact readback accepts only all-before or all-reset.
This destructive escape works with one
invalid assigned key, two invalid keys, or a survivor that cannot complete the
new pair proof; it never fabricates a survivor.

The operation pin is the durable join `admin_operation_keys` with primary key
`(operation_id,key_id)`, foreign keys to both rows, and role
`subject|candidate|survivor|retiring`. A synchronous mutation locks its complete
resource set, inserts its operation plus all join rows, excludes exactly its own
operation UUID when checking conflicts, applies the mutation, and terminalizes
that operation in the same transaction; rollback leaves neither mutation nor
pin. This self-exclusion rule applies uniformly to create/assign/replace/delete,
enable/disable, probe acceptance, and token issue/revoke. A long probe acceptance
commits `queued` plus its joins; its derived pin remains while state is
`queued|running|cancel_requested|recovery_required`. Cleanup does not delete
joins: terminalizing the operation releases the derived pin while retaining
the join as audit. Every other operation joined to any affected key is included,
including multi-key pair/replacement/reset operations. Cancel's own terminal row has
no key join; the target retains its joins until target terminal.

For `upstream_create|replacement_create`, both key and operation UUIDs are
preallocated. After configuration/resource locks, the one transaction inserts
the nonce allocation (its key FK is deferred), then the complete encrypted
`upstream_keys` row, then the `admin_operations` row, then
`admin_operation_keys`; it finally forces the deferred nonce FK and all envelope
checks before commit. No join is inserted before both immediate FK parents
exist. Other mutations lock or insert their key parent first, insert the
operation parent second, and insert joins third in UUID-network-byte order.
Rollback leaves none of the four rows, so a later reconciliation cannot observe
an impossible operation/key pin.

Probe and upstream reset are the only long-running mutations. Their accepted DTO and `GET
/operations/<id>` response are the closed `AdminOperation`:

```text
id: UUID;
kind: upstream_create|upstream_probe|upstream_assign|replacement_create|
      replacement_probe|replacement_commit|staged_delete|slot_disable|
      slot_enable|upstream_reset|downstream_issue|downstream_revoke|
      operation_cancel;
state: queued|running|cancel_requested|succeeded|failed|cancelled|
       recovery_required;
phase: accepted|planning|case_dispatched|case_cleanup|drain_requested|draining|
       committing|reopening|terminal;
progress: null |
 {variant:"probe",completed:Decimal,total:Decimal,current_case:CaseId|null} |
 {variant:"reset",stage:"withdrawing"|"waiting_zero"|"resetting"|"reopening"};
cancellable: Boolean; created_at: RFC3339; updated_at: RFC3339;
terminal_event_id: UUID|null;
evidence_target: DirectEvidenceTarget|null;
resource: null |
  {kind:"upstream_key",id:UUID} | {kind:"slot",id:"1"|"2"} |
  {kind:"downstream_credential",id:UUID} | {kind:"operation",id:UUID};
result: null |
  {variant:"upstream_key",key_id:UUID,lifecycle:<lifecycle>,slot_no:1|2|null} |
  {variant:"probe",key_id:UUID,verified_profiles:[<profile-id>],
   pair_voice_count:<decimal>|null,receipt_count:<decimal>} |
  {variant:"slot",slot_no:1|2,key_id:UUID,
   key_label_compact:String,key_fingerprint_short:LowerHex12,
   replaced_key:null|{id:UUID,label_compact:String,
                     fingerprint_short:LowerHex12},enabled:Boolean} |
  {variant:"upstream_reset",retired_key_ids:[UUID],
   cleanup_key_ids:[UUID],affected_keys:[{
     key_id:UUID,prior_lifecycle:"staged"|"assigned",
     prior_slot_no:1|2|null,
     label_compact:String,fingerprint_short:LowerHex12,
     final_lifecycle:"retired"|"retirement_pending"
   }],configured_slots:"0"} |
  {variant:"downstream_credential",credential_id:UUID,revoked:Boolean,
   secret_available:false} |
  {variant:"operation_cancel",target_operation_id:UUID,target_state:<state>};
error: null | {
  code:<operation-error>,message:<fixed>,retryable:false,
  failure_class:"invalid_credential"|"not_entitled"|"credits_exhausted"|
                "rate_limited"|"transient"|"protocol"|"request_contract"|
                "cancelled"|"interrupted"|"configuration_changed"|
                "cleanup_required"|"reset_failed_before_commit"|
                "reset_reopen_required"|"unknown",
  cleanup_state:"none"|"pending"|"unknown"|"complete",
  reset_commit_state:"all_before"|"all_reset"|null,
  next_retry_at:Timestamp|null,evidence_event_id:UUID,
  next_action:"wait_cleanup"|"delete_staged"|"quarantine_staged"|
              "replace_slot"|"reset_upstream_pair"|"retry_probe"|
              "refresh_then_retry"|"wait_reset_recovery"|"inspect_evidence"
}
```

`operation-error` is exactly `probe_failed` (`One or more proof cases failed.`),
`probe_interrupted` (`The probe was interrupted and requires reconciliation.`),
`probe_cancelled` (`The probe was cancelled.`), `configuration_changed`
(`Configuration changed before proof commit.`), `asset_cleanup_required`
(`Asset cleanup must complete before retrying.`), `provider_failure`
(`The provider did not complete the proof.`), `reset_cancelled`
(`The upstream reset was cancelled before commit.`), `reset_interrupted`
(`The upstream reset was interrupted before commit.`),
`reset_failed_before_commit` (`The upstream reset failed before commit.`), or
`reset_reopen_required`
(`The upstream reset committed and management intake must be reopened.`).
No other member or nullability is
allowed. Profile and verified-profile arrays use manifest order.
`pair_voice_count` is null when Magpie was not selected or fewer than two
current key maps exist; otherwise it is the exact intersection count.
Every error has an immutable owning event. `next_retry_at` is nonnull exactly
for `wait_cleanup|retry_probe` when a bounded future observation/cooldown exists
and null for destructive, refresh, reset, replace, or evidence-only actions;
the UI never derives a time or event from a global attention row.
Reset result key-ID arrays and `affected_keys` are strictly UUID-network-byte
sorted. `affected_keys` is the exact set observed under the reset transaction:
zero or one staged candidate plus every assigned identity. A staged row has
null `prior_slot_no`; an assigned row has its exact prior slot. Each
`retired_key_ids` member has one matching `final_lifecycle:"retired"` row and
each `cleanup_key_ids` member has one matching
`final_lifecycle:"retirement_pending"` row; those two disjoint arrays union to
the IDs in `affected_keys`. No retirement-pending row outside that reset is
projected. `label_compact` and `fingerprint_short` are copied from the locked
pre-reset rows into the immutable result; they remain available after immediate
retirement and across reload without consulting a historical snapshot. This
lets the terminal UI account for a staged candidate instead of silently
reporting only the two assigned slots.

State/nullability is exact. `queued|running|cancel_requested` has null result
and error, null `terminal_event_id`/`evidence_target`, and a kind-matching nonnull progress only for
`upstream_probe|replacement_probe|upstream_reset`;
`succeeded` has its kind-matching nonnull result and null error;
`failed|cancelled|recovery_required` has null result and nonnull error.
`phase:"terminal"` occurs iff state is terminal. Probe progress has
`0 <= completed <= total`, total equal to the durable ordered case count, and
`current_case` nonnull only while that exact case is dispatched/cleaning.
`cancellable:true` is limited to a probe that is queued, running between cases,
or has one supervised dispatched case, and to reset before its configuration
commit begins; every synchronous/terminal operation and every recovery-required
operation is false. Generated Rust/TS/JSON Schema uses
this discriminated union rather than three independently nullable fields.
Every terminal state has a nonnull immutable `terminal_event_id` and exact
`evidence_target:{kind:"event",id:<same ID>}`. That event joins its matching
`operation_terminal` Evidence, including synchronous and successful terminals;
an error's `evidence_event_id` must equal the same ID. No active operation
renders an Evidence trigger until this target is nonnull. Assignment/enable/
disable `slot` results have null `replaced_key`; replacement commit has the
exact prior key display projection and new key display projection. Thus a
reloaded terminal screen identifies both sides without querying a retired key.
Probe errors always have `reset_commit_state:null` and may use only the six
probe error codes. Reset errors always have `cleanup_state:"none"` and nonnull
`reset_commit_state`. `reset_cancelled|reset_interrupted` require
`all_before`, state `cancelled|failed`, and `refresh_then_retry`;
`reset_failed_before_commit` requires `all_before`, state `failed`, and
`inspect_evidence`. `reset_reopen_required` requires `all_reset`, state
`recovery_required`, and `wait_reset_recovery`. The reset worker automatically
resumes only the journaled reopen step; the UI polls the same operation ID and
never dispatches reset again. `wait_reset_recovery` renders
`reset_reopen_required` with the sole action label
`action_wait_reset_recovery`; that control is a safe GET/poll of the existing
operation, not a mutation or a retry. It displays the already committed
`affected_keys` projection only after the worker has attached it to the
terminal success, so the recovery state never claims a result early. Exact
all-reset readback changes that operation to succeeded after intake reopens.
No reset error can emit `retry_probe`, and no probe error can emit a reset
recovery action.
Only `upstream_probe|replacement_probe|upstream_reset` may persist
`queued|running|cancel_requested|failed|cancelled|recovery_required`; every
synchronous kind is either absent because its transaction failed or a terminal
`succeeded` row committed atomically with its mutation. Commit uncertainty is
reconciled to absent or that succeeded row by fingerprint; it never fabricates
a synchronous `failed` operation that the error union cannot represent.

The server, generated TS, and `recommendedPlan` share this exact next-action
table; first matching row wins:

| Condition | Exact `next_action` |
| --- | --- |
| probe operation `recovery_required`, or asset cleanup `pending|unknown` before its bounded foreground reconciliation ends | `wait_cleanup` |
| terminal create-unknown on a staged candidate after that bounded reconciliation | `quarantine_staged` |
| invalid credential on a staged candidate with no unknown cleanup | `delete_staged` |
| invalid credential on an assigned key with a distinct assigned survivor capable of replacement pairing | `replace_slot` |
| invalid credential on an assigned key with no such survivor, or target pair proof remains impossible | `reset_upstream_pair` |
| probe failure class rate limited, transient, cancelled, or interrupted with cleanup complete/none | `retry_probe` |
| reset cancelled or interrupted with exact `all_before` | `refresh_then_retry` |
| reset committed with management intake not yet reopened | `wait_reset_recovery` |
| reset failed before commit for another reason | `inspect_evidence` |
| failure class configuration changed | `refresh_then_retry` |
| failure class not entitled, credits exhausted, protocol, request contract, or unknown | `inspect_evidence` |

`retryable:false` still forbids replaying the same provider operation; a
`retry_probe` action creates a new UUID only after the old operation is terminal
and cleanup complete. Entitlement/credit failures may become retryable only
after the operator changes the NVIDIA account outside this product; protocol or
request-contract failures require contract evidence and a new reviewed source
candidate. Deleting a staged key remains an explicit secondary destructive
escape once assets/pins permit it, but is not falsely presented as the repair
for those conditions.

Every mutation response contains the operation created by that request, never
an implicitly substituted resource or target operation. In particular, cancel
creates an `operation_cancel` row whose `resource` names the target operation,
atomically changes an eligible target to `cancelled|cancel_requested`, and then
commits the cancel row `succeeded` with the `operation_cancel` result containing
the target's post-transaction state. Its 202 body is
`{"operation":<that cancel operation>}` and `Location` is
`/admin/api/v1/operations/<cancel-operation-id>`; the client polls the target ID
separately when its state is `cancel_requested`.

Every mutation response is `{"operation":<AdminOperation>}`. The successful
downstream 201 alone also has
`"credential":{"id", "token", "label", "scopes", "created_at"}`; its token
is the only secret-bearing response member and every other field follows the
read DTO. `GET /operations/<id>` returns `{"operation":<AdminOperation>}`.

202 includes `Location: /admin/api/v1/operations/<request-operation-id>` and the
complete current envelope. The planned ordered case list, expected
configuration/profile/schema
generations, forced key, and every case receipt are durable before dispatch.
Only one case is in flight. Cancel succeeds while queued or between cases; if a
case was dispatched, state becomes `cancel_requested`, that case and asset
cleanup finish under supervision, then no next case starts. On restart, queued
work resumes; a dispatched case without a terminal is finalized
`abandoned_after_restart`, assets are reconciled, and the operation becomes
`recovery_required`. Provider work is never replayed. A new probe needs a new
operation ID after reconciliation. The durable cleanup worker owns that
reconciliation: while any intent is unknown/nondeleted it keeps
`recovery_required`, exposes the exact next bounded retry time, and the UI's one
action is refresh/wait or open Evidence, not a guessed mutation. When all
nonambiguous descriptions/objects are absence-proved, one transaction finalizes
the operation `failed` with `probe_interrupted` and releases its pin. If a
`create_unknown` remains after the one bounded foreground cycle, the same
transaction terminalizes the operation with `asset_cleanup_required`, exact
unknown cleanup/evidence/retry fields, and `quarantine_staged`; the independent
watcher and key ciphertext, not the operation pin, retain cleanup authority.
Both branches permit a new operation ID only through their stated action and
never assert an impossible list-absence proof.

The one-time downstream plaintext exists only in the first 201 response from
the request that actually committed issuance. An identical later POST with the
same operation ID and fingerprint returns 200 with only
`{"operation":<stored succeeded operation>}`; it never repeats the
`credential` member or plaintext. Its
operation result exposes the credential ID and `secret_available:false`; a lost
201 is reconciled as issued-but-unrecoverable and the UI offers only revoke then
new issue. Lost mutation responses are otherwise reconciled by operation ID and
a fresh overview. The UI never guesses whether a swap occurred. New downstream
issuance and Hermes cutover require two assigned keys and all seven profiles
advertised; existing clients may continue in an explicitly degraded
one-eligible-key state.

Configuration generation changes are exact:

| Commit | Global generation | Affected profile generation/cursor |
| --- | --- | --- |
| staged create, staged delete, downstream issue/revoke | +1 once | unchanged |
| successful subset or full probe operation commit | +1 once | each selected profile +1; cursor unchanged |
| first/second assign, replacement commit | +1 once | all seven +1; every cursor reset to slot 1 |
| upstream-pair reset | +1 once | all seven +1; every cursor reset to slot 1 |
| key disable/enable | +1 once | all seven +1; cursor unchanged |
| automatic retirement tombstone | +1 once | unchanged |
| probe case progress/failure, cooldown, request attempt | unchanged | unchanged |

Every proof commit locks and compares its expected global/profile generations,
key UUID/fingerprint, profile-contract digest, and probe-suite digest. Snapshot
drift discards the candidate proof receipt as stale and cannot change
availability. A successful transaction stores the new identity-bound proof and
then advances snapshot generations; the increment cannot stale that proof.
Assignment/replacement atomically carries unchanged per-key proof rows by key
identity, selects the greatest already-current target pair proof for each
profile, and for Magpie CAS-publishes that proof's exact pair map in the same
transaction that installs the final ordered slots. A missing/mismatched target
proof or map aborts the whole assignment/swap and leaves both slots and the old
published singleton unchanged. Enable/disable changes no proof row. There is no
undefined separate “key generation.”

### 7.2 Round-robin, cooldown, classification, and failover

Reservation starts at the profile's `next_slot`, chooses the first eligible
slot in ring order, and advances to the successor. Excluded slots remain ring
anchors. One logical request has at most two sequential attempts and never
reuses a slot.

`Retry-After` on 429 accepts one OWS-trimmed unsigned decimal seconds value or
one IMF-fixdate and clamps the resulting delay to 1..300 seconds. The complete
delay authority is closed below. `n` is the matching profile-local stored
streak plus one at terminal finalize time. Equal jitter means an unbiased
CSPRNG integer number of milliseconds uniformly in the inclusive interval
`[ceil(cap_ms/2),cap_ms]`; no floating point or wall-clock seed is used.

| Terminal health class | Streak | Delay before `max(existing, now+delay)` | Per-event maximum deadline extension |
| --- | --- | --- | ---: |
| 429 with valid `Retry-After` | rate `n` increments | parsed/clamped 1..300 s; jitter is not added | 300 s |
| 429 with missing/invalid `Retry-After` | rate `n` increments | `cap=min(60s,2s*2^(n-1))`, equal jitter | 60 s |
| connect/TLS failure proved before first request byte | transient `n` increments | `cap=min(15s,1s*2^(n-1))`, equal jitter | 15 s |
| status-less transport failure after a request byte | transient `n` increments | same 1 s / 15 s transient formula | 15 s |
| local logical connect/header/body/poll/stream deadline | transient `n` increments | same 1 s / 15 s transient formula | 15 s |
| provider 408 | transient `n` increments | same 1 s / 15 s transient formula | 15 s |
| provider 500 or 502 | transient `n` increments | same 1 s / 15 s transient formula | 15 s |
| provider 503 or 504 | transient `n` increments | same 1 s / 15 s transient formula | 15 s |
| validated FLUX/SVD success-body `ERROR` | transient `n` increments | same 1 s / 15 s transient formula | 15 s |

Exponentiation saturates at the listed cap before multiplication and all
deadline arithmetic is checked monotonic-duration arithmetic. The wall-clock
UTC expiry persisted for owner display is derived once from that monotonic
delay; a backward wall-clock jump cannot shorten eligibility. A newer event
sets its deadline to the later of the unexpired stored deadline and its own
bounded deadline, so repetitions may preserve but never extend beyond the
latest event time plus that row's maximum. Rate and transient streaks/deadlines
are independent. Every other health-table row changes neither streak. The
product ignores all `x-ratelimit-*` headers in v3; it does not invent a
proactive quota grammar or a separate streak threshold.

Classification is exact:

- 401 is credential-global `invalid_credential` and quarantines every profile
  until an explicit full probe succeeds;
- 403 is always profile-local `not_entitled`; the body-code allowlist is empty;
- 402 is profile-local `credits_exhausted`;
- 429 and transient failures are profile-local cooldowns;
- provider 408 and validated FLUX/SVD `ERROR` are profile-local transient
  cooldown failures with no alternate; content policy has no health penalty;
- ordinary request 4xx is request-local and does not penalize health; and
- only an explicit successful profile probe clears entitlement/protocol
  quarantine, and only a successful full seven-profile sweep clears a
  credential-global 401 block.

Safe inference alternate is deliberately narrow: connect/TLS failure before
`upstream_request_maybe_sent`
or a fully received 401/402/403/429 response whose body absence is proved by
section 5.2's exact framing rule. Raw socket/parser buffering does not change
the decision and a response with absent/unknown/chunked/nonzero framing never
alternates. NVIDIA does
not document no-dispatch semantics for 408 or 5xx, so 408/500/502/503/504 never
alternate even when their body is empty. A status-less error after
`upstream_request_maybe_sent`, an ordinary request 4xx,
`upstream_first_body_byte_observed`, `downstream_handoff`, any 202, or any poll
response also forbids alternate. Neither event claims an OS downstream write,
and application frame completeness cannot weaken the first-upstream-byte
boundary. Asset
preparation uses only section 6.3's stricter absence proof.

Each reservation allocates a strictly increasing `health_seq` while holding its
key/profile row. Terminal update locks that row: an event with
`health_seq <= last_applied_health_seq` records its receipt but cannot mutate
health. For a newer 429, `n` is the stored rate streak plus one at finalize time,
not the reservation snapshot; transient failure does the same for its own
streak. Each sets its own deadline to `max(existing,new)` and leaves the other
deadline/streak unchanged. An ordinary newer success resets both streaks and
both rate/transient deadlines but never clears quarantine. Probe terminal
commit allocates a sequence newer than every already-started case and resets the
selected profile; a successful full sweep additionally clears global 401.
Cooldown expiry alone resets nothing. Eligibility uses the later of the two
deadlines; if equal, rate is the displayed/returned reason. This makes a late
older success unable to erase a newer failure.

When a first terminal class is one of the explicitly alternate-safe classes and
the second attempt succeeds, the logical request is success and returns only
the second validated representation. The first failure still applies its own
health/cooldown transition and remains immutable evidence; it is never exposed
as the public terminal. The error precedence below is consulted only when no
attempt succeeds. When two failed attempts produce different terminal classes,
the public terminal is the first present class in this fixed list; equal classes
choose the second attempt only for evidence attribution:

```text
upstream_auth_error > upstream_permission_error > upstream_credits_exhausted >
content_policy_violation > upstream_protocol_error >
upstream_response_too_large > upstream_asset_error > upstream_rate_limited >
upstream_timeout > upstream_unavailable > upstream_error > upstream_rejected
```

If reservation creates zero attempts, each permanent slot contributes exactly
one exclusion class: unassigned, manually disabled, or stale/missing proof is
`profile_unavailable`; credential-global 401 block is
`upstream_auth_error`; profile 403 quarantine is
`upstream_permission_error`; 402 quarantine is
`upstream_credits_exhausted`; protocol quarantine is
`upstream_protocol_error`; unexpired rate cooldown is
`upstream_rate_limited`; unexpired transient cooldown is
`upstream_unavailable`; and a reached local in-flight limit is `upstream_busy`.
If more than one condition applies to one slot, it uses the first in this exact
order: auth, permission, credits, protocol, rate, transient, capacity, profile
unavailable. The
zero-attempt public result is the first class present in this exact cross-slot
order:

```text
upstream_auth_error > upstream_permission_error > upstream_credits_exhausted >
upstream_protocol_error > upstream_rate_limited > upstream_unavailable > upstream_busy >
profile_unavailable
```

This rule closes mixed states such as quarantine plus cooldown or disabled plus
capacity and never fabricates an attempt. A nondeadline connect/TLS failure
proved before the first request byte creates an attempt terminal class
`upstream_unavailable`; if both sequential attempts fail that way, 503
`upstream_unavailable` wins.

If `upstream_rate_limited` wins, `Retry-After` is the rounded-up 1..300 seconds
to the earliest unexpired rate deadline among the exhausted candidates. No
other chosen class emits `Retry-After`. Preselection with both keys already
cooling uses the same rule without creating an attempt.

## 8. PostgreSQL, locks, attempts, and ambiguous commits

SQLx migrations are forward-only, checksum locked, and serialized by a
PostgreSQL advisory operation lock. Unknown/newer schema, drift, or app/schema
mismatch fails readiness. Constraints own UUID/digest sizes, lifecycle and
status enums, timestamps, nonnegative counters, scopes, slot uniqueness,
partial lifecycle cardinality, and cooldown pairs.

Every migration uses SQLx's default one-transaction execution. The operation
lock key is the fixed signed 64-bit value
`bigendian_i64(SHA-256(b"nblb:sqlx-operation-lock:v1\0"||schema_name)[0..8])`;
the migrator holds it on one dedicated `lock_conn` from before migration
transaction begin through commit and catalog readback. A lost `lock_conn`
immediately records `lock_lost`, abandons the connection, and cannot claim the
lock was held through reconnect; recovery opens a new connection, revalidates
the journal/generation and complete before/after catalog projection, reacquires
the same key, and performs only the exact readback or forward action authorized
by that journal. There is no overlapping migrator during this bounded
reacquisition.

Every migration uses SQLx's default one-transaction execution. Source scanning
rejects the `-- no-transaction` directive in any whitespace/case form and
rejects nontransactional DDL such as `CREATE INDEX CONCURRENTLY`, `VACUUM`,
`ALTER SYSTEM`, transaction control, or procedures that commit. The migrator
sets bounded lock/statement/idle-in-transaction timeouts before SQLx begins.
A fault at every statement/commit/response
boundary must read back either the complete prior schema with no
`_sqlx_migrations` row or the complete target schema plus the exact version/
checksum/success row. Any mixed catalog/object/trigger/ACL/function/policy/row
state is schema drift and never reruns automatically. Fresh and every supported upgrade path
prove this against real PostgreSQL; static checksum success alone is
insufficient.

### 8.1 Generation-bound PostgreSQL bootstrap

The derived image has one absolute volume mount
`/var/lib/postgresql/data` and exact `PGDATA=/var/lib/postgresql/data/pgdata`;
neither value is configurable. A fresh mount root becomes root:70 mode 0710 and
the `pgdata` child is created by the root parent as 70:70 mode 0700. The root
parent never runs `initdb` itself. It validates the canonical 43-character
unpadded-base64url generation secret plus LF, decodes exactly 32 bytes, and
derives three independent 32-byte values with HKDF-SHA256, salt equal to the
generation UUID's 16 network bytes, and respectively these literal info values:

```text
nblb:postgres:bootstrap-password:v1
nblb:postgres:migrator-password:v1
nblb:postgres:app-password:v1
```

Each derived value is encoded as 43 unpadded base64url ASCII characters for its
SCRAM password. The root process creates an anonymous pipe, writes only the
bootstrap password plus LF, and starts the audited `child-init` mode with that
read end fixed at descriptor 3. `child-init` clears groups/capability bounding,
ambient, permitted, inheritable, and effective sets with the same safe APIs as
section 4, changes all real/effective/saved IDs to 70:70, verifies
`CapEff=0,CapBnd=0,NoNewPrivs=1`, and directly execs exactly:

```text
/usr/local/bin/initdb
--pgdata=/var/lib/postgresql/data/pgdata
--username=postgres
--pwfile=/proc/self/fd/3
--auth-local=peer
--auth-host=scram-sha-256
--encoding=UTF8
--locale=C
--data-checksums
--no-instructions
```

The pipe is the only deliberately non-CLOEXEC descriptor and reaches EOF after
one LF; all other inherited descriptors are closed. Password bytes occur in no
argv, environment, file, log, or sentinel. Candidate tests inspect both child
and `initdb` `/proc/<pid>/status`/cmdline/environment while running and require
UID/GID 70, empty groups/capabilities, no secret, and the exact FD target.

Bootstrap creates `/run/nblb-pg-bootstrap` as 70:70 mode 0700 in the container's
private tmpfs and starts the new cluster only on that Unix socket, never TCP, as
UID/GID 70 with zero capabilities:

```text
postgres -D /var/lib/postgresql/data/pgdata
  -c listen_addresses=
  -c unix_socket_directories=/run/nblb-pg-bootstrap
  -c unix_socket_permissions=0700
  -c port=5432
  -c logging_collector=off
  -c log_statement=none
  -c log_connections=off
  -c log_disconnections=off
```

The root parent passes the two other derived passwords through separate
anonymous inherited FDs to a second UID/GID 70, cap-zero/no-new-privileges mode
of its in-image Rust catalog client. UID 70 is the image's fixed `postgres` OS
identity, so the client connects as local peer database role `postgres`, never
invokes `psql`, and uses only fixed identifiers plus bound password values. One
transaction creates roles `nblb_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB
NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1`,
`nblb_ledger_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1`,
`nblb_migrator LOGIN NOINHERIT`, and `nblb_app LOGIN NOINHERIT`; grants
`nblb_owner` and `nblb_ledger_owner` directly to `nblb_migrator` with
`ADMIN FALSE,INHERIT FALSE,SET TRUE`; there is no membership edge between the
two owner roles and `nblb_app` has no membership edge at all. It sets both login roles
`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION
LIMIT 32`, with their distinct derived passwords supplied only through the
fixed bound bootstrap function below while `password_encryption` is
SCRAM-SHA-256. After exact role readback
and commit, the client issues the one
necessarily nontransactional fixed statement `CREATE DATABASE nvidia_build_lb
OWNER nblb_owner TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE
'C'`; response ambiguity reconnects to `postgres` and accepts only exact absent
or exact target catalog state before an absent-only retry. It then connects to
that database and one transaction creates schema `nblb` owned by `nblb_owner`.
It revokes `CONNECT,TEMP` on the database from PUBLIC, grants CONNECT only to
`nblb_app,nblb_migrator,nblb_owner,nblb_ledger_owner`, revokes CREATE/USAGE on
schema `public`, and grants schema USAGE to the app plus both migration owner
roles; `nblb_owner` and `nblb_ledger_owner` alone have schema CREATE. The
physical table inventory is closed and exact (all are in schema `nblb` except
the explicitly schema-qualified SQLx table):
`routing_configuration,profile_routing_state,upstream_keys,key_profile_state,
downstream_credentials,admin_operations,mutation_intents,attentions,evidence,
request_terminals,legacy_migration_audit,intake_control,intake_admissions,runtime_epochs,
public_route_observations,live_qa_fault_permits,
upstream_attempts,upstream_asset_intents,upstream_asset_objects,events,
vault_nonce_allocations,key_profile_proofs,pair_proofs,magpie_key_voice_maps,
magpie_key_voices,magpie_pair_voice_maps,magpie_pair_sources,magpie_pair_voices,
published_magpie_map,admin_operation_keys,vault_key_binding,vault_key_generations`;
`nblb._sqlx_migrations` is the only migration-history table. Within the
`nblb` namespace (plus that explicitly qualified SQLx table), any other table,
view, foreign table, or sequence is schema drift; `pg_catalog`, `information_schema`,
`pg_toast`, and PostgreSQL system-owned objects are excluded by schema/owner
allowlist and are hashed only by the fixed server-version projection.
The generated `contracts/postgres/table-contract.v1.json` is the physical SQL
authority for every inventory row: each entry contains the exact ordered
columns/types/nullability, primary/unique/partial indexes, CHECK/FK names and
actions, trigger names/functions/enabled mode, owner/default ACL, and canonical
row projection. DTO diagrams never substitute for this manifest. The migration
generator compares its byte/hash to the checked-in SQL and a real-PostgreSQL
catalog projection; a missing manifest entry, extra column, changed FK action,
or unlisted state transition fails before readiness. Mutable tables use their
named transition guards above (including singleton `intake_control`, operation
and intent terminal transitions, runtime epochs, observations, credentials,
and QA permits); append-only tables have INSERT-only ACL plus immutable
triggers. `contracts/postgres/catalog-contract.v1.json` is the companion
function/trigger/default-ACL authority: every table trigger entry points to one
of its listed function names and records timing, level, enabled mode,
constraint/deferred status, plus a table-local body-digest slot. During this
design phase those slots are explicitly `null` and the top-level `phase` is
`design_contract`; this is the only permitted pre-implementation placeholder
state and is not a runtime/readiness claim. Before source freeze, the SQLx
migration generator replaces every null with the SHA-256 of the canonical
function/trigger body, sets `phase:"implementation_frozen"`, recomputes both
RFC 8785 JCS manifest hashes, and fails closed if any placeholder survives.
This closes DDL/constraint decisions without implementation-time guessing.

The credential guards are explicit: `nblb_guard_upstream_key_update` freezes
key ID/fingerprint/ciphertext/nonce/tag/envelope generation, staged intent and
slot identity after creation, and allows only the manifest lifecycle
transitions `staged -> assigned|retired` and `assigned ->
retirement_pending|retired` with the exact pin/asset/revoke predicates.
`nblb_guard_downstream_credential_update` freezes digest/version/scopes/ID and
allows only `active,revoked_at=NULL -> active,revoked_at=Timestamp`; it rejects
reactivation, digest replacement, scope changes, and DELETE. The same guards
are attached `ENABLE ALWAYS` and their transition matrices are catalog and
real-PostgreSQL fixtures, so app UPDATE privilege cannot erase credential
identity or revocation history.

`legacy_migration_audit` is a real v3 append-only table, not an untracked file:
`(id UUID PRIMARY KEY, transition_id UUID NOT NULL UNIQUE,
source_schema_sha256 BYTEA NOT NULL,
source_cursor_projection BYTEA NOT NULL, source_counter_projection BYTEA NOT
NULL, transformed_at TIMESTAMPTZ NOT NULL, receipt_sha256 BYTEA NOT NULL
CHECK(octet_length(receipt_sha256)=32), canonical_json BYTEA NOT NULL)` with
one unique row per legacy transition and an immutable INSERT-only guard. It is
included in the migration/state projection and is the exact storage named by
the field-level transformation manifest.

The app ACL is table-specific, never a broad CRUD grant. It has
SELECT/INSERT/UPDATE (no DELETE) on mutable projection rows
`{routing_configuration,profile_routing_state,upstream_keys,key_profile_state,
downstream_credentials,admin_operations,mutation_intents,intake_control,
runtime_epochs,live_qa_fault_permits}`. The append-only and projection
exceptions are separate, exact rules:

```text
public_route_observations: SELECT/INSERT only;
attentions: SELECT/INSERT/UPDATE, no DELETE;
evidence: SELECT/INSERT only;
request_terminals and legacy_migration_audit: SELECT/INSERT only;
```

the only
DELETE grant is on transient `intake_admissions`, and only the drain controller
may delete rows after the admission is terminal and the zero predicate is
proved. It has SELECT/INSERT/UPDATE on workflow rows
`{upstream_attempts,upstream_asset_intents,upstream_asset_objects}` (no row
delete; cleanup changes their closed state to `deleted`), SELECT/INSERT on
append-only `{events,vault_nonce_allocations,key_profile_proofs,pair_proofs,
magpie_key_voice_maps,magpie_key_voices,magpie_pair_voice_maps,
magpie_pair_sources,magpie_pair_voices}`, SELECT/INSERT on
`admin_operation_keys` (joins are retained as audit and are never deleted),
and SELECT/UPDATE on `published_magpie_map`. It has SELECT only on
`{vault_key_binding,vault_key_generations,nblb._sqlx_migrations}` and no access
to any other object. Upstream-key retirement, downstream-token revocation,
attempt/event history, runtime epochs, observations, mutation intents,
operations, and QA permits are tombstone/update paths; their ACLs intentionally
cannot erase audit rows. A named BEFORE UPDATE guard on attempts/assets rejects
changes to immutable identity/fingerprint/key/proof fields and permits only the
closed delivery, transport, cleanup, and terminal transitions.

`nblb_owner` has SELECT on all tables and is a database owner, so PostgreSQL
cannot ACL-restrict its direct DDL/DML; only the trusted signed migrator/ops
binaries may log in or `SET ROLE` to it, while the app never uses that role.
`nblb_ledger_owner` owns only `vault_nonce_allocations`, its sequence (if one is
introduced by a reviewed migration), and its trigger functions; schema CREATE is
granted only for the one migration transaction and revoked before it returns.
This v3 schema creates no SQL sequence objects: UUIDs and locked Decimal
counters are allocated by the app, so the expected sequence ACL projection is
empty and a nonempty catalog is drift. Default ACLs for both owner roles revoke
PUBLIC; app grants are applied only to the listed groups; `_sqlx_migrations` is
SELECT-only for app; and every function has PUBLIC/APP EXECUTE revoked unless
it is a named trigger function or the fixed operation-function allowlist.
Bootstrap first executes fixed `REVOKE ALL ON ALL TABLES, SEQUENCES, FUNCTIONS
IN SCHEMA nblb FROM PUBLIC,nblb_app`, then applies only this generated matrix.
The schema/database/table/sequence/function/default-ACL projection uses
canonical role and object names rather than cluster-local role OIDs. Each
transaction is read back before commit and the final projection is read after
reconnect.
Migration connects as `nblb_migrator` and executes `SET ROLE nblb_owner` for
ordinary migrations or the separately declared `SET ROLE
nblb_ledger_owner` block for ledger DDL; neither owner can become the other and
the app can never set either role. Role/database attributes, membership
options, grants, and the absence of every other edge are read back in the same
transaction before commit.

PostgreSQL utility grammar cannot bind an `ALTER ROLE ... PASSWORD $1` value,
so the bootstrap never attempts it. Rust derives each SCRAM-SHA-256 verifier
locally with 4,096 iterations, a distinct 16-byte CSPRNG salt, and PostgreSQL's
exact verifier encoding, then opens one catalog transaction with
`SET LOCAL log_statement='none'` and `SET LOCAL log_min_error_statement='panic'`.
It installs one fixed `pg_temp.nblb_set_login_scram(target text, verifier text)`
PL/pgSQL function. The function has two literal branches only
(`nblb_migrator`,`nblb_app`), validates the canonical verifier grammar, and in
each branch executes a fixed literal `ALTER ROLE <literal> PASSWORD ` plus
`quote_literal(verifier)`; every other target raises the fixed
`bootstrap_role_rejected` without echo. Calls use extended-protocol binds
`SELECT pg_temp.nblb_set_login_scram($1,$2)`. The superuser reads the two exact
`pg_authid.rolpassword` values inside the transaction, constant-time compares
them, stores only the secret-free binding projection, drops the temp function
with session end, and zeroizes password/verifier buffers. Commit ambiguity on a
new connection accepts only all roles absent or both exact role attributes plus
both verifier bindings; mixed state is failed attention and no statement text,
bind value, verifier, salt, or key reaches logs/evidence.

The canonical `pg_hba.conf` bytes, in this exact first-match order, are generated
from a committed fixture and hash-bound by the manifest/sentinel:

```text
local all postgres peer
local nvidia_build_lb nblb_migrator scram-sha-256
local nvidia_build_lb nblb_app scram-sha-256
local all all reject
hostnossl nvidia_build_lb nblb_migrator 0.0.0.0/0 scram-sha-256
hostnossl nvidia_build_lb nblb_app 0.0.0.0/0 scram-sha-256
hostnossl nvidia_build_lb nblb_migrator ::0/0 scram-sha-256
hostnossl nvidia_build_lb nblb_app ::0/0 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::0/0 reject
```

Canonical `postgresql.conf` fixes UTF8/C cluster identity,
`listen_addresses='*'`, port 5432, `ssl=off`, `fsync=on`,
`full_page_writes=on`, `synchronous_commit=on`, and `wal_level=replica`,
`password_encryption='scram-sha-256'`,
`unix_socket_directories='/run/postgresql'`,
`unix_socket_permissions=0700`, and secret-free bounded logging. Steady `db`
execs only `postgres -D /var/lib/postgresql/data/pgdata` as Compose user 70:70;
the private data network and no host port are its network boundary. Compose
pins one data-network name/CIDR with only `app`, `migrate`, and `db` members;
the verifier rejects any second network, published DB address, DNS alias, or
foreign container attachment. The HBA `hostnossl` rules are therefore scoped
to that isolated network contract, and both network membership and the three
durability settings are included in startup/restore readback and evidence.

After catalog commit the bootstrap server performs a fast clean shutdown and
its exact PID/socket absence is read back. The UID70 child writes both canonical
config files through exclusive same-directory temp, file fsync, rename, PGDATA
fsync, and hash readback. Validation then starts a new UID70 server with the
same Unix-only command-line overrides above; those overrides win over the
steady `listen_addresses='*'` only for validation, while canonical HBA is live
and therefore exercises SCRAM for both login roles. It shuts down cleanly before
sentinel publication. No steady TCP listener exists during `db-init`.

Application code never builds or accepts a textual database URL. Its exact
SQLx `PgConnectOptions` is host `db`, port `5432`, database
`nvidia_build_lb`, username `nblb_app` or `nblb_migrator` according to mode,
the corresponding derived password, `ssl_mode=Disable`, application name
`nvidia-build-lb-app` or `nvidia-build-lb-migrate`, and startup option
`search_path=nblb,pg_catalog`; every unspecified option remains SQLx's pinned
default. Debug/Display/telemetry of the options and pool connection errors is
redacted. Migrate mode immediately `SET ROLE nblb_owner` and verifies it before
the advisory lock; app mode rejects an effective role other than `nblb_app`.

The closed root-only sentinel is
`/var/lib/postgresql/data/.nblb-bootstrap.json`, root:root 0600, JCS UTF-8 with
no LF:

```text
PgSentinel={version:1,generation_id:UUID,transaction_id:UUID,
 pgdata:"/var/lib/postgresql/data/pgdata",postgres_version:"17.9",
 system_identifier:Decimal,encoding:"UTF8",locale:"C",checksums:true,
 database:"nvidia_build_lb",owner_role:"nblb_owner",
 ledger_owner_role:"nblb_ledger_owner",
 migrator_role:"nblb_migrator",app_role:"nblb_app",
 postgresql_conf_sha256:LowerHex64,pg_hba_sha256:LowerHex64,
 catalog_projection_sha256:LowerHex64,password_binding:LowerHex64}
```

`password_binding` is excluded while serializing the preceding fields as JCS
`S`. Its key is
`HKDF-SHA256(secret32,salt=generation_uuid.network_bytes,
info=b"nblb:postgres:sentinel-binding:v1",L=32)` and its value is lowercase hex
`HMAC-SHA256(key,b"nblb:postgres:sentinel:v1\0"||u32be(len(S))||S)`.
The sentinel catalog projection is deliberately **bootstrap-only**. It covers
system identifier, exact four role attributes, the two and only two migrator
membership edges with their ADMIN/INHERIT/SET options, absence of app/owner
cross-membership, database owner/base CONNECT policy, SCRAM verifier algorithm/
iteration count, and config hashes, never verifier salt/stored/server keys. It
excludes schema/table/sequence/function/trigger objects, `_sqlx_migrations`,
object/default ACLs, and application rows because SQLx creates or changes those
after `db-init`. Those are owned by the separately versioned schema/catalog
projection and startup migration verifier, not by a mutable sentinel. This binds the supplied
generation secret without storing a reusable password oracle outside the
root-only volume authority.

Fresh initialization is journaled at
`/var/lib/postgresql/data/.nblb-bootstrap-journal.json`, root:root 0600, with
generation/transaction UUIDs, exact root/PGDATA/config/image/password-file
metadata hashes, `published:false`, nullable closed failure, and phases
`prepared|root_prepare_requested|root_prepared|
pgdata_create_requested|pgdata_created|initdb_requested|
initdb_complete|bootstrap_start_requested|bootstrap_started|
roles_commit_requested|roles_committed|database_create_requested|
database_created|schema_commit_requested|catalog_committed|config_install_requested|
config_installed|validation_started|validated|sentinel_install_requested|
sentinel_installed|committed|partial_initdb|failed_attention`.
Requested phases precede every filesystem/process/catalog side effect and
completed phases require exact readback/fsync. The only pre-`prepared` write is
exclusive creation of the journal in an otherwise empty regular mounted root;
the journal stores exact observed and target root tuples.
`root_prepare_requested` precedes changing that fresh mount root to root:70
0710. Before `initdb_requested`, only the one bootstrap journal and empty owned
PGDATA may exist and recovery resumes.
Loss during `initdb` becomes `partial_initdb`; neither binary deletes, rewrites,
recursively chowns, nor reruns `initdb` in that volume. After
`initdb_complete`, each roles/database/schema ambiguity accepts only its exact
phase-before or phase-after projection and rolls forward; mixed roles/grants/
database/config are attention. Sentinel install ambiguity likewise
accepts only absent or exact target bytes.

Only root `nblb-ops` may discard an entire `partial_initdb` volume, and only
after active/candidate pointers plus every release/restore journal prove the
Docker volume label names that transaction/generation, `published:false`, no
container has it mounted, and no backup/evidence references it. It records
`volume_discard_requested`, removes that exact Docker volume by ID, proves
absence, records `volume_discarded`, and allocates a different volume name and
bootstrap transaction. An unlabeled, published, mounted, sentinel-only,
unknown, or foreign partial volume is never cleaned automatically.

On an existing volume `db-init` performs validation only. It requires exact
root/PGDATA/sentinel/journal ownership and modes, a committed journal, matching
generation/system identifier/version/config/bootstrap-catalog hashes, and the sentinel
HMAC. It starts the same Unix-socket-only server and performs real SCRAM logins
as both `nblb_app` and `nblb_migrator` using the newly derived values; the latter
must successfully `SET ROLE nblb_owner`, while the former must fail that SET and
pass a rolled-back CRUD privilege fixture. It then cleanly stops PostgreSQL,
fsyncs PGDATA and mount parent, and exits. Missing sentinel with no matching
partial journal, a failed SCRAM login, a foreign file/config, or any catalog/
metadata drift fails without reinitialization or ownership repair.
Post-bootstrap schema, owner/default ACL, ledger trigger, and migration-history
drift is checked immediately afterward by migrate/app against the immutable
generation schema manifest. Neither process updates the root sentinel; a
normal first migration therefore cannot make bootstrap validation stale.

Durable intake authority is the database, not process memory:

```text
intake_control(singleton=true,epoch BIGINT,mode open|draining|closed,
               owner_kind reset|release|restore|null,owner_id UUID|null,
               prior_mode open|closed|null,phase requested|quiescing|quiescent|
               commit_irreversible|reopen_requested|null,updated_at)
intake_admissions(id UUID,epoch BIGINT,class public|admin_mutation|probe_case|
                  cleanup,operation_id UUID nullable,started_at)
```

Admission takes the singleton row first. `open` commits one admission row before
request/provider work; terminalization deletes that exact row in the same
transaction as its durable terminal. Owner GETs/health are always read-only and
need no admission. `draining|closed` rejects new public work with fixed
`service_unavailable` and new owner mutations with admin `intake_draining`, but
the already accepted owner operation whose UUID equals `owner_id` is the sole
self-excluded controller. No new probe case or cleanup network dispatch starts;
already dispatched work finishes/reconciles. Quiescent means zero admission
rows except the owner, zero live pins/in-flight provider tasks, and no other
nonterminal operation.

Root release/restore controls intake only through
`/run/nvidia-build-lb-control/control.sock`, an app-created Unix stream socket
owned 65532:65532 mode 0600 in a host tmpfs directory created by tmpfiles with
the exact rule `d /run/nvidia-build-lb-control 0730 root 65532 - -`; Compose
bind-mounts that directory read-write into the app and never creates it from a
container; the infrastructure PR installs the exact rule in
`/etc/tmpfiles.d/nvidia-build-lb.conf` and its verifier proves the directory
exists before Compose start. This is separate from the read-only host
runtime bind so the root host `nblb-ops` helper can connect without a Docker
socket. The server requires Linux
`SO_PEERCRED.uid=0`, accepts one bounded JCS command
`{version:1,transaction_id:UUID,action:"withdraw"|"open",
reason:"release"|"restore",expected_epoch:Decimal}`, and returns a closed
secret-free transition DTO. The only other accepted command shape is section
14's generation-bound, one-use live-QA fault permit while its root file exists;
without that file the action discriminator is rejected before DB work. The two
schemas share framing/peer authentication but no fields or inferred defaults.
There is no TCP/HTTP drain or QA route. Reset uses its
already authenticated operation row and the same DB transition functions.

Withdraw first commits `requested` with incremented epoch, owner and exact
prior mode, then readback permits cancellation/wait and finally commits
`quiescent`. App restart preserves draining/closed mode, marks old-epoch
admissions abandoned only through their normal receipt/asset reconciliation,
and never auto-opens. Before an irreversible reset/release pointer decision,
failure or cancel commits `reopen_requested`, CAS-restores exact prior mode, and
readbacks it. After `commit_irreversible`, only the journaled target may request
open; a crash resumes that direction. Every release/reset journal phase naming
withdraw/open binds the intake epoch, owner UUID, exact row bytes/hash, and
requested/completed DB transition. This closes reset self-exclusion and
state-forward release/rollback without an unavailable hidden admin endpoint.

The global row-lock order is:

```text
intake_control singleton -> intake_admissions (UUID network-byte order) ->
request_terminals (request UUID network-byte order) ->
runtime_epochs (boot ID,process start ticks) ->
public_route_observations (observed_at,observation UUID) ->
routing_configuration -> vault_key_binding singleton ->
vault_key_generations (generation UUID network-byte order) ->
vault_nonce_allocations (generation UUID network bytes, nonce unsigned bytes) ->
profile_routing_state (profile-manifest ordinal) ->
upstream_keys (UUID network-byte order) -> key_profile_state
  (key UUID network bytes, profile-manifest ordinal) ->
key_profile_proofs (key UUID network bytes, profile-manifest ordinal,
                    proof revision order) ->
magpie_key_voice_maps/key voices
  (key UUID network bytes, manifest ordinal, key-proof revision,
   voice unsigned-UTF-8 order) ->
pair_proofs (profile-manifest ordinal, pair-proof revision order) ->
magpie_pair_voice_maps/sources/voices
  (manifest ordinal, pair-proof revision, slot then voice unsigned-UTF-8 order) ->
published_magpie_map singleton -> live_qa_fault_permits
  (permit UUID network-byte order) -> admin_operations (UUID network-byte order) ->
admin_operation_keys (operation then key UUID network-byte order) ->
upstream_attempts (UUID network-byte order) ->
upstream_asset_intents (attempt UUID network bytes, ordinal,
                        row UUID network bytes) ->
upstream_asset_objects (intent then provider-asset then row UUID network bytes) ->
events (occurred_at,id) -> evidence (event UUID, evidence UUID) ->
attentions (code,resource_key) ->
downstream_credentials (UUID network-byte order)
```

Every path that creates, retires, restores, or rebinds a vault generation uses
the same `vault_key_binding -> vault_key_generations ->
vault_nonce_allocations` order (generation IDs in network-byte order), including
the source-to-target handoff and retirement receipt. No code may lock a nonce
or generation before the singleton binding, and no path may take the binding
after a nonce. This fixed order is part of the SQLx race fixtures and the
catalog/readiness contract.

These comparators are executable rather than prose aliases. Every UUID lock
query orders by `uuid_send(column)` ascending; Rust sorts the same RFC 4122
16 network bytes. Every profile query joins the immutable seven-row registry
and orders by its unique `ordinal` 0..6, never profile text. Voice columns are
UTF-8 under database `COLLATE "C"`; SQL `ORDER BY voice COLLATE "C"` and Rust
unsigned UTF-8 byte comparison must produce identical fixture order. Startup
checks server encoding UTF8 and the required `C` collation before readiness.

Writers never acquire a skipped earlier class after a later one. Admission/
reserve first locks `intake_control`, inserts/locks its admission, then locks
configuration, one profile cursor, selects candidates in slot-ring order but
locks every candidate key by UUID, then sorted profile states, per-key proofs,
Magpie key maps/voices, matching pair proofs, Magpie pair maps/sources/voices,
the published singleton when applicable, the preallocated attempt, and the
already-ordered `vault_nonce_allocations` rows.
Finalize first reads immutable attempt/admission metadata without a lock, then
locks `intake_control`, its exact admission, configuration, key/profile,
attempt, assets, and any required `vault_nonce_allocations` rows in global
order and revalidates identity. A
draining owner permits already-admitted rows to finish but rejects new rows.
Discovery queries may collect immutable UUIDs without row locks, but every
writer then issues explicit per-class `SELECT ... FOR UPDATE ORDER BY` steps.
It never relies on a multi-table join's planner order to lock operations,
operation-key joins, proofs, or assets.

A drain/reset/release transaction locks only `intake_control`, records
`requested|quiescing` and commits, releasing the row before it waits. It never
holds that lock while polling admissions/provider/operations. Each observation
is a new read transaction. The final quiescent transaction relocks
`intake_control`, then the remaining admission rows in UUID order, and succeeds
only at the defined zero/self-excluded set. Thus terminalizers can always take
the earlier locks and delete their admissions; the controller cannot wait on a
row lock it still owns.

Re-encryption and key-generation binding changes lock
`routing_configuration`, then the singleton `vault_key_binding`, then insert/
lock nonce allocations before any ciphertext row in UUID order. Ordinary
request finalize never locks the binding after a key/profile row; it uses the
generation captured and verified at reservation.

Nonce rows are new unique-index reservations, not a late exception to this
order. Create inserts its allocation after routing configuration and before its
new key. Restore/migration first discovers immutable row UUIDs without locks,
inserts all target-generation allocations ordered by row UUID/allocation UUID,
then locks and revalidates the complete key set in normal order; any drift rolls
back both reservations and envelopes. No path first locks a key and then tries
to reserve a nonce.

Probe planning/result commit locks configuration, affected profile cursors,
sorted affected keys, profile states, per-key proofs, Magpie key maps/voices,
pair proofs, Magpie pair maps/sources/voices, the published singleton when
applicable, its admin operation and operation-key joins, attempts, assets, and
ledger; it compares every expected generation before applying proof. Asset
create/upload/delete transactions lock key, key/profile, attempt, intent,
sorted object rows, then ledger; no lock is held across network. Cleanup worker
uses the same order. Retirement locks configuration, all profile cursors,
sorted affected keys, their profile states and per-key proofs, Magpie key
maps/voices, affected pair proofs, Magpie pair maps/sources/voices, the
published singleton, related admin operations/joins, attempts, intents,
objects, then ledger before proving zero operation/pin/asset counts and
tombstoning. Enable/disable and replacement use that same class order over
their sorted affected set. Upstream reset uses the retirement order over both
slots, all proofs/maps/publication, operation joins, attempts/assets, then
ledger; it performs no network while locked. Token issue/revoke locks configuration, required
profile cursors, both assigned keys, profile states, per-key proofs, Magpie key
maps/voices, pair proofs, Magpie pair maps/sources/voices, the published
singleton, its admin operation/joins, the credential, then ledger. Migration,
backup, restore, and release first hold the stable host operation lock, then the
DB advisory lock, then rows in this order. Hermes cutover holds operations then
Hermes host locks and invokes the closed admin API; only the app transaction
locks database rows, and the cutover helper never holds a DB advisory lock
across HTTP.

One logical request increments the downstream and logical-request counter once.
Every selected key increments key/profile request and inserts one `started`
attempt once. Polls and asset calls are parts of that attempt. Exactly one
terminal increments success or failure/cancellation. Network starts only after
the started transaction is known committed. Terminal wire output occurs only
after terminal commit.

All request attempts, asset intents/objects, and synchronous admin mutations use
preallocated UUIDs and an idempotency fingerprint. The exact additional
formulas are below. UUID is its 16 RFC 4122 network-order bytes; integer fields
are unsigned big-endian at the shown width; ASCII/UTF-8 strings are preceded by
their shown byte length; generations, proof revisions, and health sequences are
constrained to `0..2^63-1`, with proof revisions positive when nonzero.

```text
request_digest =
  SHA-256(b"nblb:validated-request:v1\0" ||
          u16be(method_ascii_len) || method_ascii ||
          u16be(path_ascii_len) || path_ascii ||
          u32be(exact_input_body_len) || exact_input_body_bytes)

attempt_fingerprint =
  SHA-256(b"nblb:upstream-attempt:v1\0" || u8(origin) || attempt_uuid ||
          owner_uuid || downstream_credential_uuid_or_zero || key_uuid ||
          u8(slot_no_or_zero) || u16be(attempt_or_case_ordinal) ||
          u16be(profile_id_utf8_len) || profile_id_utf8 ||
          u64be(configuration_generation) ||
          u64be(profile_generation) ||
          u64be(global_health_seq) || u64be(profile_health_seq) ||
          u64be(key_proof_revision_or_planned_revision) ||
          u64be(pair_proof_revision_or_zero) ||
          magpie_key_map_digest_or_zero32 ||
          magpie_pair_map_digest_or_zero32 || request_digest)

asset_intent_fingerprint =
  SHA-256(b"nblb:asset-intent:v1\0" || intent_uuid || attempt_uuid || key_uuid ||
          u32be(media_ordinal) || u16be(mime_ascii_len) || mime_ascii ||
          u64be(decoded_byte_count) || raw_media_sha256 ||
          u16be(description_ascii_len) || description_ascii)

asset_object_fingerprint =
  SHA-256(b"nblb:asset-object:v1\0" || object_row_uuid || intent_uuid ||
          provider_asset_uuid || u8(provenance))
```

`origin` is `0=public,1=probe`; `provenance` is
`0=response,1=reconciliation`. For public origin, `owner_uuid` is the logical
request UUID, downstream UUID is real, slot is 1 or 2, ordinal is 1 or 2, and
the input body is the exact validated ingress body. Reservation stores and
fingerprint-binds the exact per-key and matching ordered-pair proof revisions
used for eligibility. For Magpie it also binds both selected key-map and
published pair-map digests and retains the byte-exact selected voice/locale in
the immutable attempt row; all four digest bytes are zero for non-Magpie. This
lets an old live-pin request finish against its pinned map after an atomic
replacement publishes a new singleton. For probe origin,
`owner_uuid` is the admin operation UUID, downstream is sixteen zero bytes,
slot is the assigned/target slot or zero for an unbound first candidate,
ordinal is the manifest case ordinal, the preallocated target per-key/pair
revision is bound even before proof commit (zero only when that pair does not
yet exist), and input body is the exact immutable fixture body before adapter
rewriting. The Magpie voice-list discovery attempt necessarily binds zero map
digests because its validated response creates the candidate key map;
subsequent per-key synth attempts bind that key-map digest, and pair synth
attempts bind the forced key's source-map digest plus the already computed
candidate pair-map digest, whose source rows themselves bind both key maps. No
future provider result is guessed into a fingerprint.
`request_digest` is streamed before body
disposal and only the digest is stored. An exact readback requires fingerprint equality and
byte-equality of every immutable field used above. A later state is accepted
only when it is reachable by the closed state machine from the state being
reconciled and becomes the recovery authority; an impossible/backward state or
any immutable mismatch is corruption and withdraws readiness.

A normal `queued|running|cancel_requested` probe operation is governed by
section 7.1 and is not commit ambiguity. Ambiguous
database reconciliation is:

| Readback | Start/reservation | Terminal/mutation |
| --- | --- | --- |
| exact committed row | continue once; never insert again | use stored result; never emit/mutate twice |
| absent | insert reservation once because network has not begun | insert only the preselected terminal/mutation; never replay network |
| pending/in-progress | withdraw readiness and fail-stop | withdraw readiness; emit no terminal success |
| conflicting fingerprint/state | fail-stop as corruption | fail-stop as corruption |
| DB unavailable/unknown | perform no network and fail-stop | preserve selected outcome in memory, emit nothing, fail-stop |

Startup marks old-epoch `started` attempts `abandoned_after_restart` without
replay, releases their in-flight projection only after durable finalization,
and resumes asset cleanup. It never fabricates success.

## 9. Encryption and secret custody

The master key path is
`/opt/nvidia-build-lb/secrets/generations/<key-generation-uuid>/vault.key`.
Parents are root:root 0700 and the file is root:root 0600 containing exactly 32
raw bytes. Creation uses an exclusive same-directory temp file, fsync, atomic
rename, and parent fsync. The active generation record selects the path; online
key rotation is not supported.

The infrastructure Compose contract exposes only the three flat active
projections `/opt/nvidia-build-lb/secrets/admin_token`,
`/opt/nvidia-build-lb/secrets/vault_master_key`, and
`/opt/nvidia-build-lb/secrets/db_password`. A root-owned release step binds
those projections byte-for-byte to the selected generation files (or creates
them from the generation before first start), records their inode/hash tuple,
and never lets the container resolve a generation glob. Generation directories
remain the custody/backup authority; the flat names are stable deployment
mounts and are replaced only through the journaled CAS path.

Upstream credentials use AES-256-GCM, random 12-byte nonce, 16-byte tag, and
envelope v2. Random means a nonce successfully reserved in section 7's
permanent `(key_generation_id,nonce)` allocation ledger; generation code may
not encrypt before that insert and may not fall back after 32 collisions.
`fingerprint` is raw 32-byte SHA-256 of credential bytes. AAD is:

```text
b"nvidia-build-lb:vault:v2\0" || key_generation_uuid.network_bytes ||
row_uuid.network_bytes || fingerprint_raw_32
```

After decrypt, the fingerprint is recomputed and compared constant-time. The
`vault_key_binding` singleton stores key-generation UUID, random 32-byte salt,
and a 32-byte verifier `HMAC-SHA256(master_key,
b"nvidia-build-lb:vault-key-binding:v2\0" ||
generation_uuid.network_bytes_16 || salt)`. Its singleton CHECK is exactly
`(key_generation_id,salt,verifier all NULL) OR (generation FK nonnull,
octet_length(salt)=32,octet_length(verifier)=32)`, and the generation state is
`active` for the binding and `retired` only after every ciphertext row is
retired. SQLx migration creates exactly one unbound row with null
generation/salt/verifier and no key-bearing rows. Fresh
bootstrap or legacy import supplies the selected 32-byte master key and
generation UUID, locks that row, writes salt/verifier, read-verifies it, and
only then may insert ciphertext-bearing rows, all in one transaction. Migration
never invents a key. Startup verifies the binding, requires every ciphertext
row's generation to exist in `vault_key_generations` with the binding generation
or a retained retired generation, rejects an unknown generation/extra nonce
projection, and authenticates every ciphertext-bearing staged, assigned, and
retirement-pending row before readiness. The root file is mounted only at the fixed root-only canonical
prestart path; app-mode `nblb-init` copies it to private tmpfs as `vault.key`,
while migrate mode has no vault mount or readable copy.

`nblb-vault` owns non-`Serialize`/`Debug`/`Display` secret leases. Store returns
encrypted records; service opens one lease after reserve commit and transfers
ownership to the NVIDIA adapter. Authorization materializes immediately before
dispatch. The same lease is held for asset/poll/cleanup work and then dropped.
Owned buffers are zeroized; no claim is made that TLS/HTTP library internal
copies can be synchronously zeroized, so process isolation, no logging, bounded
lifetime, and image/core-dump policy remain required.

Every process that can read an admin/upstream/downstream/PostgreSQL/vault/backup
plaintext—including `nblb-init`, gateway, catalog bootstrap, release/restore/
Hermes helpers, and their secret-bearing children—sets hard and soft
`RLIMIT_CORE=0` and Linux `PR_SET_DUMPABLE=0` through audited safe wrappers
before opening the first secret FD/path. It immediately verifies
`getrlimit(RLIMIT_CORE)==(0,0)` and `PR_GET_DUMPABLE==0`; failure exits 70 with
only `dump_policy_failed`. Compose also fixes `ulimits.core` soft/hard zero for
every app/DB-init/migrate/DB service, and no container gains SYS_PTRACE. The
flags survive privilege drop/exec and are rechecked by PID 1 and each root
operations helper after fork/exec but before custody. No code changes
`core_pattern`, and host/systemd-coredump settings cannot override a
non-dumpable process.

Candidate QA loads canary-shaped secrets into each process class, triggers the
release `panic=abort` path and ordinary fatal signals, then proves no core file,
coredump journal/storage entry, helper output, container log, or evidence byte
contains the canary. `/proc/<pid>/limits`, `PR_GET_DUMPABLE`, Docker ulimits,
capabilities, crash exit, and host failed-unit/coredump query are bound receipts.
This test runs before any real NVIDIA plaintext is entered.

New downstream token digest is raw
`SHA-256(b"nvidia-build-lb:downstream:v2\0" || full_token_ascii_bytes)`, where
the input includes the exact `nblb_ds_` prefix and 43-character unpadded
base64url payload, with `digest_version=downstream_v2`. Authentication first rejects shape,
length, non-ASCII, padding, and alphabet errors, then hashes the complete bearer,
loads the one digest candidate, and performs constant-time 32-byte comparison;
it never decodes a supplied token to choose different digest bytes. Plaintext is
returned once. Legacy raw SHA-256 digest bytes may exist only in revoked audit
tombstones with
`digest_version=legacy_sha256_v1`.

The admin token file is `/opt/nvidia-build-lb/secrets/admin_token`, root:root 0600,
containing the exact token plus one LF. The app accepts exactly one trailing LF
from disk, holds the token in secret memory, and compares constant-time.
Its Compose source is the fixed root-only canonical prestart mount and app-mode
`nblb-init` copies it to private tmpfs as `admin.token`; migrate and PostgreSQL
have no admin-token mount. No HTTP endpoint creates or recovers it. After
`compromised_hold` and before the
first merged-image host release candidate starts, root runs
`/usr/local/sbin/nblb-ops admin-token initialize --handoff-fd <n>`. It generates
32 CSPRNG bytes, exclusive-temp/fsync/rename/parent-fsync/readback installs the
file only if absent, and writes the token exactly once with LF to an already
open operator-controlled TTY/anonymous FD; stdout/stderr, argv, environment,
journal, shell tracing, and evidence receive no token. A secret-free handoff
receipt records file metadata, generation time, and success, never token
bytes/digest. The same host file is mounted read-only into candidate and
production, so ordinary restore does not rotate it. Pre-merge fixture/browser
candidates use isolated ephemeral test tokens and never create or read this
host file.

Loss or suspected disclosure is recovered only by root
`nblb-ops admin-token rotate --handoff-fd <n>` under the operations lock. A
root-only journal records old/new file tuples and
`prepared|new_install_requested|new_installed|app_restart_requested|
app_restarted|verified|committed|rollback_started|old_restore_requested|
old_restored|rollback_restart_requested|rollback_restarted|rolled_back|
failed_attention`. The journal binds exact old/new file bytes in root-only
same-filesystem backup/staging paths plus uid/gid/mode/device/inode/nlink/hash
and the exact app container tuple. Each requested phase precedes its file or
runtime side effect; a completed phase requires exact readback, and recovery
accepts only the journaled old/new and stopped/running tuples. Rotation installs
a fresh token, restarts the app, proves management readiness with the new
bearer, then commits; failure restores the exact old file and process. Every
browser session becomes invalid and must
log in again. A missing/corrupt file makes management readiness false; on the
same host rotate/recover uses the journal, while a genuinely new restore host
uses `initialize`. The old plaintext is never displayed by any command.
`/etc/nvidia-build-lb/runtime.env` becomes root:root 0600 and contains exactly
`NBLB_ACTIVE_GENERATION_FILE=/opt/nvidia-build-lb/state/active-generation.json`
and `NBLB_ADMIN_TOKEN_FILE=/opt/nvidia-build-lb/secrets/admin_token`, one LF-terminated
assignment each in that order. It is a non-secret locator projection and may
not contain image, volume, schema, source, vault-generation, host, port, or
token values; another key fails staging. No `/etc/nvidia-build-lb/.lock` is
created. The observed `/etc/nvidia-build-lb/runtime.env.lock` remains the
legacy lock: its zero bytes, inode, root ownership, nlink one, and 0644 mode are
preserved as provenance and never silently converted into the v3 authority.
The only v3 operation lock is `/run/lock/nvidia-build-lb-ops.lock`.

Lock authority moves exactly once. The transition journal records the original
`runtime.env` complete bytes/uid/gid/mode/hash and legacy-lock metadata. The
operator holds v3 operations lock first and the legacy lock exclusively second,
stops/disables every discovered legacy writer, proves no process has the legacy
Compose wrapper open or running, atomically installs the closed v3
`runtime.env` at root:root 0600, and installs only v3 entrypoints that acquire
the operations lock. No v3 entrypoint acquires or treats the legacy file as a
sentinel. The retired Python image contains no production entrypoint, and old
`production-compose.sh` is absent from installed automation and rejects the v3
closed runtime schema before a write. Before active-generation pointer swap,
rollback under both locks restores the exact prior runtime bytes/mode and
legacy automation state; after the legacy generation becomes
`retired_no_serve`, the lock file is retained but no writer is restored. Thus
there is no interval in which two independent locks authorize writers. The
current `runtime.env` 0644 mode is a rollout defect corrected only inside this
journaled transition before any new secret is installed.

## 10. Legacy-to-v3 transformation and generation activation

The last pre-hold observation of the legacy database was PostgreSQL 17.9 at
Alembic `0005`, with two upstream rows, three downstream rows (one active), 127
admin events, 58 attempt receipts, and zero live pins. Those mutable counts are
host evidence, not export constants: hold entry and the final snapshot record
fresh secret-free counts and reject drift in safety predicates rather than
silently assuming this observation. The database is not empty and is never
discarded or migrated in place.

### 10.1 Transition hold and export

The section 1.1 `compromised_hold` is already active before implementation. Once
both PRs are user-merged, export re-acquires
`/run/lock/nvidia-build-lb-ops.lock`, then the legacy `runtime.env.lock`
exclusively, then the Hermes lock, and holds all three through export and the
section 10.3 authority transition. It re-proves legacy app stopped, Hermes paused,
every discovered legacy writer disabled/quiescent, `live_pin=0`,
`pending_attempt=0`, database unchanged, and both exposed key IDs revoked.
Failure preserves the hold and deletes nothing. Only after the v3 runtime
projection and entrypoints are durable is the legacy lock released forever;
steady state then uses operations->Hermes only.

One `REPEATABLE READ READ ONLY DEFERRABLE` transaction exports a snapshot. It
remains open until the safe-state projection/digests and `pg_dump --snapshot`
both finish. The stable host lock is acquired before DB advisory/migration lock.
A separately restored legacy drill proves schema, IDs, counters, events,
digests, scheduler cursor, and ciphertext decryptability without an upstream
call.

### 10.2 Field-level transformation manifest

The exporter emits a closed, row-counted transformation manifest:

| Legacy source | Preserved projection | Intentional v3 transformation |
| --- | --- | --- |
| upstream key | ID, `source_label_present=false`, raw fingerprint, counters, timestamps, event links | nullable audit label remains null; lifecycle `retired`, slot null, enabled false, ciphertext/nonce/tag absent, provider revoke event ID/time |
| downstream token | ID, label, legacy digest/version, scopes, counters, timestamps, event links | active false, one ID-specific `legacy_token_revoked` event and `revoked_at`; no active token row |
| scheduler singleton | source cursor and counters in `legacy_migration_audit` | every v3 profile cursor initialized deterministically to slot 1 |
| attempts/events | IDs, safe status classes, counts, timestamps, relationships | closed enum mapping with source schema/version; no raw body/header/path |
| schema metadata | Alembic revision and source schema hash | new SQLx schema generation/hash |

`preserved_projection_digest` covers only fields that must be byte-equivalent.
`intentional_transform_digest` covers source values plus the exact expected v3
tombstone/revocation/cursor mapping. The target is compared with the generated
expected target digest; source and target whole-state digests are not expected
to match. Every count and ID has a bijection or an explicit discard reason.
Legacy downstream tokens become zero active tokens. No legacy plaintext enters
v3.

### 10.3 Root-owned generation tuple and CAS

Each generation manifest at
`/opt/nvidia-build-lb/generations/<generation-uuid>/manifest.json` is root:root
0600 under a 0700 parent and contains exactly:

```text
generation UUID; app registry digest; PostgreSQL registry digest; DB volume;
PostgreSQL-auth generation UUID and absolute secret path;
vault-key generation UUID and absolute path; SQLx schema SHA-256; source commit;
infra commit; Compose manifest SHA-256; transform/backup manifest SHA-256;
state-source generation UUID and final state-projection SHA-256;
schema-compatibility manifest SHA-256;
activation eligibility (`state_forward_source | retired_no_serve`); created_at
```

`/opt/nvidia-build-lb/state/active-generation.json` is the sole production
tuple. It is RFC 8785 JCS UTF-8 with no final LF and exactly:

```text
{"version":1,"generation_id":UUID,
 "manifest_path":"/opt/nvidia-build-lb/generations/<same UUID>/manifest.json",
 "manifest_sha256":LowerHex64,
 "activation_eligibility":"state_forward_source"|"retired_no_serve"}
```

The manifest path component and embedded generation UUID must match byte for
byte; target manifest bytes/hash, schema, image, volume, and secret generations
are verified before the pointer is eligible. Initial activation is an exact
absent-to-candidate CAS whose synthetic prior eligibility is
`retired_no_serve`; later releases require exact prior bytes. The release
journal's prior pointer bytes/hash are therefore explicitly nullable only for
that initial branch.

The one-time legacy-lock authority transfer is separately journaled at
`/opt/nvidia-build-lb/state/authority-transition.json`, JCS root:root 0600. It
contains exactly version, transaction UUID, phase, the complete legacy lock
file tuple, complete before/target tuples and SHA-256 for generated
`runtime.env`, v3 release/updater entrypoints and systemd/tmpfiles artifacts,
the nullable exact prior active pointer, exact target pointer, the hold-journal
hash, legacy/v3 container tuples, and nullable closed failure. Its phases are:

```text
AuthorityTransition={
 version:1,transaction_id:UUID,phase:AuthorityPhase,
 hold_journal_sha256:LowerHex64,legacy_lock:PathFileTuple,
 backup_root:DirectoryTuple,
 runtime:ManagedAuthorityFile,
 entrypoints:[ManagedAuthorityFile],
 system_integration:[ManagedAuthorityFile],
 legacy_writer_identity_manifest:{bytes_base64url:Base64Url,
                                  sha256:LowerHex64,size:Decimal},
 legacy_writers:[LegacyWriter],
 file_progress:{direction:"forward"|"rollback",
                group:"runtime"|"entrypoints"|"system_integration"|null,
                ordinal:Decimal|null},
 prior_pointer:null,
 expected_target_pointer:{bytes_base64url:String,sha256:LowerHex64,generation_id:UUID},
 writer_authority:{path:"/opt/nvidia-build-lb/state/writer-authority.json",
                   before:null,target:PathFileTuple,
                   rollback_tombstone_path:AbsolutePath,
                   rollback_tombstone:PathFileTuple|null},
 legacy_app:ContainerTuple,
 failure:"hold_invalid"|"lock_tuple_changed"|"file_tuple_changed"|
         "pointer_tuple_changed"|"container_tuple_changed"|
         "backup_failed"|"restore_failed"|"side_effect_failed"|
         "tuple_unknown"|null
}
LegacyWriterIdentityManifest={
 version:1,
 enumerators:[{ordinal:1|2|3|4|5,
               kind:"systemd_graph"|"scheduled_entries"|
                    "installed_entrypoints"|"docker_graph"|"live_processes",
               count:Decimal,projection_sha256:LowerHex64}],
 writers:[LegacyWriterIdentity]
}
LegacyWriterIdentity=
 {ordinal:Decimal,writer_id:UUID,kind:"systemd_unit",unit:String,
  fragment:PathFileTuple,dropins:[PathFileTuple]}
|{ordinal:Decimal,writer_id:UUID,kind:"entrypoint"|"scheduled_entry",
  authority:PathFileTuple,
  managed_file:{group:"entrypoints"|"system_integration",
                ordinal:Decimal}|null}
|{ordinal:Decimal,writer_id:UUID,kind:"container",name:String,
  compose_project:String|null,compose_service:String|null,
  image:String,config_sha256:LowerHex64,
  managed_file:{group:"entrypoints"|"system_integration",
                ordinal:Decimal}|null}
LegacyWriter=
 {identity:<systemd_unit LegacyWriterIdentity>,
  before:SystemdWriterObservation,
  current_observation:SystemdWriterObservation,
  quiesced_observation:SystemdWriterObservation|null,
  restored_observation:SystemdWriterObservation|null,
  state:"discovered"|"stop_requested"|"stopped"|
        "disable_requested"|"disabled"|"verified"|
        "restore_enable_requested"|"restore_enabled"|
        "restore_start_requested"|"restored"}
|{identity:<entrypoint|scheduled_entry|container LegacyWriterIdentity>,
  before:ProcessWriterObservation,
  current_observation:ProcessWriterObservation,
  quiesced_observation:ProcessWriterObservation|null,
  restored_observation:ProcessWriterObservation|null,
  state:"discovered"|"stop_requested"|"stopped"|
        "disable_requested"|"disabled"|"verified"|
        "restore_enable_requested"|"restore_enabled"|
        "restore_start_requested"|"restored"}
SystemdWriterObservation={
 enabled:"enabled"|"disabled"|"static"|"indirect",
 active:"active"|"inactive",masked:Boolean,
 invocation_id:LowerHex32|null,main_pid:Decimal,control_pid:Decimal,
 cgroup_pids:[Decimal]}
ProcessWriterObservation={reachable:Boolean,running:Boolean,
 pids:[{pid:Decimal,start_ticks:Decimal,boot_id:UUID}],
 container:ContainerTuple|null}
ManagedAuthorityFile=
 {group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:PathFileTuple,
  before_bytes_base64url:Base64Url,
  target_expected:ExpectedAuthorityFile,backup:null,installed:null,restored:null,
  restore_swap:{path:AbsolutePath,phase:"empty",tuple:null},
  rollback_tombstone_path:null,rollback_tombstone:null,
  state:"planned"|"backup_requested"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:PathFileTuple,
  before_bytes_base64url:Base64Url,
  target_expected:ExpectedAuthorityFile,
  backup:{path:AbsolutePath,tuple:PathFileTuple},installed:null,restored:null,
  restore_swap:{path:AbsolutePath,phase:"empty",tuple:null},
  rollback_tombstone_path:null,rollback_tombstone:null,
  state:"backed_up"|"install_requested"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:PathFileTuple,
  before_bytes_base64url:Base64Url,
  target_expected:ExpectedAuthorityFile,
  backup:{path:AbsolutePath,tuple:PathFileTuple},installed:PathFileTuple,
  restored:null,
  restore_swap:{path:AbsolutePath,phase:"empty",tuple:null},
  rollback_tombstone_path:null,rollback_tombstone:null,
  state:"installed"|"restore_stage_requested"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:PathFileTuple,
  before_bytes_base64url:Base64Url,
  target_expected:ExpectedAuthorityFile,
  backup:{path:AbsolutePath,tuple:PathFileTuple},installed:PathFileTuple,
  restored:null,
  restore_swap:{path:AbsolutePath,phase:"staged_prior",tuple:PathFileTuple},
  rollback_tombstone_path:null,rollback_tombstone:null,
  state:"restore_staged"|"restore_exchange_requested"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:PathFileTuple,
  before_bytes_base64url:Base64Url,
  target_expected:ExpectedAuthorityFile,
  backup:{path:AbsolutePath,tuple:PathFileTuple},installed:PathFileTuple,
  restored:PathFileTuple,
  restore_swap:{path:AbsolutePath,phase:"displaced_target",tuple:PathFileTuple},
  rollback_tombstone_path:null,rollback_tombstone:null,state:"restored"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:null,before_bytes_base64url:null,
  target_expected:ExpectedAuthorityFile,backup:null,installed:null,restored:null,
  restore_swap:null,
  rollback_tombstone_path:AbsolutePath,rollback_tombstone:null,
  state:"planned"|"backup_requested"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:null,before_bytes_base64url:null,
  target_expected:ExpectedAuthorityFile,backup:null,installed:null,restored:null,
  restore_swap:null,
  rollback_tombstone_path:AbsolutePath,rollback_tombstone:null,
  state:"backed_up"|"install_requested"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:null,before_bytes_base64url:null,
  target_expected:ExpectedAuthorityFile,backup:null,installed:PathFileTuple,
  restored:null,restore_swap:null,
  rollback_tombstone_path:AbsolutePath,rollback_tombstone:null,
  state:"installed"|"restore_absence_requested"}
|{group:"runtime"|"entrypoints"|"system_integration",ordinal:Decimal,
  path:AbsolutePath,before:null,before_bytes_base64url:null,
  target_expected:ExpectedAuthorityFile,backup:null,installed:PathFileTuple,
  restored:null,restore_swap:null,
  rollback_tombstone_path:AbsolutePath,
  rollback_tombstone:PathFileTuple,state:"restored"}
ManagedRestoreSwap=
 {path:AbsolutePath,phase:"empty",tuple:null}
|{path:AbsolutePath,phase:"staged_prior"|"displaced_target",
  tuple:PathFileTuple}
ExpectedAuthorityFile={path:AbsolutePath,sha256:LowerHex64,uid:Decimal,
 mode:Octal4,gid:Decimal,size:Decimal,nlink:1}
DirectoryTuple={path:AbsolutePath,uid:0,gid:0,mode:"0700",
                device:Decimal,inode:Decimal,nlink:Decimal}
```

```text
prepared|legacy_writers_quiesce_requested|legacy_writers_quiesced|
originals_backup_requested|originals_backed_up|
runtime_install_requested|runtime_installed|
entrypoints_install_requested|entrypoints_installed|
system_integration_requested|system_integration_installed|
candidate_pointer_prepare_requested|candidate_pointer_prepared|
authority_switch_requested|v3_authority_active|
legacy_lock_release_authorized|committed|
rollback_started|writer_authority_restore_requested|
writer_authority_restored|system_integration_restore_requested|
system_integration_restored|entrypoints_restore_requested|
entrypoints_restored|runtime_restore_requested|runtime_restored|
legacy_writers_restore_requested|legacy_writers_restored|
rolled_back_hold|failed_attention
```

The backup root is exactly
`/opt/nvidia-build-lb/authority-transition-backups/<transaction-uuid>/`,
root:root 0700. At `prepared`, each managed target byte sequence and path is
fixed as `ExpectedAuthorityFile`, but no install inode/device is invented.
`target_expected.path` equals `path`; `before` and
`before_bytes_base64url` are either both null or both nonnull. A nonnull before
requires a nonnull backup after `backed_up`, has no rollback-tombstone path,
and prebinds a unique same-directory restore-swap path in
`restore_swap:{phase:"empty"}`. The path is absent and tuple null until rollback
staging.
An absent before has a null backup forever and, already at `prepared`, a unique
same-directory `rollback.<transaction-uuid>.<group>.<ordinal>.tombstone` path;
its tuple remains null until a rollback moves the exact installed inode there,
and `restore_swap` is null forever.
This prebinding prevents an absence restore from choosing a name after a crash.

Writer discovery is closed before `prepared`. The root scanner enumerates (1)
the systemd unit/dependency graph and effective fragments/drop-ins, including
timer/service enablement, active, masked, InvocationID and complete cgroup PID
sets; (2) `/etc/cron.d`, `/etc/cron.{hourly,daily,weekly,monthly}` and root's
crontab; (3) root-owned installed entrypoints in `/usr/local/bin`,
`/usr/local/sbin`, `/usr/local/libexec`, and the exact nblb paths under `/opt`;
(4) Docker containers and Compose service/project labels that can mount the
legacy DB, publish 2456, or execute the legacy migrate/app image; and (5) live
`/proc` executable, cwd, command-line path, mount, and open-file references to
the captured legacy Compose/wrapper/DB tuple. It never scans credential file
contents or records environment values. The decoded
`legacy_writer_identity_manifest` is exactly the closed
`LegacyWriterIdentityManifest`: it contains one strictly increasing ordinal per
discovered persistent/scheduled writer plus a hash/count receipt for every one
of those five enumerators, in fixed ordinal order
`systemd_graph,scheduled_entries,installed_entrypoints,docker_graph,live_processes`.
The five receipts describe the stable authority/coverage projection: the live
process scan carries captured process references as coverage anchors, so a
quiesced process disappearing is an observation change, not a missing identity.
It excludes `before`, `current_observation`,
`quiesced_observation`, `restored_observation`, state, PIDs, InvocationIDs, and
runtime container IDs. A container identity instead uses its stable name,
Compose labels, image, and config digest; its observed runtime ID belongs only
to `ProcessWriterObservation`. The current minimum
expected set is the legacy app container, the stopped migrate service/container
descriptor, and the legacy `production-compose.sh`, `backup.sh`, and
`restore.sh` installed/manual entrypoints; a missing expected row or an
additional discovered row is recorded rather than ignored and requires the
same state machine. Arbitrary future interactive root commands are outside the
persistent-writer inventory because root can bypass any local control; all
existing automated, installed, scheduled, and running authority is in scope.
The manifest is assembled entirely in memory, canonicalized, and embedded as
base64url bytes in the first atomic AuthorityTransition journal write; its
decoded size/hash and each structured `legacy_writers[].identity` projection
must agree. Each writer begins with `current_observation == before` and null
quiesced/restored observations. Only the structured row's current observation,
milestone observations, and state may advance; the identity bytes never change. No
external discovery file or host mutation precedes `prepared`.

`legacy_writers_quiesce_requested` precedes each writer's ordinal stop then
disable sequence. For a systemd row, `stop_requested` is durable before stop,
`stopped` requires its captured InvocationID's MainPID/ControlPID and all cgroup
PIDs zero, `disable_requested` precedes disable/mask, and `disabled` requires
the effective enabled/masked state. For a container, stop requires exact
container ID/config/restart-policy CAS; disable means restart policy `no` plus
no running process. For an entrypoint/scheduled row, stop proves its captured
PID set zero and disable is complete when every discovered service/timer/cron/
container reference to it is persistently disabled; `reachable` means reachable
from that captured automated graph, not that interactive root has lost execute
power. Its bytes remain untouched until backup and are later replaced only
through the matching managed-file state. `legacy_writers_quiesced` requires
every row `verified`, a fresh discovery whose identity/coverage manifest is
byte-identical to the immutable saved manifest, and each row's
`current_observation == quiesced_observation` equal to that fresh observation.
The quiesced observation must show no new InvocationID/PID or running container
and no listener at the legacy public tuple; it is not required to equal
`before`. The delayed agent-apps updater remains governed by
the compromised hold/updater contract rather than being mislabeled as a legacy
nblb writer; its unit tuple is nevertheless rechecked as an external restart
source.

`originals_backup_requested` precedes each `backup_requested` row and exclusive
creation of one group/ordinal-named 0600 backup for every nonnull original. The
complete original bytes are also stored as canonical unpadded base64url in the
root-only journal; decoding must reproduce `before.sha256` and `before.size`,
and the backup file must reproduce the same bytes. `originals_backed_up`
requires every row `backed_up`, file/parent fsync, and byte/hash readback.
Forward installation is group order runtime, entrypoints, system integration,
then ascending ordinal. Immediately before a side effect `file_progress` names
that group/ordinal and the row is `install_requested`; completion requires the
full installed tuple to equal `target_expected` plus its observed
device/inode, then sets `installed`. With no file in flight,
`file_progress.group/ordinal` are null. External evidence contains only tuple
hashes, never original bytes or secret paths.

The operations, legacy-runtime, and Hermes locks remain held in that order.
Every v3 writer refuses to run before `v3_authority_active`; every legacy writer
is still persistently inhibited and the legacy container is hold-stopped with
restart policy `no`. The active-generation pointer remains exactly absent
through this journal. `authority_switch_requested` is durable before the exact
absent-to-target CAS of `writer-authority.json`, whose target JCS bytes are
`{"version":1,"authority":"v3","transaction_id":<same UUID>,
"active_pointer_state":"absent_initial"}`. On recovery, absent writer marker is
accepted only before that request, target bytes only at/after it, and any
nonabsent active pointer, other marker, file tuple, or container tuple is
`failed_attention`. Runtime and entrypoint
partial tuples roll forward under the still-held/adopted legacy lock; they
never select writer authority by file presence. `legacy_lock_release_authorized`
requires all v3 artifacts and writer marker read back, the future candidate
pointer bytes/hash prepared, and the legacy `retired_no_serve` marker. After
`committed`, no steady v3 path opens the legacy
lock and no legacy app/admin API is started.

Before `legacy_lock_release_authorized`, any implementation/install/readback
failure may enter `rollback_started` while all three locks and persistent hold
remain effective. If the writer marker was never switched, its restore phases
are an exact absence no-op. If it was switched, the requested phase precedes an
exact `RENAME_NOREPLACE` CAS from the marker to the prebound absent rollback
tombstone, followed by parent fsync and exact tombstone/marker-absence
readback. Managed files then restore in strict reverse group and reverse
ordinal order. For a prior file, `restore_stage_requested` is durable before
rebuilding the journal bytes with exact prior uid/gid/mode at its prebound
restore-swap path. File/parent fsync and tuple/hash readback set
`restore_swap:staged_prior` and row `restore_staged`; the original backup remains
untouched. `restore_exchange_requested` then precedes
`renameat2(RENAME_EXCHANGE)` with the exact installed live inode. After the
exchange, the live path must equal `before` bytes/metadata and the same prebound
swap path must contain the exact installed target inode; parent fsync/readback
changes the nested phase to `displaced_target` and the row to `restored`. That
retained swap path is the displaced-target authority, so a crash immediately
after exchange is recognized from the exact two possible path/tuple pairings
and never searches or chooses a new name. A prior absence uses
`restore_absence_requested` before
`renameat2(RENAME_NOREPLACE)` from the exact installed target to its prebound
rollback tombstone, followed by parent fsync and exact target-absence/tombstone
readback; it is never an unchecked unlink. `file_progress` switches to
`direction:"rollback"` and names each row before its requested state; the row's
`restored`/tombstone nullability proves whether bytes or absence were restored.
Every restore has its durable requested/completed state above. Only the exact
before or installed tuple is accepted; mixed/unknown files stop at
`failed_attention`.
In a final existing-file row, `restored` is the exact live prior tuple,
`restore_swap.phase="displaced_target"`, and its tuple is the exact former
installed inode at the prebound swap path; rollback tombstone fields are null.
In a final absent row, `restored` and `restore_swap` are null and the exact
installed inode is the nonnull rollback tombstone. No other final nullability
validates.

After all managed files are restored, `legacy_writers_restore_requested`
walks writer rows in reverse ordinal. It restores only the exact pre-transition
enable/mask/reachability state and then, only when `before.active` or
`before.running` was true, issues a separately durable start request and
requires a new exact InvocationID/container/PID tuple. In this initial
transition the already-complete compromised hold makes those saved states
quiescent and persistently inhibited, so the ordinary result is a verified
no-start restoration; the algorithm does not infer that from phase names.
`legacy_writers_restored` requires every row `restored`, a fresh immutable
identity/coverage projection equal to the saved manifest, and a fresh mutable
observation exactly equal to that row's nonnull `restored_observation`. A row
whose saved active/running state is restarted records the newly observed
InvocationID and PID set or container ID in `restored_observation`; those runtime
identifiers are expected to differ from the stopped pre-transition invocation
and are never forced to equal `before`. A saved inactive/stopped row instead
proves the corresponding quiescent restored observation. `rolled_back_hold` then proves every
original/absence, absent writer marker, stopped legacy/Hermes containers, and
unchanged hold journal. It permits a new reviewed transition transaction but
does not weaken the hold. At or after `legacy_lock_release_authorized`, this
journal is roll-forward only and never recreates legacy writer authority.

`/opt/nvidia-build-lb/state/release-journal.json` binds the committed authority-
transition journal/marker hashes, exact prior and
candidate pointer bytes/hashes, release transaction UUID, final-state snapshot
digest, prior/candidate activation eligibility, nullable exact prior/candidate
Hermes `active.json` bytes/hashes, exact app/Hermes container tuples, nullable
`rollback_origin_phase`, `pointer_restore_mode:"prior_rename"|
"initial_absence"`, and a nullable initial-absence tombstone path/expected file
tuple, and `verification_mode:management_only|traffic_ready`.

Final snapshot and DB/vault generation materialization is not hidden inside the
two parent phase names. Before `final_snapshot_started`, the parent creates
`/opt/nvidia-build-lb/state/release-materialization/<release-uuid>.json` as a
separate JCS root:root 0600 child journal and records
`{path,transaction_id,terminal_sha256:null}`. Only a child `committed` file may
be rehashed into nonnull `terminal_sha256` and advance the parent to
`final_snapshot_restored`. The child is exactly:

```text
ReleaseMaterializationJournal={
 version:1,transaction_id:UUID,release_id:UUID,
 phase:ReleaseMaterializationPhase,
 source:
  {authority:"initial_absent",generation_id:UUID,
   active_pointer_sha256:null,
   authority_transition_sha256:LowerHex64,
   writer_authority_sha256:LowerHex64,
   prepared_selector_sha256:LowerHex64,
   generation_manifest_sha256:LowerHex64,db_volume_id:LowerHex64,
   schema_sha256:LowerHex64,vault_binding_sha256:LowerHex64}
 |{authority:"active_generation",generation_id:UUID,
   active_pointer_sha256:LowerHex64,
   authority_transition_sha256:null,writer_authority_sha256:null,
   prepared_selector_sha256:null,
   generation_manifest_sha256:LowerHex64,db_volume_id:LowerHex64,
   schema_sha256:LowerHex64,vault_binding_sha256:LowerHex64},
 snapshot:
   {state:"planned",snapshot_id:null,owner_backend_pid:null,owner:null,
    dump_child:null,dump:LocalObjectTxn,state_projection_sha256:null,
    toc_count:null,toc_sha256:null,owner_acl_projection_sha256:null,
    sqlx_migrations_sha256:null}
  |{state:"open",snapshot_id:String,owner_backend_pid:Decimal,
    owner:SnapshotOwner,dump_child:SnapshotChild,
    dump:LocalObjectTxn,state_projection_sha256:null,
    toc_count:null,toc_sha256:null,owner_acl_projection_sha256:null,
    sqlx_migrations_sha256:null}
  |{state:"sealed",snapshot_id:String,owner_backend_pid:Decimal,
    owner:SnapshotOwner,dump_child:SnapshotChild,
    dump:LocalObjectTxn,state_projection_sha256:LowerHex64,
    toc_count:Decimal,toc_sha256:LowerHex64,
    owner_acl_projection_sha256:LowerHex64,
    sqlx_migrations_sha256:LowerHex64},
 target:
   {state:"planned",generation_id:UUID,db_volume_name:String,
    db_volume_id:null,postgres_secret_generation_id:UUID,
    postgres_secret:null,vault_key_generation_id:UUID,vault_key:null,
    generation_manifest:null}
  |{state:"allocated",generation_id:UUID,db_volume_name:String,
    db_volume_id:LowerHex64,postgres_secret_generation_id:UUID,
    postgres_secret:PathFileTuple,vault_key_generation_id:UUID,
    vault_key:PathFileTuple,generation_manifest:null}
 |{state:"manifested",generation_id:UUID,db_volume_name:String,
    db_volume_id:LowerHex64,postgres_secret_generation_id:UUID,
    postgres_secret:PathFileTuple,vault_key_generation_id:UUID,
    vault_key:PathFileTuple,generation_manifest:PathFileTuple},
 reencrypt:ReencryptTxn,
 target_projection_sha256:LowerHex64|null,
 failure:"source_changed"|"snapshot_lost"|"dump_failed"|
         "target_tuple_changed"|"bootstrap_failed"|"restore_failed"|
         "catalog_normalize_failed"|
         "reencrypt_state_mixed"|"manifest_failed"|
         "projection_mismatch"|"cleanup_failed"|"tuple_unknown"|null
}
ReleaseMaterializationPhase=
 "prepared"|"snapshot_open_requested"|"snapshot_open"|
 "dump_write_requested"|"dump_sealed"|
 "snapshot_projection_requested"|"snapshot_sealed"|
 "target_allocate_requested"|"target_allocated"|
 "db_bootstrap_requested"|"db_bootstrapped"|
 "dump_restore_requested"|"dump_restored"|
 "catalog_normalize_requested"|"catalog_normalized"|
 "reencrypt_plan_requested"|"reencrypt_plan_sealed"|
 "reencrypt_commit_requested"|"reencrypted"|
 "generation_manifest_install_requested"|"generation_manifest_installed"|
 "target_projection_verify_requested"|"target_projection_verified"|
 "committed"|"cleanup_requested"|"cleaned"|"failed_attention"
```

The materialization snapshot union has executable child-lifecycle rules. The
`planned` branch has no owner backend, exported snapshot, dump child, dump
bytes, TOC, or projection. `snapshot_open_requested` is written before opening
the repeatable-read owner transaction; `snapshot_open` is legal only with a
live owner heartbeat and a `SnapshotChild` in `running` (or an exactly observed
`exited_success` awaiting sealed readback). `dump_write_requested` requires
the same owner session plus a running child whose PID/start-ticks/boot tuple,
FD-3 password lease, FD-4 dump lease, argv digest, and output path match the
journal. `dump_sealed` requires an exact zero exit, child reaped, fsynced dump
size/hash, and no owner/session loss. `stopped_unknown` is terminal
`snapshot_lost`/`failed_attention`; it is never adopted or rerun under the
same release ID. `snapshot_projection_requested` and `snapshot_sealed` require
the owner backend identity and heartbeat to still match, plus the dump TOC,
owner/default-ACL, `_sqlx_migrations`, registry/binding, and state-projection
hashes from that one exported snapshot. A parent restart first stops and reaps
only the exact child; an unknown PID/backend or mixed child/owner state fails
attention before any target work.

The `target` union is likewise closed: `planned` has a fixed target generation
UUID, volume name, password-generation UUID, vault-generation UUID, and null
secret/file/manifest tuples; `allocated` adds only the exact new volume and
zeroized secret/vault file tuples; `manifested` adds the fsynced manifest.
`target_allocate_requested`, `db_bootstrap_requested`, and
`generation_manifest_install_requested` each precede their one side effect and
must read back those exact tuples. No target registry row or nonce allocation
may exist in `planned`; the registry insertion and nonce allocations are the
single serializable re-encryption transaction described below. Cleanup may
delete only the exact unpublished target tuple after proving no registry,
pointer, binding, allocation, mount, container, backup, or release reference
remains; source evidence is never regenerated or removed.

The snapshot owner transaction remains alive from `snapshot_open` through
`snapshot_sealed`; `pg_dump --snapshot` exact zero exit, dump fsync/hash
readback, exact TOC count/hash, owner/default-ACL projection,
`_sqlx_migrations` digest, and the complete source projection must all bind
that one snapshot. It uses the same owner-preserving `pg_dump` arguments and
TOC validator as Backup. The target uses the same `--no-owner --no-privileges`
single-transaction restore followed by the same fixed peer-superuser
`db-init --normalize-restored-catalog` transaction as Restore; its two added
requested/completed phases cannot be skipped or inferred from catalog shape.
Owner loss before sealing permanently fails this child ID and neither dump nor
projection is regenerated under it. Target UUIDs, volume name, empty secret
paths, and allocation labels are fixed before `target_allocate_requested`.
Every requested phase is durable before its file/volume/process/DB side effect;
every completed phase requires exact tuple, catalog, row-count, and projection
readback. Dump restore accepts only exact empty-bootstrapped or exact all-source
state. Re-encryption uses section 11's sealed all-before/all-after `ReencryptTxn`
matrix; a mixed row/binding/nonce projection is never resumed or repaired in
place. The generation manifest is installed only after the all-after state and
is not eligible for the active pointer until the child target projection is
verified and `committed`.

The initial source is the unpublished transformed v3 generation selected by
the committed authority-transition journal and its exact candidate selector;
the production active pointer must remain absent before and after every source
read. Its DB volume/manifest/binding are nonnull even though pointer authority
is absent. Later releases require `active_generation` and exact pointer bytes.
The two tagged branches are not interchangeable, and a pointer appearing in the
initial branch or disappearing in the active branch is `source_changed` before
snapshot export.

On recovery the parent always reconciles this child first. A nonterminal child
never permits pointer/Hermes/intake work, and a second child ID is never
allocated for the same release. Cleanup may delete only the exact unpublished
target after proving no pointer, container, backup, restore, or other release
references it; source snapshot/dump evidence and the source generation are
untouched. This closes every final-snapshot, volume allocation, restore,
re-encryption, manifest, commit-response, and host-restart boundary at the same
level as Backup/Restore rather than treating `final_snapshot_restored` as an
oracle.

Its exact phases are
`prepared|preflight_verified|hermes_pause_requested|hermes_paused|
intake_withdraw_requested|intake_withdrawn|source_stop_requested|
source_stopped|final_snapshot_started|final_snapshot_restored|
candidate_verified|candidate_preflight_stop_requested|
candidate_preflight_stopped|pointer_swap_requested|
pointer_swapped|hermes_bind_requested|hermes_bound|
candidate_start_requested|candidate_started_drained|intake_open_authorized|
intake_opened|hermes_start_requested|hermes_started|committed|
rollback_started|candidate_stop_requested|candidate_stopped|
hermes_restore_requested|hermes_restored|pointer_restore_requested|
pointer_restored|pointer_absent_restore_requested|pointer_absent_restored|
prior_start_requested|prior_started_drained|
prior_intake_open_authorized|prior_intake_opened|
prior_hermes_start_requested|prior_hermes_started|rolled_back|
rolled_back_no_serve|failed_attention`. Both files are root:root 0600; state
parent is 0700. Every `*_requested` phase is durable before its named external
side effect; the following completed phase is written only after exact
readback.

Pre-CAS candidate authority is explicit and cannot masquerade as production.
The root release process writes a JCS selector containing exactly
`{version:1,mode:"candidate",release_id,
manifest_path:"/run/nblb-selector/manifest.json",manifest_sha256,
listen:"127.0.0.1:12456",public_intake:false}` and mounts it read-only at
`/run/nblb-selector/candidate.json` together with the journal-bound candidate
manifest, candidate DB volume/password, and candidate vault key. The image is
invoked with exact `gateway --candidate-selector
/run/nblb-selector/candidate.json`; candidate mode refuses production port,
public authority, and active-generation file. Production is invoked without
that flag, refuses any candidate selector mount, reads only `runtime.env`'s
active pointer, and binds 2456. After pointer swap the 12456 container is
stopped/removed and a fresh production-mode container is started. There is no
environment/argv secret or one-use credential. The root verifier uses ordinary
admin bearer auth and frozen in-process fake providers.

A preliminary scratch candidate may run while old intake is open, but its DB is
never activation state. Under the operations then Hermes locks, release first
durably records `hermes_pause_requested`, stops/adopts the exact stopped Hermes
container when an active binding exists, verifies no Hermes task process, and
records `hermes_paused`. With a null binding, the hold-stopped container must
remain stopped and the same two phases are verified no-ops. For a normal
`state_forward_source`, release uses section 8's root-peer Unix control command
to durably withdraw public work and owner mutations, then waits for the exact
admission/DB/provider/cleanup/nonterminal-operation zero predicate and
records `intake_withdrawn`. For the initial `retired_no_serve` bootstrap, the
section 1.1 hold already stopped the legacy app and disabled its restart policy:
no unavailable admin API is called; offline DB/receipt readback under all three
locks proves the same zero counts and the intake phases are explicit verified
no-ops. Release then records `source_stop_requested`, stops/adopts the exact
source app in the normal branch or adopts the exact hold-stopped tuple in the
initial branch, proves its main/control processes and published port absent,
and records `source_stopped`.
Only then does one exported final snapshot capture every key replacement,
revoke/issue, counter, attempt, asset, proof, voice map, event, and Hermes token
row. It restores that snapshot to a new final candidate DB volume, creates a new
PostgreSQL password and vault generation, performs section 11 re-encryption,
and verifies an exact state-projection digest at 12456. Intake remains withdrawn
through this potentially long copy. No old or candidate writer exists between
final snapshot and pointer decision.

All mutating migration/admin/browser/fault cases run on the preliminary scratch
DB. Final-snapshot candidate verification is provider-free and read-only except
transactions that are forced to roll back; the complete state-projection digest
must be byte-identical before and after. It cannot leave test keys, tokens,
counters, operations, events, or asset rows in activation state.
After that proof it records `candidate_preflight_stop_requested`, stops/removes
only the exact 12456 candidate, proves its runtime and port absent, and records
`candidate_preflight_stopped` before any pointer request. The later production
start is a fresh container and cannot inherit candidate-selector mode.

If Hermes `active.json` exists, release reads the live `.env` token into secret
memory, computes its section 9 digest, and proves the candidate DB contains the
recorded credential ID/digest/version active with exact
`models:read,chat:write` scopes. The release journal records prior and candidate
generation IDs plus exact prior and candidate `active.json` bytes and hashes;
the candidate differs only in `bound_generation_id` and the resulting canonical
file tuple. After pointer swap it durably records `hermes_bind_requested`,
CAS-updates the exact prior bytes to candidate bytes, reads them back, and then
records `hermes_bound`. If no Hermes state exists both authorities are explicit
null, the bind phases are verified no-ops, and Hermes stays stopped until the
later initial cutover. Thus a generation cannot activate while dropping the
credential Hermes actually holds.

The production-mode app is then started only after
`candidate_start_requested`; it binds 2456 while the state-forwarded DB's
durable intake drain remains true, and exact image/volume/key/pointer/readiness
readback produces `candidate_started_drained`. `intake_open_authorized` is the
irreversible write-ahead decision; the app clears the drain in one reconciled
transaction and readback produces `intake_opened`. This enables owner mutation
and lets public health reflect the selected management-only or traffic-ready
structure. Only then may `hermes_start_requested` start an existing bound
Hermes, and `hermes_started` requires the selected generation, binding, live
token row, Compose tuple, and health to agree. Null-Hermes phases are no-ops and
do not start the legacy container.

Rollback can begin only before `intake_open_authorized`. It first reconciles
any in-progress pointer/Hermes CAS, stores the exact `rollback_origin_phase`,
stops/adopts the drained candidate under requested/completed phases, restores
candidate Hermes bytes to exact prior bytes when necessary, and only then
requests/restores the generation pointer. If the prior manifest is
`state_forward_source`, it starts that exact app with its unchanged drained DB,
authorizes and verifies prior intake, then starts/verifies the prior bound
Hermes before `rolled_back`. If the prior manifest is `retired_no_serve`, as in
the initial revoked-legacy transition, pointer restoration is an evidence
rollback only: no prior app or Hermes is started, intake remains closed, and
the terminal is `rolled_back_no_serve`. This is a service-attention state, not
permission to reuse revoked credentials.

Every journal/pointer write uses same-directory exclusive temp, file fsync,
rename, parent fsync, and byte readback. Pointer rename is preceded by durable
`pointer_swap_requested`; a nonnull-prior restore rename by
`pointer_restore_requested`. Initial absent restoration never treats unlink as
an atomic CAS. Its journal prebinds the absent target and unique path
`/opt/nvidia-build-lb/state/active-generation.rollback.<release-uuid>.tombstone`
whose expected bytes/hash/uid/gid/mode/device/inode/nlink are the exact candidate
pointer. After `pointer_absent_restore_requested`, root calls safe
`renameat2(RENAME_NOREPLACE)` from the exact active pointer to that absent
tombstone name, fsyncs the state parent, and records
`pointer_absent_restored` only after active path absence plus exact tombstone
readback. The tombstone remains immutable evidence and is not automatically
unlinked. If rollback began before pointer swap, the same requested/completed
phases are an exact verified no-op requiring both active pointer and tombstone
absent. No other phase may create that tombstone.
Recovery reads both authorities and follows this complete matrix:

| Journal phase | Active pointer allowed | Recovery |
| --- | --- | --- |
| before `pointer_swap_requested` | exact prior, or absent for initial | continue or roll back candidate-only state |
| `pointer_swap_requested` | prior, or absent for initial | perform candidate CAS |
| `pointer_swap_requested` | candidate | adopt completed rename, record `pointer_swapped` |
| `pointer_swapped` through `candidate_started_drained` | candidate | continue candidate or begin pre-intake rollback |
| `rollback_started` through `hermes_restored`, with rollback origin before `pointer_swapped` | prior | candidate-only cleanup; pointer CAS is a no-op |
| `rollback_started` through `hermes_restored`, with rollback origin at/after `pointer_swapped` | candidate | restore/verify Hermes, then request pointer restore |
| `pointer_restore_requested`, post-swap nonnull-prior origin | candidate | perform candidate-to-prior rename CAS |
| `pointer_restore_requested`, nonnull-prior origin | prior | adopt exact prior and record `pointer_restored` |
| `pointer_absent_restore_requested`, pre-swap initial origin | active absent and tombstone absent | verify no-op and record `pointer_absent_restored` |
| `pointer_absent_restore_requested`, post-swap initial origin | active candidate and tombstone absent | rename candidate to exact tombstone, fsync parent |
| `pointer_absent_restore_requested`, post-swap initial origin | active absent and exact tombstone present | adopt completed rename and record `pointer_absent_restored` |
| `pointer_restored` through `prior_hermes_started`, or `rolled_back` | exact nonnull prior | finish the state-forward prior branch |
| `pointer_absent_restored` through `rolled_back_no_serve` | active absent; tombstone absent for pre-swap or exact for post-swap | start neither prior app nor Hermes; finish initial no-serve branch |
| `intake_open_authorized` through `committed` | candidate | roll forward candidate only; stale-DB rollback forbidden |

Let `H0` be the exact prior Hermes `active.json` bytes and `H1` the exact
candidate bytes; when no state exists both are the same explicit null. Hermes
recovery is independently closed:

| Journal phase | Hermes authority allowed | Recovery |
| --- | --- | --- |
| before `hermes_bind_requested` | `H0` only | continue |
| `hermes_bind_requested` | `H0` | perform `H0 -> H1` CAS |
| `hermes_bind_requested` | `H1` | adopt completed CAS and record `hermes_bound` |
| `hermes_bound` through `candidate_started_drained` | `H1` only | continue or begin rollback |
| rollback before `hermes_restore_requested` | exact `H0` or `H1` named by reconciled `rollback_origin_phase` | continue to restore; no inference from file contents |
| `hermes_restore_requested` | `H1` | perform `H1 -> H0` CAS |
| `hermes_restore_requested` | `H0` | adopt completed restore and record `hermes_restored` |
| `hermes_restored` through rollback terminal | `H0` only | continue prior branch |
| `intake_open_authorized` through `committed` | `H1` only | roll forward candidate |

Absent, malformed, or any other pointer/phase combination becomes
`failed_attention`, starts neither tuple, and never guesses; the same applies
to any Hermes value other than the table's exact bytes. At
`candidate_start_requested`, runtime is either stopped or the exact candidate
running with intake drained; the latter is adopted. At
`candidate_started_drained` it must be that exact running tuple. The symmetric
rules apply to `candidate_stop_requested`, `prior_start_requested`, and their
completed phases; an unrelated container/config/image is never stopped or
adopted. The durable
`intake_open_authorized` transition is the irreversible state boundary and is
written before clearing the candidate's durable drain; after it, recovery never
selects prior DB. Before that boundary, prior DB is safe because no writer has
run since the final snapshot. Candidate start verifies its exact
image/volume/key/pointer and the DB-derived expected management/traffic mode.
Initial v3 with zero fresh keys commits management-only and `/health` 503; a
restored proved pair requires traffic readiness and health 200.

An operator `rollback` after a committed release never activates a historical
DB tuple. It is a new state-forward release: withdraw/drain current intake,
take a final current snapshot, materialize a new DB/vault generation, and use a
prior app image only when its embedded schema-compatibility manifest
lists the exact current schema SHA-256 and its hash is bound by the image/source
manifest. Otherwise rollback is unavailable and a
roll-forward fix is required. Historical generation DB volumes remain evidence/
backup sources, never mutable state authorities. The initial legacy generation
is permanently `retired_no_serve` and never selectable to serve, even when its
evidence pointer is restored by a failed initial transition.

## 11. Backup, restore, rollback, and custody

Local roots are:

- DB dump: `/var/backups/nvidia-build-lb/db/<backup-id>/database.dump`;
- public manifest: `/var/backups/nvidia-build-lb/manifests/<backup-id>/manifest.json`;
- separately encrypted key:
  `/var/lib/nvidia-build-lb/key-custody/<backup-id>/vault.key.age`; and
- partial journal: `/var/lib/nvidia-build-lb/backup-state/<backup-id>.json`.

That partial journal is the closed `BackupJournal`, JCS root:root 0600:

```text
BackupJournal={
 version:1,transaction_id:UUID,backup_id:UUID,phase:BackupPhase,
 source:SourceTxn,offhost_config:PathFileTuple,
 local:{database:LocalObjectTxn,encrypted_vault_key:LocalObjectTxn,
        manifest:LocalObjectTxn},
 destinations:[DestinationTxn,DestinationTxn],failure:BackupFailure|null
}
SourceTxn=
 {phase:"planned",generation_id:UUID,active_pointer_sha256:LowerHex64,
  generation_manifest_sha256:LowerHex64,snapshot_id:null,
  snapshot_owner:null,dump_child:null,state_projection_sha256:null}
|{phase:"snapshot_open",generation_id:UUID,active_pointer_sha256:LowerHex64,
  generation_manifest_sha256:LowerHex64,snapshot_id:String,
  snapshot_owner:SnapshotOwner,dump_child:SnapshotChild,
  state_projection_sha256:null}
|{phase:"sealed",generation_id:UUID,active_pointer_sha256:LowerHex64,
  generation_manifest_sha256:LowerHex64,snapshot_id:String,
  snapshot_owner:SnapshotOwner,dump_child:SnapshotChild,
  state_projection_sha256:LowerHex64}
SnapshotOwner={boot_id:UUID,backend_pid:Decimal,start_ticks:Decimal,
               backend_start:Timestamp,session_id:LowerHex32,
               login_role:"nblb_migrator",effective_role:"nblb_owner",
               database:"nvidia_build_lb",isolation:"repeatable_read",
               read_only:true,deferrable:true,transaction_xid:Decimal,
               exported_snapshot:String,heartbeat_at:Timestamp}
SnapshotChild={state:"running"|"exited_success"|"stopped_unknown",
               pid:Decimal,start_ticks:Decimal,boot_id:UUID,
               argv_sha256:LowerHex64,auth_fd:3,output_fd:4,
               started_at:Timestamp,ended_at:Timestamp|null,
               exit_code:Decimal|null}
LocalObjectTxn=
 {final_path:AbsolutePath,partial_path:AbsolutePath,size:null,sha256:null,
  phase:"planned"|"write_requested"}
|{final_path:AbsolutePath,partial_path:AbsolutePath,size:Decimal,
  sha256:LowerHex64,phase:"sealed"|"rename_requested"|"final_verified"}
RemoteObjectTxn=
 {final_path:AbsolutePosixPath,size:null,sha256:null,phase:"planned"}
|{final_path:AbsolutePosixPath,size:Decimal,sha256:LowerHex64,
  phase:"sealed"|"open_exclusive_requested"|"opened_owned"|
        "write_requested"|"fsync_requested"|"fsync_reopen_requested"|
        "fsync_reopened"|"fsync_confirmed"|"final_verified"}
RemoteDirectoryTxn={path:AbsolutePosixPath,
 phase:"planned"|"fsync_requested"|"fsync_confirmed"}
DestinationAuthority={ordinal:1|2,config_sha256:LowerHex64,id:UUID,
 host:DNSName,port:Decimal,user:AsciiUser,root:AbsolutePosixPath,
 host_key_sha256:"SHA256:<base64>",identity:PathFileTuple,
 directory_fsync_extension:"fsync-dir@nvidia-build-lb-v1",
 directory_fsync_helper_sha256:LowerHex64,
 connection:null|{resolved_addresses:[IPAddress],selected:IPAddress}}
DestinationTxn={authority:DestinationAuthority,
 remote_dir:AbsolutePosixPath,root_directory:RemoteDirectoryTxn,
 backup_directory:RemoteDirectoryTxn,owner_marker:RemoteObjectTxn,
 database:RemoteObjectTxn,encrypted_vault_key:RemoteObjectTxn,
 manifest:RemoteObjectTxn,
 phase:"planned"|"directory_verified"|"objects_verified"|
       "manifest_verified"|"receipt_recorded"}
BackupPhase="prepared"|"snapshot_open_requested"|"snapshot_open"|
            "local_objects_requested"|"snapshot_seal_requested"|
            "snapshot_sealed"|"local_objects_verified"|"destination_1_running"|
            "destination_1_verified"|"destination_2_running"|
            "destination_2_verified"|"receipt_requested"|
            "receipt_verified"|"committed"|"failed_attention"
BackupFailure="source_changed"|"local_object_conflict"|
              "remote_object_conflict"|"host_key_changed"|
              "offhost_config_changed"|"identity_file_changed"|
              "snapshot_lost"|"remote_partial_final"|
              "remote_capability_missing"|"upload_failed"|
              "readback_failed"|"tuple_unknown"
```

At `prepared`, snapshot ID/projection, owner identity, dump child tuple, and
every object size/hash are null; values
are never guessed before bytes exist. Each local partial path is the final
basename plus `.partial.<transaction_id>` and is immutable after `prepared`.
`write_requested` is durable before materialization. Only after complete write,
file fsync, streaming size/hash readback, and source-snapshot projection does an
object become `sealed` with nonnull size/hash. Local manifest is sealed last.
`snapshot_seal_requested` precedes the final state-projection read; all three
local objects and projection must be sealed while the same exported snapshot
owner connection remains alive. The owner is one `REPEATABLE READ READ ONLY
DEFERRABLE` session with the exact login/effective roles, DB, exported snapshot,
transaction XID, boot/backend start identity, session ID, and a bounded
heartbeat; a PID alone never proves liveness. `pg_dump --snapshot` must use that
same effective role/DB and already have an exact zero-exit receipt for its
sealed dump (its normal process exit is not snapshot loss). Its child tuple binds
PID/start ticks/boot ID and FD-3/FD-4 leases; parent crash or SSH loss first
stops and reaps that exact child, then accepts only exact exit/readback or marks
`stopped_unknown` and fails attention. `snapshot_sealed` closes that owner only
after exact readback.
If the owner connection/exported snapshot is lost, or pg_dump exits without the
exact success receipt, before this point, the journal becomes terminal
`snapshot_lost`: no bytes may be regenerated under that backup ID. A new backup
command allocates a new backup/transaction ID and fresh snapshot; exact owned
partials from the failed ID remain quarantined or are removed only by a
separately journaled inactive-ID cleanup.

Local publication uses same-directory rename after `rename_requested`, parent
fsync, and exact final readback; manifest is renamed last. Recovery adopts a
final only when its size/hash equal the sealed journal, resumes an exact owned
partial, and never overwrites a different or unowned object. Destination
authority is copied byte-for-byte from the exact config plus each identity file
tuple at `prepared`; any later config, identity inode/hash/metadata, DNS set, or
selected address mismatch stops before connection. Completed destination 1 is
never recopied while destination 2 resumes. Remote publication uses the
exclusive protocol below rather than an assumed rename CAS. This journal and
the final off-host receipt close every local/remote crash boundary.

`manifest.json` is RFC 8785 JCS UTF-8 with no trailing LF and exactly:

```text
{
  "version":1,"backup_id":UUID,"created_at":Timestamp,
  "source_generation":{
    "id":UUID,"manifest_sha256":LowerHex64,
    "active_pointer_sha256":LowerHex64,"app_image":OCIRef,
    "postgres_image":PostgresOCIRef,
    "postgres_base_image":"postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609",
    "vault_key_generation_id":UUID
  },
  "database":{
    "format":"postgresql-custom","postgres_major":17,
    "postgres_version":"17.9","size":Decimal,"sha256":LowerHex64,
    "schema_sha256":LowerHex64,"state_projection_sha256":LowerHex64,
    "vault_binding_sha256":LowerHex64,"toc_count":Decimal,
    "toc_sha256":LowerHex64,"owner_acl_projection_sha256":LowerHex64,
    "sqlx_migrations_sha256":LowerHex64
  },
  "encrypted_vault_key":{
    "format":"age","recipient":AgeRecipient,
    "recipient_sha256":LowerHex64,"size":Decimal,"sha256":LowerHex64
  }
}
```

The DB dump is PostgreSQL custom format. `state_projection_sha256` covers the
canonical row-counted configuration/key/profile/proof/voice-map/token/counter/
attempt/asset/request-terminal/event/evidence/active-and-resolved-Attention/
nonce-allocation/generation-registry projection read inside the same exported
snapshot used by `pg_dump`; `vault_binding_sha256` covers the nonsecret binding
row and the registry state/manifest/retirement tuple. The encrypted
file is the exact vault key generation named by source tuple, encrypted to the
manifest recipient. These IDs/digests make a mismatched dump/key/snapshot fail
before decrypt or restore.

The snapshot owner launches the pinned PostgreSQL 17.9 binary with exact
semantic arguments `pg_dump --format=custom
--snapshot=<journal snapshot ID> --role=nblb_owner
--host=db --port=5432 --username=nblb_migrator --no-password
--dbname=nvidia_build_lb --file=/proc/self/fd/4`. FD 4 is the one inherited
sealed-output memfd: the child is the only writer, and the parent seals it and
read-backs its final bytes before publication. The one-shot helper is
attached only to the exact source data network, where `db` is the verified
source container alias. Authentication uses a sealed anonymous memfd fixed at
FD 3. Its exact 0600 bytes are
`db:5432:nvidia_build_lb:nblb_migrator:<derived password>\n`; the parent writes,
rewinds, applies `F_SEAL_WRITE|GROW|SHRINK|SEAL`, and clears CLOEXEC only for
the child. The child's complete environment allowlist is
`PGPASSFILE=/proc/self/fd/3`, `LC_ALL=C`, and `TZ=UTC`; it contains no password,
URI, or other PG variable. Standard libpq therefore actually consumes the FD
through `PGPASSFILE` instead of merely inheriting an unused descriptor. The
parent verifies the memfd target/mode/seals and child argv/environment through
`/proc`, waits, closes the last FD, and zeroizes the derived password/pgpass
buffer on every path. `nblb_owner`'s explicit ledger SELECT makes the snapshot
complete without granting mutation. Object owners, ACLs, default ACLs, schema
definitions, triggers, ledger rows, and `_sqlx_migrations` are included;
`--no-owner`, `--no-privileges`, data/schema filters, large-object filters, and
parallel jobs are forbidden. A pinned `pg_restore --list` parser accepts only
the canonical semicolon-prefixed header comments emitted by the pinned
PostgreSQL 17.9 binary (normalizes no bytes), ignores those comments for TOC
count/hash, and rejects unknown comments or record types, extension or
external-language objects,
unexpected role names, and any object outside the closed schema contract; its
canonical owner/object/dependency rows produce `toc_count/toc_sha256`.
Independent catalog queries inside the same snapshot produce the exact owner/
ACL/default-ACL and ordered `_sqlx_migrations` projections. The zero-exit/
version/argv receipt and all four digests are validated before sealing.

Off-host custody supports exactly SFTP over OpenSSH. Root configuration
`/etc/nvidia-build-lb/offhost-backup.json` is JCS, root:root 0600, and has
exactly `{"version":1,"destinations":[D1,D2]}` where each destination is:

```text
{
  "id":UUID,"kind":"sftp","host":DNSName,"port":1..65535,
  "user":AsciiUser,"root":AbsolutePosixPath,
  "host_key_sha256":"SHA256:<base64>",
  "identity_file":AbsolutePath,
  "directory_fsync_extension":"fsync-dir@nvidia-build-lb-v1",
  "directory_fsync_helper_sha256":LowerHex64
}
```

The two entries have distinct IDs, hosts, host-key fingerprints, and roots.
Host is a lowercase non-IP DNS name, user matches
`[A-Za-z_][A-Za-z0-9_-]{0,31}`, root is normalized absolute UTF-8 with no
empty/dot/dot-dot component, and identity path is exactly
`/etc/nvidia-build-lb/offhost/<destination-id>.key`.
`DNSName` is 1..253 ASCII bytes of lowercase LDH labels, each 1..63 bytes,
without a trailing dot or an all-numeric final label. `AbsolutePosixPath` and
`AbsolutePath` are the same type here: 1..4,096 UTF-8 bytes, begin with `/`,
contain no NUL/control/backslash, repeated slash, trailing slash (except `/`),
or empty/dot/dot-dot component. `AsciiUser` is the stated regex.
`IPAddress` is either canonical dotted-decimal IPv4 with four 0..255 components
and no leading zero except literal `0`, or RFC 5952 lowercase compressed IPv6
with no zone ID or IPv4-embedded alternate spelling. Resolver arrays are unique
and sorted by 16-byte IPv4-mapped network address; the selected value must be a
member.
`host_key_sha256` is exactly `SHA256:` plus 43 unpadded RFC 4648 base64
characters that decode canonically to 32 bytes; padding, whitespace, alternate
alphabet, or a noncanonical encoding fails. JSON integers are lexical decimal
without exponent or leading zero. These scalar rules are emitted into both
Rust and JSON Schema and are not delegated to OpenSSH parsing.
Identity files are root:root 0600 regular/nlink-one, never agent/password/env,
and are not part of backup. DNS is resolved then pinned for one connection;
OpenSSH host-key comparison is exact. Before each destination starts, the helper
revalidates the complete config-file tuple and every literal destination field,
identity path/bytes/uid/gid/mode/device/inode/nlink, host-key fingerprint,
resolver answer set, and selected IP against `DestinationAuthority`; redirects,
SSH config, ProxyCommand, agent, known-host fallback, and hostname canonicalization
are disabled. The server must support SFTP v3 `SSH_FXF_CREAT|SSH_FXF_EXCL` and
the OpenSSH `fsync@openssh.com` extension. Because that extension does not make
new directory entries durable, it must also advertise the exact project
extension `fsync-dir@nvidia-build-lb-v1` with the configured helper SHA-256.
That restricted server helper accepts one canonical path already under the
configured root, opens every component no-follow, calls `fsync` on the final
directory FD, and returns only success or a fixed code. No shell/argv command is
available. Ordinary OpenSSH SFTP without this reviewed extension fails the
external go-live gate rather than receiving a durability claim.

For each destination the Rust SFTP helper first issues exclusive `MKDIR` for
`<root>/<backup-id>`, then durably requests and confirms extension fsync of
`<root>` before adopting the new name. If it already exists, recovery may adopt it only when the
regular file `.nblb-owner` is the exact previously journaled object. That owner
marker is itself created with `OPEN WRITE|CREAT|EXCL`, contains JCS
`{version:1,backup_id,transaction_id,destination_id,authority_sha256}`, is
remote-fsynced, closed, downloaded, and hash-verified. A directory left without
the exact marker is an ID collision and is never removed or claimed. The marker
is not `final_verified` until its file fsync and a subsequent confirmed
extension fsync of `<root>/<backup-id>` both succeed.

Inside an owned directory each final name—`database.dump`, `vault.key.age`, and
last `manifest.json`—is opened directly with
`SSH_FXF_WRITE|SSH_FXF_CREAT|SSH_FXF_EXCL`. The requested phase is local-durable
before OPEN. The helper streams the sealed bytes, calls remote fsync on that
open handle, closes it, then downloads the final path to a streaming size/hash
verifier before `final_verified`. It never calls standard or POSIX rename and
never overwrites a target. On recovery, an existing final path is adopted only
when its complete size/hash equals the sealed journal and the owner marker is
exact, and that adoption proves byte ownership only; it still must reach the
closed `fsync_confirmed` path below. A short/different file is `remote_partial_final`; that backup ID is
permanently unusable and neither the file nor directory is deleted/reused. Since
consumers recognize a backup only after a complete, hash-valid manifest whose
referenced objects also verify, direct exclusive data-object writes are safe
without pretending SFTP has no-clobber rename. This protocol also closes two
concurrent helpers: only one can create the directory/marker/final object.

After each data/manifest file reaches object `fsync_confirmed`, the helper
durably sets `backup_directory.fsync_requested`, calls the extension for the
owned backup directory, and requires explicit success before that object's
`final_verified`. The manifest is last, so a verified remote backup proves its
directory entry and all referenced prior entries. A directory-fsync response
loss repeats only the idempotent directory fsync after revalidating authority,
owner marker, and object hashes; it never rewrites an object. Root-directory
fsync has the same repeatable requested/confirmed rule after MKDIR. Capability,
helper digest, file-fsync, and directory-fsync fault tests are mandatory on
both concrete off-host servers before go-live.

An fsync request without a durably recorded successful response is never
treated as durable merely because a download hash matches. Recovery from
`fsync_requested` first records `fsync_reopen_requested`, reconnects using the
same frozen `DestinationAuthority`, and opens the exact existing final with
`SSH_FXF_WRITE` only—no CREATE, TRUNCATE, APPEND, or write call. It fstats and
requires the sealed size before `fsync_reopened`, invokes
`fsync@openssh.com` on that reopened handle, requires an explicit success
response, closes it, and only then records `fsync_confirmed`. The normal
uninterrupted path records the original handle's explicit success directly as
`fsync_confirmed`. A channel/response loss on the reopened fsync repeats the
same reopen-and-fsync sequence; it never rewrites bytes. Only
`fsync_confirmed` may proceed to full download/hash verification and
`final_verified`. This rule applies independently to the owner marker and all
three final objects at both destinations.

If a crash follows destination 1's durably confirmed complete manifest fsync
but precedes its local receipt, recovery downloads and rehashes all three finals, adopts
`manifest_verified`, and writes the missing receipt. It never recopies the
destination. The root receipt
`/var/lib/nvidia-build-lb/backup-state/<backup-id>-offhost.json` is JCS 0600 and
contains exactly:

```text
OffhostReceipt = {
  version:1,backup_id:UUID,manifest_sha256:LowerHex64,
  destinations:[DestinationReceipt,DestinationReceipt]
}
DestinationReceipt = {
  id:UUID,authority_sha256:LowerHex64,config_sha256:LowerHex64,
  identity_file_sha256:LowerHex64,host_key_sha256:"SHA256:<base64>",
  durability:{extension:"fsync-dir@nvidia-build-lb-v1",
    helper_sha256:LowerHex64,capability_receipt_sha256:LowerHex64,
    owner_marker_file_fsync_sha256:LowerHex64,
    database_file_fsync_sha256:LowerHex64,
    encrypted_vault_key_file_fsync_sha256:LowerHex64,
    manifest_file_fsync_sha256:LowerHex64,
    root_directory_fsync_sha256:LowerHex64,
    backup_directory_fsync_sha256:LowerHex64},
  database:{size:Decimal,sha256:LowerHex64},
  encrypted_vault_key:{size:Decimal,sha256:LowerHex64},
  manifest:{size:Decimal,sha256:LowerHex64},
  uploaded_at:Timestamp,readback_at:Timestamp,status:"verified"
}
```

Rows are destination-config order and there are exactly two; it contains no
remote credential or plaintext. Release requires both verified rows for the
same manifest hash. Each durability hash is the canonical secret-free receipt
of the explicit successful response plus destination/backup/object IDs and the
bound helper/version; a download hash without these nonnull receipts cannot
construct `DestinationReceipt`. The backup-directory receipt is captured after
the manifest's final file fsync and therefore covers all four owned entries;
the root-directory receipt covers adoption of the backup directory name.

Loss of the original host/local receipt is a supported disaster path, not a
dead backup. `nblb-ops import-offhost-backup <backup-id>` first creates
`/var/lib/nvidia-build-lb/offhost-import-state/<transaction-id>.json` and an
exclusive empty local import directory. Its closed journal is:

```text
OffhostImportJournal={
 version:1,transaction_id:UUID,backup_id:UUID,phase:OffhostImportPhase,
 offhost_config:PathFileTuple,
 destinations:[{authority:DestinationAuthority,
   owner_marker_sha256:LowerHex64,database:RemoteObjectTxn,
   encrypted_vault_key:RemoteObjectTxn,manifest:RemoteObjectTxn},
  {authority:DestinationAuthority,
   owner_marker_sha256:LowerHex64,database:RemoteObjectTxn,
   encrypted_vault_key:RemoteObjectTxn,manifest:RemoteObjectTxn}],
 local:{database:LocalObjectTxn,encrypted_vault_key:LocalObjectTxn,
        manifest:LocalObjectTxn},
 receipt:PathFileTuple|null,
 failure:"authority_mismatch"|"owner_mismatch"|"remote_mismatch"|
         "manifest_invalid"|"download_failed"|"local_conflict"|
         "directory_fsync_failed"|"tuple_unknown"|null
}
OffhostImportPhase="prepared"|"destination_1_verify_requested"|
 "destination_1_verified"|"destination_2_verify_requested"|
 "destination_2_verified"|"cross_copy_verified"|
 "local_materialize_requested"|"local_materialized"|
 "receipt_requested"|"receipt_verified"|"committed"|"failed_attention"
DisasterImportReceipt={
 version:1,kind:"disaster_import",transaction_id:UUID,backup_id:UUID,
 manifest_sha256:LowerHex64,
 destinations:[DestinationReceipt,DestinationReceipt],
 local:{database:PathFileTuple,encrypted_vault_key:PathFileTuple,
        manifest:PathFileTuple},verified_at:Timestamp
}
```

The operator restores the two destination configs/identity files from separate
custody; their IDs, roots, host keys, directory-fsync helper digests, and owner
markers must match the remote backup. The importer reads and file+directory-
fsync-verifies all objects independently on both servers, requires byte-equal
manifest hashes and manifest-referenced database/encrypted-key hashes/sizes,
and requires the two owner markers to share backup/transaction IDs while naming
their distinct destination IDs. It downloads from destination 1 to exclusive
local partials, fsyncs/renames/parent-fsyncs them, and byte-compares against a
streaming destination-2 readback before publication. A response loss repeats
only safe GET/fsync/readback, never remote write. The reconstructed receipt is
new local authority explicitly marked `disaster_import`; it does not claim the
lost original receipt hash. Restore accepts it only through the tagged union
below. One surviving copy, mismatched copies, new host keys/roots, or an
unverified directory entry are insufficient.

For a disaster import, each reconstructed `DestinationReceipt.durability`
names the currently verified helper digest/capability and new idempotent reopen-
and-fsync receipts for the owner marker, every data object, and both
directories; it never copies a hash from a missing original host receipt. A
helper/version change, missing historical owner marker, or inability to produce
the new root/backup-directory response hashes is
`directory_fsync_failed` and cannot reach `receipt_verified`. The committed
import receipt hash is then the exact `disaster_import` source consumed by the
shared restore validator below.

All parents are root:root 0700; files are 0600 and generation directories 0700.
One exported snapshot binds the dump and state projection. The dump, encrypted
key, and manifest publish to same-directory temporary files with fsync; the
manifest is renamed last. The age recipient is the public value in
`/etc/nvidia-build-lb/backup.age-recipient`; the private identity is never on
this host. The file contains one canonical X25519 recipient matching
`age1[023456789acdefghjklmnpqrstuvwxyz]{58}` plus LF and no other bytes.
Dump/manifest and encrypted key must reach two separate configured
off-host custody destinations and be read-back verified before go-live. Because
their concrete hosts/IDs/keys are not yet supplied, a valid closed configuration
and two successful receipts are an explicit external go-live gate, not
permission to skip backup. Encryption uses the pinned Rust `age`/X25519
implementation `age =0.11.1` from the committed Cargo.lock inside a local
`nblb-age` helper; no `age` CLI, shell, dynamic library, or network process is
called. The source/lockfile digest, recipient bytes, age file-header digest,
and helper image digest are bound in the manifest. The helper accepts the vault
key only through a sealed 0600 FD-3 memfd, emits only the encrypted output to
the sealed FD-4 object, writes no stderr/log/evidence, zeroizes its buffers, and
rejects any recipient/header/size/hash mismatch. Restore uses the same pinned
helper and manifest-bound algorithm before custody validation.

Every restore is owned by
`/var/lib/nvidia-build-lb/restore-state/<transaction-id>.json`, a JCS root:root
0600 `RestoreJournal` written before allocating a volume or secret:

```text
RestoreJournal={
 version:1,transaction_id:UUID,phase:RestorePhase,
 source:{backup_id:UUID,manifest:PathFileTuple,database:PathFileTuple,
         encrypted_vault_key:PathFileTuple,
         custody_receipt:{kind:"original",sha256:LowerHex64}|
                         {kind:"disaster_import",sha256:LowerHex64,
                          transaction_id:UUID},
         source_validation_sha256:LowerHex64,
         source_generation_id:UUID,source_projection_sha256:LowerHex64,
         source_binding_sha256:LowerHex64},
 target:RestoreTarget,
 reencrypt:ReencryptTxn,
 release_id:UUID|null,failure:RestoreFailure|null
}
RestoreTarget=
 {state:"planned",generation_id:UUID,db_volume_name:String,db_volume_id:null,
  postgres_secret_generation_id:UUID,postgres_secret:null,
  vault_key_generation_id:UUID,vault_key:null,generation_manifest:null}
|{state:"allocated",generation_id:UUID,db_volume_name:String,
  db_volume_id:LowerHex64,postgres_secret_generation_id:UUID,
  postgres_secret:PathFileTuple,vault_key_generation_id:UUID,
  vault_key:PathFileTuple,generation_manifest:null}
|{state:"manifested",generation_id:UUID,db_volume_name:String,
  db_volume_id:LowerHex64,postgres_secret_generation_id:UUID,
  postgres_secret:PathFileTuple,vault_key_generation_id:UUID,
  vault_key:PathFileTuple,generation_manifest:PathFileTuple}
ReencryptTxn=
 {state:"planned",row_count:null,plan_attempt:0,
  before_projection_sha256:null,target_projection_sha256:null,
  target_registry:{state:"planned",id:UUID,manifest_digest:LowerHex64}}
|{state:"before_verified",row_count:Decimal,plan_attempt:Decimal,
  before_projection_sha256:LowerHex64,target_projection_sha256:null,
  target_registry:{state:"planned",id:UUID,manifest_digest:LowerHex64}}
|{state:"plan_sealed"|"commit_requested"|"committed",row_count:Decimal,
  plan_attempt:Decimal,before_projection_sha256:LowerHex64,
  target_projection_sha256:LowerHex64,
  target_registry:{state:"planned"|"inserted",id:UUID,
                   manifest_digest:LowerHex64,
                   registry_projection_sha256:LowerHex64|null}}
RestorePhase="prepared"|"source_verify_requested"|"source_verified"|
 "target_allocate_requested"|"target_allocated"|"db_bootstrap_requested"|
 "db_bootstrapped"|"dump_restore_requested"|"dump_restored"|
 "catalog_normalize_requested"|"catalog_normalized"|
 "reencrypt_plan_requested"|"reencrypt_plan_sealed"|
 "reencrypt_commit_requested"|"reencrypted"|"candidate_verify_requested"|
 "candidate_verified"|"release_requested"|"release_committed"|"committed"|
 "cleanup_requested"|"cleaned"|"failed_attention"
RestoreFailure="source_mismatch"|"offhost_receipt_mismatch"|
 "disaster_import_invalid"|
 "vault_key_invalid"|"target_tuple_changed"|"bootstrap_failed"|
 "dump_restore_failed"|"catalog_normalize_failed"|
 "reencrypt_state_mixed"|"nonce_allocation_exhausted"|
 "candidate_verification_failed"|"release_failed"|"cleanup_failed"|
 "tuple_unknown"
```

Every requested phase is durable before its external file/volume/process/DB/
release side effect; completed phases require exact tuple readback. Target UUIDs,
Docker volume labels/ID, secret paths, and empty-file metadata are fixed in
`target_allocate_requested`; secret file hashes enter the root-only target tuple
only after exclusive creation/readback and never enter external evidence. Dump
restore accepts only an empty bootstrapped target or the exact full restored
source projection. A response loss is reconciled by schema/table/count/digest,
not by rerunning into a partially restored database.

Source validation is two composed validators, not one impossible union.
`CustodySourceValidator` is used only by Restore and has two branches.
`original` requires
the local committed `BackupJournal`, exact `OffhostReceipt`, two verified
`DestinationReceipt` rows including capability/helper/file-fsync/directory-
fsync hashes, and local object/manifest tuples to agree. Its failure maps to
`offhost_receipt_mismatch` or `source_mismatch`. `disaster_import` additionally
requires the named `OffhostImportJournal` to be `committed`, its terminal
journal hash and transaction UUID to equal the `DisasterImportReceipt`, both
remote rows and the three reconstructed local tuples to rehash, and no original
receipt claim. Its branch-specific failure is `disaster_import_invalid`; common
object/manifest mismatch remains `source_mismatch`. It returns a sealed local
dump/key/manifest tuple plus a custody-projection hash.

`SealedDumpValidator` then has exactly three tagged inputs:
`custody_original`, `custody_disaster_import`, and `live_snapshot`. The first
two require the corresponding successful custody result above. `live_snapshot`
requires the active/initial source union, exported snapshot ID/owner, dump
tuple, TOC count/hash, owner/ACL, `_sqlx_migrations`, schema, binding, and state-
projection digests already sealed in `ReleaseMaterializationJournal`; it has no
custody receipt. All three run the identical archive bytes/TOC/catalog/
migration/projection checks and return one common `ValidatedDump` type. Restore
hashes the custody result plus `ValidatedDump` into
`source_validation_sha256` before `source_verify_requested`. State-forward
release/rollback uses `live_snapshot` directly and stores its result in the
sealed child snapshot fields, never fabricating a BackupJournal. Thus the same
dump validator cannot reinterpret bytes, while off-host custody remains a
separate Restore-only requirement.

`db-init --restore-target` is a distinct journaled mode: it creates/validates
the fixed cluster, database, four roles, SCRAM bindings, HBA/config, and an empty
database, but it does not create schema `nblb`, default privileges, application
tables, triggers, or `_sqlx_migrations`. Before restore, the custody validator
and then sealed-dump validator rehash the manifest, receipt, dump, exact
TOC count/hash, owner/default-ACL projection, and `_sqlx_migrations` digest.
The restore then runs exact semantic arguments
`pg_restore --single-transaction --exit-on-error --no-owner --no-privileges
--role=nblb_owner --host=db --port=5432 --username=nblb_migrator
--no-password --dbname=nvidia_build_lb` with its stdin duped from the same
sealed dump FD 4 (never a shell path). Its isolated target
network exposes only the verified unpublished target as alias `db`; it uses the
same FD-3 sealed 0600 memfd/`PGPASSFILE=/proc/self/fd/3` bytes, environment
allowlist, `/proc` proof, close, and zeroization contract as `pg_dump`.
The restore journal also binds `RestoreChild={pid,start_ticks,boot_id,
target_backend_pid,target_backend_start,auth_fd:3,dump_fd:4,state:"running"|
"exited_success"|"stopped_unknown",argv_sha256}`. A parent restart stops/reaps
that exact child and accepts only the empty target or exact all-source catalog;
an unknown child/target backend is `dump_restore_failed` and never gets adopted
or rerun into place.
`--clean`, `--create`,
filters, parallelism, post-data skipping, and any other owner/ACL choice are
forbidden. This first transaction restores schema, data, constraints, triggers,
ledger rows, and migration history into an unpublished catalog; fixed owner/ACL
normalization is deliberately separate because `--no-owner` cannot recreate
the distinct ledger owner.

`catalog_normalize_requested` then invokes only
`db-init --normalize-restored-catalog` while the target remains Unix-only. Its
UID-70 peer connection is PostgreSQL role `postgres`; no SQL/role/object name is
accepted from argv, environment, dump, or operator input. One fixed transaction
requires the already validated TOC/schema/migration/state digests, changes every
ordinary object to `nblb_owner` and only the nonce ledger plus its immutable
trigger tables, trigger functions, and sequences to `nblb_ledger_owner`,
installs the two exact default-ACL sets, function ACLs, and object ACLs,
re-enables the exact triggers, and reasserts the four-role
attributes/membership graph. It changes no table row. `catalog_normalized`
requires reconnect readback equal to the source's fixed owner/ACL/function
projection and exact TOC object count, `_sqlx_migrations` versions/checksums,
append-only ledger triggers/functions, schema hash, and state projection. A killed/lost pg_restore is
either empty or exact all-source data with pre-normalized fixed ownership; a
killed normalization is exact before or after because it is one transaction.
Any other schema/object/role/ACL/migration/projection state is mixed and the
volume is never reused.

Before re-encryption the exact restored binding/envelope/nonce-ledger and
generation-registry projection is `before_projection_sha256`. The target
generation UUID and its 32-byte manifest digest are sealed in `target_registry`
before the commit transaction. Because the restored dump initially contains
the source generation as the one active registry row, the serializable commit
transaction takes the binding and source-generation locks, sets the singleton
binding to its explicit all-null branch, retires the source generation with its
`retired_at`, and only then inserts exactly one
`vault_key_generations(state='active',id,manifest_digest)` target row and reads
it back. It reserves each new-generation nonce through section 7's permanent
ledger only after that insert, then locks/revalidates and decrypts all rows and
prepares every target envelope in zeroizing memory, and
persists only its row count and complete target projection digest as
`plan_sealed` before requesting commit. The one database transaction updates
all envelopes, allocation rows, and `vault_key_binding` atomically. The
intermediate unbound/retired state is visible to no other transaction and the
deferred registry/binding trigger requires the target active row and target
binding before commit; any failure rolls back the source retirement as well.
On commit
ambiguity, a new connection accepts exactly one of:

- **all-before**: the source generation is still active and bound with the
  exact source binding/envelope/ledger projection, the target registry ID is
  absent exactly as recorded before the transaction, and there are zero
  target-generation allocation/envelope rows. The lost in-memory plan is never
  reconstructed.
  After a newly supplied source-key FD verifies again, the journal increments
  `plan_attempt`, creates a fresh nonce plan/digest, and may request a new
  transaction; or the operator cleans the unpublished target.
- **all-after**: the source generation is retired with its exact retirement
  timestamp/projection, exactly one target registry row with the sealed ID,
  active state, manifest digest, and registry projection digest exists; target
  binding, every ciphertext-bearing row's target generation/envelope, exactly
  `row_count` distinct committed target nonce allocations, decrypt-all/
  fingerprint checks, and the complete projection digest exactly match the
  sealed target. Recovery records `reencrypted` and never repeats the
  transaction.

Any mixed binding, partial target generation, missing/extra allocation, or
different digest is `reencrypt_state_mixed`, withdraws the candidate, and never
guesses forward. The same matrix is checked after process/host restart, so a
phase name alone is not authority. Cleanup may remove only the exact target
volume and target secret generation after every active/candidate pointer,
release journal, mount, container, and backup reference proves it unpublished;
`cleanup_requested` precedes deletion and source files/backups are untouched.

Restore always creates a new DB volume and vault-key generation and then uses
the exact canonical handoff above: it opens the source backup key from the
inherited FD, verifies the source binding, and decrypts every ciphertext row
against its original key-generation UUID/AAD; it creates the new UUID,
independent 32-byte key, and target manifest in the unpublished candidate;
inside the journaled serializable transaction it locks binding -> source
generation -> nonce ledger, sets the binding null, retires the source registry
row, inserts/read-verifies the target active registry row, reserves every fresh
12-byte nonce, re-encrypts each row with section 9 AAD, preserves row
UUID/fingerprint, and binds the target with a fresh salt/verifier. It
read-decrypts and fingerprint-verifies all rows with the new key before commit;
commit ambiguity uses the journal's all-before/all-after matrix (including
source/target registry and nonce projections) and never repeats an unknown
transaction. The source and new
plaintext key buffers are independently zeroized. Any failure destroys only
the unpublished new key/candidate DB through the journaled cleanup phases after
proving they are inactive and leaves
the backup and source generation untouched. Restore then verifies manifest,
schema, token digests, slots, counters, asset cleanup, and a scoped synthetic
request at 12456, and activates the full tuple through the same CAS.
Committed rollback follows section 10.3's state-forward materialization, never
selects a historical DB volume, never runs SQL downgrade, and never selects
legacy. Restore accepts plaintext key material only through
`--vault-key-fd <n>`: an off-host custody workstation decrypts and streams over
an authenticated encrypted operator channel into an anonymous inherited FD.
The private age identity, plaintext path, argv, and environment never exist on
the server. The root process requires exactly 32 bytes followed by EOF, keeps
them in a zeroizing buffer, creates the new generation key through exclusive
temp/fsync/rename/readback, closes the FD, and zeroizes on every success/failure
path. Manifest digest, vault binding, decrypt-all, and off-host receipt IDs are
verified before the new generation is eligible for pointer activation; failure removes only the
unpublished temp/new candidate and retains source backups.

Generations and backups are never automatically deleted. Active, the two most
recent verified prior state-forward source generations, every generation younger than 30
days, any generation referenced by a journal, and every legacy artifact before
provider revoke are ineligible for deletion. `nblb-ops retire-generation <id>`
is the only retirement/tombstone path and requires inactive/nonrollback status,
zero pins/assets/operations, provider revoke for contained upstreams,
two-destination off-host readback, typed confirmation of the UUID, and a
retirement receipt. It updates the registry to `retired` but never deletes the
registry row or its nonce allocations.
Capacity pressure raises attention only. `uninstall` stops/removes app
containers, networks, tunnel ingress, and system integration while preserving
all DB volumes, manifests, vault keys, backups, receipts, and Hermes backups.
There is no `purge` command. Provider-revoke evidence remains mandatory before
any legacy artifact deletion.

## 12. Svelte owner console and design system

The live loopback `codex-lb` console is a behavioral reference, never a source
dependency. Before UI implementation, Codex captures its secret-free 320 px and
1280 px screenshots plus sanitized DOM/navigation/state inventory from the
unchanged service on port 2455 and hashes the receipts. The audit may carry
forward its useful operator density, state-first hierarchy, stable navigation,
and evidence disclosure patterns; it copies no HTML/CSS/JS, logo, asset,
token, or wording and makes no request that changes `codex-lb`. The NVIDIA
graphite/green tokens and the decision axis below remain the v3 visual
authority. Final before/after health and container identity prove that using
the reference did not modify the service.

`apps/admin` uses SvelteKit 2, Svelte 5 runes, TypeScript, adapter-static, and
build-time prerender for `/admin` and `/showcase`. Rust embeds production output;
there is no JS server. SvelteKit bootstrap inline scripts are permitted only by
their exact generated SHA-256 CSP hashes. Build scans every HTML script block,
rejects inline event handlers or unmanifested blocks, and emits the exact hashes
into the Rust asset manifest. CSP is:
`default-src 'none'; script-src 'self' <exact hashes>; style-src 'self';
img-src 'self' data:; font-src 'none'; connect-src 'self'; object-src 'none';
base-uri 'none'; frame-ancestors 'none'; form-action 'none'`.
No nonce, eval, external script/font/tracker, or third-party connection exists.

The closed manifest binds path, identity bytes, media type, size, SHA-256, ETag,
and deterministic gzip bytes. Every HTML, compiled JavaScript, CSS, and SVG
entry has both identity and gzip; no other type is gzipped. A path absent from
the manifest is 404. JSON, SSE, audio, image/video payloads, and admin DTOs never
pass compression middleware.

Owner-console visible copy is concise Korean with `<html lang="ko">`; model
IDs, scopes, safe case IDs, HTTP codes, and copied credential labels remain
byte-exact technical strings. UTC DTO timestamps display in `Asia/Seoul` with
an accessible absolute date/time, while Evidence retains the UTC value behind
disclosure. V3 has no locale switch or half-translated duplicate copy. Public
OpenAI-compatible error messages remain the exact English wire strings in
section 5.2 and are not UI copy.

The generated `packages/contracts/src/ko-copy.ts` and Rust fixture share the
closed code-to-copy authority below; punctuation and spacing are exact.
Technical IDs are inserted only in separately labelled values, never
interpolated into these sentences. Each table is a total map over its named
Rust enum. The TypeScript `satisfies Record<Enum,KoCopy>` check, Rust fixture,
and generator cardinality/hash check all fail for a missing, extra, or duplicate
row.

| UI code | Exact visible Korean copy |
| --- | --- |
| `loading` | `현재 상태를 불러오는 중입니다.` |
| `partial` | `일부 상태만 확인되었습니다. 표시된 증거를 확인하세요.` |
| `degraded` | `일부 기능 또는 공개 경로가 저하되었습니다. 안내된 확인을 진행하세요.` |
| `empty_slot1` | `슬롯 1에 첫 키를 등록하세요.` |
| `empty_slot2` | `슬롯 2에 두 번째 키를 등록하세요.` |
| `ready_no_action` | `지금 필요한 작업이 없습니다.` |
| `wait_system` | `관리 작업이 끝날 때까지 기다린 뒤 새로고침하세요.` |
| `offline` | `네트워크가 끊겼습니다. 연결 후 새로고침하세요.` |
| `stale` | `상태가 오래되었습니다. 새로고침 후 계속하세요.` |
| `conflict` | `다른 작업으로 상태가 바뀌었습니다. 새 상태를 확인하세요.` |
| `probe_running` | `키가 지원 기능을 실제로 수행하는지 확인하고 있습니다.` |
| `probe_failed` | `일부 기능 확인에 실패했습니다. 증거를 확인하세요.` |
| `probe_interrupted` | `확인이 중단되었습니다. 정리가 끝난 뒤 다시 시작하세요.` |
| `probe_cancelled` | `기능 확인을 취소했습니다.` |
| `reset_cancelled` | `슬롯 정리 전에 초기화를 취소했습니다. 현재 상태를 다시 확인하세요.` |
| `reset_interrupted` | `슬롯 정리 전에 초기화가 중단되었습니다. 현재 상태를 다시 확인하세요.` |
| `reset_failed_before_commit` | `슬롯 상태는 바뀌지 않았습니다. 증거에서 실패 원인을 확인하세요.` |
| `reset_reopen_required` | `두 슬롯은 비워졌습니다. 관리 화면이 다시 열릴 때까지 기다리세요.` |
| `configuration_changed` | `확인 중 구성이 바뀌었습니다. 새 상태를 불러온 뒤 다시 시작하세요.` |
| `asset_cleanup_required` | `외부 미디어 정리가 끝나야 다음 작업을 할 수 있습니다.` |
| `provider_failure` | `NVIDIA가 기능 확인을 완료하지 못했습니다. 증거를 확인하세요.` |
| `cleanup_wait` | `외부 미디어 정리를 확인하는 중입니다. 표시된 시각에 다시 확인하세요.` |
| `invalid_staged` | `이 후보 키는 인증되지 않았습니다. 후보를 정리하고 새 키를 등록하세요.` |
| `invalid_assigned_replace` | `배정된 키가 인증되지 않았습니다. 해당 슬롯을 교체하세요.` |
| `invalid_pair_reset` | `안전하게 유지할 키가 없습니다. 두 슬롯을 초기화하고 다시 등록하세요.` |
| `cooldown_wait` | `일시 제한이 끝날 때까지 기다리세요.` |
| `issue_first_client` | `API를 사용할 첫 접속 키를 발급하세요.` |
| `secret_present` | `이 값은 다시 표시되지 않습니다. 안전한 곳에 옮긴 뒤 화면과 클립보드를 비우세요.` |
| `clipboard_manual` | `정리 문구를 복사해 아래 칸에 붙여넣으세요.` |
| `session_expired` | `관리 세션이 끝났습니다. 비밀값 정리를 마친 뒤 다시 로그인하세요.` |
| `operation_unknown` | `응답을 받지 못했습니다. 같은 작업을 다시 보내지 말고 결과를 확인하세요.` |
| `evidence_open` | `실패 원인과 다음 확인 시각을 증거에서 확인하세요.` |
| `action_wait_cleanup` | `정리 확인 기다리기` |
| `action_delete_staged` | `후보 키 삭제` |
| `action_quarantine_staged` | `후보를 격리하고 새로 시작` |
| `action_replace_slot` | `이 슬롯 교체` |
| `action_reset_pair` | `두 슬롯 초기화` |
| `action_retry_probe` | `새 확인 시작` |
| `action_refresh_retry` | `새로고침 후 다시 시작` |
| `action_wait_reset_recovery` | `관리 화면 복구 기다리기` |
| `action_inspect_evidence` | `증거 열기` |
| `external_account_action` | `NVIDIA 계정에서 권한 또는 크레딧 조치를 마친 뒤 이 모델을 다시 확인할 수 있습니다.` |
| `action_external_fix_probe` | `외부 조치 후 새 확인 시작` |
| `action_enable_slot` | `슬롯 다시 사용` |
| `action_wait_expiry` | `제한 종료 기다리기` |
| `action_wait_capacity` | `처리 여유 기다리기` |
| `action_continue_intent` | `준비한 작업 계속` |
| `action_abandon_intent` | `준비한 작업 취소` |
| `action_reconcile_intent` | `작업 결과 확인` |
| `action_acknowledge_intent` | `확인하고 닫기` |
| `action_revoke_lost_secret` | `유실된 접속 키 폐기` |
| `lost_secret_revoke` | `접속 키 값이 유실되었습니다. 발급된 키를 폐기한 뒤 새로 발급하세요.` |
| `safe_fallback` | `현재 상태를 해석할 수 없습니다. 새 작업을 시작하지 말고 증거를 확인하세요.` |
| `system_hold.draining` | `안전한 전환을 위해 새 작업을 잠시 받지 않습니다. 끝날 때까지 기다리세요.` |
| `system_hold.management_not_ready` | `관리 준비가 끝날 때까지 새 작업을 시작할 수 없습니다.` |

Every admin wire error code has this separate owner-facing map. It never renders
the English wire `message`:

| Admin error code | Exact visible Korean copy |
| --- | --- |
| `invalid_admin_token` | `관리 접속 키가 올바르지 않습니다. 다시 로그인하세요.` |
| `admin_access_forbidden` | `이 주소에서는 관리 화면을 사용할 수 없습니다.` |
| `invalid_request` | `요청 형식을 확인하고 다시 시도하세요.` |
| `unsupported_media_type` | `JSON 요청만 사용할 수 있습니다.` |
| `request_too_large` | `요청이 너무 큽니다. 입력 크기를 줄이세요.` |
| `validation_failed` | `입력값을 확인하고 다시 시도하세요.` |
| `resource_not_found` | `대상을 찾을 수 없습니다. 새 상태를 불러오세요.` |
| `stale_configuration` | `구성이 바뀌었습니다. 새 상태를 불러온 뒤 다시 시작하세요.` |
| `resource_conflict` | `현재 상태에서는 이 작업을 진행할 수 없습니다.` |
| `intake_draining` | `안전한 전환을 위해 새 작업을 잠시 받지 않습니다.` |
| `operation_id_conflict` | `같은 작업 번호가 다른 요청에 사용되었습니다. 새 작업을 시작하세요.` |
| `database_unavailable` | `상태 저장소에 연결할 수 없습니다. 잠시 뒤 다시 확인하세요.` |
| `service_unavailable` | `서비스를 사용할 수 없습니다. 잠시 뒤 다시 확인하세요.` |
| `request_timeout` | `작업 시작 전에 시간이 초과되었습니다. 새 상태를 확인하세요.` |
| `mutation_outcome_unknown` | `작업 결과를 확인할 수 없습니다. 같은 요청을 다시 보내지 말고 결과를 확인하세요.` |
| `internal_error` | `작업을 완료하지 못했습니다. 새 상태와 증거를 확인하세요.` |
| `method_not_allowed` | `이 화면에서는 해당 작업을 사용할 수 없습니다.` |

Every `AdminOperation` state, phase, error, and failure class also has one exact
label or guidance sentence:

| Operation code | Exact visible Korean copy |
| --- | --- |
| `state.queued` | `확인 대기 중` |
| `state.running` | `확인 중` |
| `state.cancel_requested` | `취소 후 정리 중` |
| `state.succeeded` | `완료됨` |
| `state.failed` | `완료하지 못함` |
| `state.cancelled` | `취소됨` |
| `state.recovery_required` | `복구 확인 필요` |
| `phase.accepted` | `작업을 접수했습니다.` |
| `phase.planning` | `확인 순서를 준비하고 있습니다.` |
| `phase.case_dispatched` | `NVIDIA 기능을 확인하고 있습니다.` |
| `phase.case_cleanup` | `외부 미디어를 정리하고 있습니다.` |
| `phase.drain_requested` | `새 요청을 멈추고 있습니다.` |
| `phase.draining` | `진행 중인 요청이 끝나기를 기다리고 있습니다.` |
| `phase.committing` | `확인된 변경을 저장하고 있습니다.` |
| `phase.reopening` | `요청 처리를 다시 열고 있습니다.` |
| `phase.terminal` | `작업이 끝났습니다.` |
| `error.probe_failed` | `일부 기능 확인에 실패했습니다. 표시된 증거를 확인하세요.` |
| `error.probe_interrupted` | `기능 확인이 중단되었습니다. 정리 결과를 확인하세요.` |
| `error.probe_cancelled` | `기능 확인을 취소했습니다. 정리가 끝났는지 확인하세요.` |
| `error.configuration_changed` | `확인 중 구성이 바뀌었습니다. 새 상태에서 다시 시작하세요.` |
| `error.asset_cleanup_required` | `외부 미디어 정리가 끝나야 다음 작업을 할 수 있습니다.` |
| `error.provider_failure` | `NVIDIA가 기능 확인을 완료하지 못했습니다. 증거를 확인하세요.` |
| `error.reset_cancelled` | `슬롯 정리 전에 초기화를 취소했습니다. 새 상태를 확인하세요.` |
| `error.reset_interrupted` | `슬롯 정리 전에 초기화가 중단되었습니다. 새 상태를 확인하세요.` |
| `error.reset_failed_before_commit` | `슬롯 상태는 바뀌지 않았습니다. 증거에서 실패 원인을 확인하세요.` |
| `error.reset_reopen_required` | `두 슬롯은 비워졌지만 관리 화면이 아직 다시 열리지 않았습니다.` |
| `failure.invalid_credential` | `NVIDIA 키가 인증되지 않았습니다.` |
| `failure.not_entitled` | `이 NVIDIA 키에는 해당 모델 사용 권한이 없습니다.` |
| `failure.credits_exhausted` | `이 NVIDIA 키에서 사용할 수 있는 크레딧이 없습니다.` |
| `failure.rate_limited` | `NVIDIA 요청 제한이 적용되었습니다.` |
| `failure.transient` | `NVIDIA 연결이 일시적으로 불안정했습니다.` |
| `failure.protocol` | `NVIDIA 응답 형식이 검증된 계약과 다릅니다.` |
| `failure.request_contract` | `확인 요청이 검증된 계약과 맞지 않습니다.` |
| `failure.cancelled` | `사용자가 기능 확인을 취소했습니다.` |
| `failure.interrupted` | `재시작으로 기능 확인이 중단되었습니다.` |
| `failure.configuration_changed` | `기능 확인 중 구성이 바뀌었습니다.` |
| `failure.cleanup_required` | `외부 미디어 정리가 필요합니다.` |
| `failure.reset_failed_before_commit` | `초기화가 적용되기 전에 실패했습니다.` |
| `failure.reset_reopen_required` | `초기화 적용 후 관리 화면 복구가 필요합니다.` |
| `failure.unknown` | `확인되지 않은 실패입니다. 새 작업을 시작하지 말고 증거를 확인하세요.` |

Routing and Models use these exhaustive reason/action maps. Capacity is never
described as a timed cooldown:

| Routing/model code | Exact visible Korean copy |
| --- | --- |
| `reason.available` | `지금 요청할 수 있습니다.` |
| `reason.unconfigured` | `두 슬롯 구성이 필요합니다.` |
| `reason.missing_proof` | `현재 기능 확인 증거가 없습니다.` |
| `reason.stale_proof` | `기능 확인 증거가 현재 계약과 다릅니다.` |
| `reason.manual_disabled` | `운영자가 이 슬롯을 잠시 제외했습니다.` |
| `reason.invalid_credential` | `NVIDIA 키가 인증되지 않았습니다.` |
| `reason.not_entitled` | `이 키에는 해당 모델 사용 권한이 없습니다.` |
| `reason.credits_exhausted` | `이 키에서 사용할 수 있는 크레딧이 없습니다.` |
| `reason.protocol_quarantined` | `응답 계약 차이로 이 경로를 격리했습니다.` |
| `reason.rate_cooldown` | `NVIDIA 요청 제한이 끝날 때까지 기다려야 합니다.` |
| `reason.transient_cooldown` | `일시 오류 대기가 끝날 때까지 기다려야 합니다.` |
| `reason.capacity` | `현재 처리 중인 요청이 끝나면 다시 확인하세요.` |
| `routing_action.probe` | `새 기능 확인을 시작하세요.` |
| `routing_action.enable` | `검증된 슬롯을 다시 사용하세요.` |
| `routing_action.replace_slot` | `인증되지 않은 슬롯을 교체하세요.` |
| `routing_action.reset_upstream_pair` | `두 슬롯을 초기화하고 다시 등록하세요.` |
| `routing_action.inspect_evidence` | `원인과 계약 증거를 확인하세요.` |
| `routing_action.wait_expiry` | `표시된 종료 시각까지 기다리세요.` |
| `routing_action.wait_capacity` | `진행 중인 요청이 끝난 뒤 새로고침하세요.` |

The server-authoritative mutation intent is rendered only with this map:

| Intent code | Exact visible Korean copy |
| --- | --- |
| `intent.prepared_nonsecret` | `작업이 준비되었습니다. 계속하거나 취소하세요.` |
| `intent.prepared_secret` | `비밀값은 저장되지 않았습니다. 준비한 작업을 취소하고 다시 입력하세요.` |
| `intent.executing` | `작업 결과를 확인하고 있습니다. 같은 요청을 다시 보내지 마세요.` |
| `intent.terminal_succeeded` | `작업이 완료되었습니다. 결과를 확인하고 닫으세요.` |
| `intent.terminal_rejected_stable` | `작업을 시작하지 못했습니다. 원인을 확인하고 닫으세요.` |
| `intent.terminal_abandoned` | `준비한 작업을 취소했습니다. 확인하고 닫으세요.` |

Attention and operation kinds use these total maps; no raw enum spelling is
shown as a human label:

| Owner code | Exact visible Korean copy |
| --- | --- |
| `attention.asset_create_unknown` | `외부 미디어 생성 결과를 확인해야 합니다.` |
| `attention.asset_cleanup_pending` | `외부 미디어 정리가 끝나지 않았습니다.` |
| `attention.operation_recovery_required` | `중단된 작업의 결과와 정리를 확인해야 합니다.` |
| `attention.probe_failed` | `기능 확인에 실패한 항목이 있습니다.` |
| `attention.configuration_stale` | `현재 증거가 새 구성과 맞지 않습니다.` |
| `attention.backup_gate_missing` | `배포 전에 필요한 외부 백업 증거가 없습니다.` |
| `attention.release_failed` | `배포 전환을 완료하지 못했습니다.` |
| `attention.hermes_failed` | `Hermes 연결 또는 실제 작업 확인에 실패했습니다.` |
| `attention.provider_contract_drift` | `NVIDIA 응답 계약이 검증된 형태와 다릅니다.` |
| `attention.public_route_degraded` | `로컬 게이트웨이는 정상일 수 있지만 공개 주소·터널 경로 확인이 필요합니다.` |
| `operation_kind.upstream_create` | `후보 키 등록` |
| `operation_kind.upstream_probe` | `후보 키 기능 확인` |
| `operation_kind.upstream_assign` | `슬롯 배정` |
| `operation_kind.replacement_create` | `교체 후보 등록` |
| `operation_kind.replacement_probe` | `교체 후보 기능 확인` |
| `operation_kind.replacement_commit` | `슬롯 교체` |
| `operation_kind.staged_delete` | `후보 키 정리` |
| `operation_kind.slot_disable` | `슬롯 사용 중지` |
| `operation_kind.slot_enable` | `슬롯 다시 사용` |
| `operation_kind.upstream_reset` | `두 슬롯 초기화` |
| `operation_kind.downstream_issue` | `접속 키 발급` |
| `operation_kind.downstream_revoke` | `접속 키 폐기` |
| `operation_kind.operation_cancel` | `작업 취소` |

Progress and result discriminants are likewise never rendered by string
prettification:

| Owner code | Exact visible Korean copy |
| --- | --- |
| `progress.probe` | `기능 확인 진행` |
| `progress.reset` | `두 슬롯 초기화 진행` |
| `reset_stage.withdrawing` | `새 요청을 멈추는 중` |
| `reset_stage.waiting_zero` | `진행 중인 요청이 끝나기를 기다리는 중` |
| `reset_stage.resetting` | `키와 슬롯 상태를 정리하는 중` |
| `reset_stage.reopening` | `관리 화면을 다시 여는 중` |
| `result.upstream_key` | `후보 키 결과` |
| `result.probe` | `기능 확인 결과` |
| `result.slot` | `슬롯 변경 결과` |
| `result.upstream_reset` | `두 슬롯 초기화 결과` |
| `result.downstream_credential` | `접속 키 결과` |
| `result.operation_cancel` | `작업 취소 결과` |

Every event and Evidence discriminant/status has this total owner copy:

| Owner code | Exact visible Korean copy |
| --- | --- |
| `event_kind.legacy_imported` | `이전 상태를 증거로 보존함` |
| `event_kind.legacy_token_revoked` | `이전 접속 키를 폐기함` |
| `event_kind.upstream_staged` | `후보 키를 등록함` |
| `event_kind.probe_terminal` | `기능 확인이 끝남` |
| `event_kind.assigned` | `키를 슬롯에 배정함` |
| `event_kind.replaced` | `슬롯 키를 교체함` |
| `event_kind.upstream_reset` | `두 슬롯을 초기화함` |
| `event_kind.retired` | `키를 사용 종료함` |
| `event_kind.slot_state_changed` | `슬롯 사용 상태를 변경함` |
| `event_kind.downstream_issued` | `접속 키를 발급함` |
| `event_kind.downstream_revoked` | `접속 키를 폐기함` |
| `event_kind.request_terminal` | `API 요청 처리가 끝남` |
| `event_kind.cooldown_changed` | `요청 대기 상태가 바뀜` |
| `event_kind.quarantine_changed` | `응답 계약 격리 상태가 바뀜` |
| `event_kind.asset_attention` | `외부 미디어 확인이 필요함` |
| `event_kind.asset_quarantined` | `외부 미디어를 정리 전용으로 격리함` |
| `event_kind.operation_recovery` | `작업 복구 확인이 필요함` |
| `event_kind.attention_opened` | `확인이 필요한 상태를 기록함` |
| `event_kind.attention_resolved` | `확인이 필요한 상태가 해소됨` |
| `event_kind.backup_terminal` | `백업 작업이 끝남` |
| `event_kind.restore_terminal` | `복원 작업이 끝남` |
| `event_kind.release_terminal` | `배포 전환이 끝남` |
| `event_kind.hermes_terminal` | `Hermes 연결 작업이 끝남` |
| `severity.info` | `정보` |
| `severity.attention` | `확인 필요` |
| `outcome.succeeded` | `성공` |
| `outcome.failed` | `실패` |
| `outcome.cancelled` | `취소됨` |
| `outcome.recovery_required` | `복구 확인 필요` |
| `evidence_variant.probe_case` | `기능 확인 항목` |
| `evidence_variant.routing_state_transition` | `라우팅 상태 변경` |
| `evidence_variant.asset_cleanup` | `외부 미디어 정리` |
| `evidence_variant.spool_orphan` | `재시작 후 미디어 잔여물을 정리함` |
| `evidence_variant.profile_contract` | `모델 계약` |
| `evidence_variant.operation_terminal` | `작업 종료` |
| `evidence_variant.backup` | `백업` |
| `evidence_variant.restore` | `복원` |
| `evidence_variant.release` | `배포 전환` |
| `evidence_variant.hermes` | `Hermes 연결` |
| `evidence_status.pass` | `통과` |
| `evidence_status.failed` | `실패` |
| `evidence_status.cancelled` | `취소됨` |
| `evidence_status.abandoned_after_restart` | `재시작 후 중단됨` |
| `evidence_state.succeeded` | `성공` |
| `evidence_state.failed` | `실패` |
| `evidence_state.attention` | `확인 필요` |
| `cleanup.none` | `정리할 외부 미디어 없음` |
| `cleanup.pending` | `외부 미디어 정리 중` |
| `cleanup.unknown` | `외부 미디어 정리 결과 확인 필요` |
| `cleanup.complete` | `외부 미디어 정리 완료` |
| `cleanup.revoked_cleanup_closed` | `키 폐기 후 외부 미디어 정리 종료` |
| `routing_cause.upstream_401` | `NVIDIA가 키 인증을 거부함` |
| `routing_cause.upstream_403` | `NVIDIA가 모델 권한을 거부함` |
| `routing_cause.upstream_402` | `NVIDIA 크레딧을 사용할 수 없음` |
| `routing_cause.upstream_429` | `NVIDIA 요청 제한이 적용됨` |
| `routing_cause.transient_failure` | `NVIDIA 연결이 일시적으로 실패함` |
| `routing_cause.protocol_failure` | `NVIDIA 응답 계약이 다름` |
| `routing_cause.probe_success` | `새 기능 확인이 통과함` |
| `routing_cause.manual_disabled` | `운영자가 슬롯 사용을 중지함` |
| `routing_cause.manual_enabled` | `운영자가 슬롯 사용을 다시 시작함` |
| `routing_applied.true` | `현재 라우팅 상태에 반영됨` |
| `routing_applied.false` | `더 새로운 상태가 있어 기록만 보존됨` |
| `routing_remediation.none` | `추가 조치 없음` |
| `routing_remediation.external_fix_then_probe` | `NVIDIA 계정 조치 후 새 기능 확인 필요` |

Readiness, lifecycle, credential, route, and modality values use only the
following labels. Profile IDs, Case IDs, scope IDs, UUIDs, hashes, and HTTP
codes remain exact technical values inside separately labelled `<code>` or
definition-list values; they are never passed through a humanizing fallback.

| Owner code | Exact visible Korean copy |
| --- | --- |
| `readiness.management_ready` | `관리 가능` |
| `readiness.management_not_ready` | `관리 준비 안 됨` |
| `readiness.traffic_ready` | `두 키와 모델 검증 완료` |
| `readiness.traffic_not_ready` | `두 키와 모델 검증 필요` |
| `readiness.draining` | `새 요청을 잠시 받지 않음` |
| `readiness.accepting` | `새 요청을 받고 있음` |
| `readiness.public_ok` | `공개 주소 응답 정상` |
| `readiness.public_degraded` | `공개 주소 응답 확인 필요` |
| `readiness.public_unknown` | `공개 주소 확인 결과가 아직 없거나 오래되었습니다.` |
| `availability.some` | `현재 요청 가능한 모델이 있습니다.` |
| `availability.none` | `현재 요청 가능한 모델이 없습니다. 라우팅 원인을 확인하세요.` |
| `lifecycle.staged` | `확인 중인 후보` |
| `lifecycle.assigned` | `슬롯에 배정됨` |
| `lifecycle.retirement_pending` | `외부 미디어 정리 후 종료 예정` |
| `lifecycle.retired` | `사용 종료됨` |
| `key_health.clear` | `인증 이상 없음` |
| `key_health.invalid_credential` | `키 인증 실패` |
| `slot.enabled` | `사용 중` |
| `slot.disabled` | `사용 중지` |
| `credential.active` | `사용 중` |
| `credential.revoked` | `폐기됨` |
| `staged_intent.first` | `슬롯 1 구성` |
| `staged_intent.second` | `슬롯 2 구성` |
| `staged_intent.replacement` | `슬롯 키 교체` |
| `entitlement.unknown` | `권한 확인 전` |
| `entitlement.ok` | `모델 권한 확인됨` |
| `entitlement.not_entitled` | `모델 권한 없음` |
| `entitlement.credits_exhausted` | `사용 가능한 크레딧 없음` |
| `protocol.clear` | `응답 계약 이상 없음` |
| `protocol.protocol_quarantined` | `응답 계약 차이로 격리됨` |
| `cooldown.none` | `요청 대기 없음` |
| `cooldown.rate` | `요청 제한 대기 중` |
| `cooldown.transient` | `일시 오류 대기 중` |
| `cooldown.rate_and_transient` | `요청 제한·일시 오류 대기 중` |
| `resource.upstream_key` | `NVIDIA 키` |
| `resource.downstream_credential` | `접속 키` |
| `resource.operation` | `작업` |
| `resource.asset` | `외부 미디어` |
| `resource.backup` | `백업` |
| `resource.release` | `배포 전환` |
| `resource.hermes` | `Hermes 연결` |
| `resource.slot` | `라우팅 슬롯` |
| `resource.profile` | `모델` |
| `resource.public_route` | `공개 접속 경로` |
| `route.chat` | `대화` |
| `route.embeddings` | `임베딩` |
| `route.images` | `이미지 생성` |
| `route.speech` | `음성 생성` |
| `route.nvidia_native` | `NVIDIA 전용 미디어` |
| `modality.text` | `텍스트` |
| `modality.image` | `이미지` |
| `modality.audio` | `오디오` |
| `modality.video` | `비디오` |
| `modality.vector` | `벡터` |
| `voice_kind.base` | `기본 음성` |
| `voice_kind.emotion` | `감정 음성` |
| `model.advertised` | `검증되어 모델 목록에 표시됨` |
| `model.not_advertised` | `검증이 끝나지 않아 모델 목록에 표시되지 않음` |
| `model.available_now` | `지금 요청 가능` |
| `model.unavailable_now` | `지금 요청할 수 없음` |

All recurring controls, destructive dialogs, and field labels use this closed
copy set. A route-specific control composes one of these strings with separately
labelled slot/profile values; it does not invent another synonym.

| Copy code | Exact visible Korean copy |
| --- | --- |
| `control.configure_slot1` | `슬롯 1 구성 시작` |
| `control.configure_slot2` | `슬롯 2 구성 시작` |
| `control.register_candidate` | `후보 키 등록` |
| `control.start_probe` | `기능 확인 시작` |
| `control.review_candidate` | `확인 결과 검토` |
| `control.assign_slot1` | `슬롯 1에 배정` |
| `control.assign_slot2` | `슬롯 2에 배정` |
| `control.start_replacement` | `이 슬롯 교체 시작` |
| `control.commit_replacement` | `확인한 키로 교체` |
| `control.delete_candidate` | `후보 키 정리` |
| `control.disable_slot` | `이 슬롯 사용 중지` |
| `control.enable_slot` | `이 슬롯 다시 사용` |
| `control.cancel_operation` | `진행 중인 작업 취소` |
| `control.issue_credential` | `접속 키 발급` |
| `control.revoke_credential` | `접속 키 폐기` |
| `control.refresh` | `새로고침` |
| `control.copy_secret` | `접속 키 복사` |
| `control.clear_secret` | `접속 키 화면에서 지우기` |
| `control.revoke_and_close` | `접속 키 폐기 후 닫기` |
| `control.logout` | `로그아웃` |
| `control.open_evidence` | `정확한 증거 열기` |
| `control.view_all_evidence` | `전체 증거 기록 보기` |
| `control.view_attention_evidence` | `확인 필요한 증거 보기` |
| `control.retry_evidence` | `증거 다시 불러오기` |
| `control.close_dialog` | `닫기` |
| `control.go_back` | `이전 단계` |
| `control.confirm_reset` | `두 슬롯 초기화 시작` |
| `control.confirm_external_action` | `외부 조치를 마쳤습니다` |
| `control.login` | `관리 화면 열기` |
| `control.cancel_login` | `로그인 취소` |
| `control.skip_to_content` | `본문으로 건너뛰기` |
| `control.load_more` | `더 보기` |
| `control.retry_bootstrap` | `페이지 다시 불러오기` |
| `control.wait_system` | `관리 작업이 끝날 때까지 기다리기` |
| `dialog.cancel.title` | `진행 중인 작업을 취소할까요?` |
| `dialog.delete_candidate.title` | `후보 키를 정리할까요?` |
| `dialog.disable_slot.title` | `이 슬롯 사용을 중지할까요?` |
| `dialog.revoke_credential.title` | `이 접속 키를 폐기할까요?` |
| `dialog.reset.title` | `두 슬롯을 초기화할까요?` |
| `dialog.reset.consequence` | `배정된 키와 확인 중인 후보를 모두 사용 종료합니다. 기존 접속 키는 폐기하지 않습니다.` |
| `dialog.external_fix.title` | `외부 NVIDIA 조치를 확인할까요?` |
| `dialog.external_fix.consequence` | `자동 재시도나 계정 변경은 하지 않습니다. NVIDIA 계정에서 권한 또는 크레딧 조치를 마친 뒤에만 확인을 시작합니다.` |
| `field.admin_token` | `관리 접속 키` |
| `heading.login` | `NVIDIA Build LB 관리 화면` |
| `title.login` | `관리 로그인 · NVIDIA Build LB` |
| `heading.bootstrap_failed` | `관리 화면을 시작할 수 없습니다` |
| `title.bootstrap_failed` | `시작 오류 · NVIDIA Build LB` |
| `field.upstream_label` | `NVIDIA 키 이름` |
| `field.upstream_credential` | `NVIDIA API 키` |
| `field.replacement_slot` | `교체할 슬롯` |
| `field.downstream_label` | `접속 키 이름` |
| `field.downstream_scopes` | `사용 권한` |
| `field.reset_acknowledgement` | `배정된 키와 후보가 사용 종료됨을 확인했습니다.` |
| `field.cleanup_phrase` | `클립보드 정리 문구` |
| `field.cleanup_paste` | `정리 문구 붙여넣기` |
| `evidence.attention_heading` | `확인이 필요한 증거` |
| `evidence.history_heading` | `전체 증거 기록` |
| `evidence.direct_heading` | `증거 상세` |
| `evidence.attention_empty` | `지금 확인할 주의 증거가 없습니다.` |
| `evidence.history_empty` | `아직 저장된 증거 기록이 없습니다.` |
| `evidence.direct_loading` | `선택한 증거를 불러오는 중입니다.` |
| `evidence.direct_not_found` | `선택한 증거를 찾을 수 없습니다. 현재 상태를 새로고침하세요.` |
| `evidence.direct_error` | `선택한 증거를 불러오지 못했습니다. 다시 시도하거나 전체 기록을 확인하세요.` |
| `reset.result_heading` | `두 슬롯 초기화 결과` |
| `reset.zero_slots` | `두 슬롯이 비었습니다. 슬롯 1부터 다시 구성하세요.` |
| `reset.cancel_cutoff` | `키 정리가 시작되어 이제 취소할 수 없습니다.` |
| `clipboard.revoke_escape` | `클립보드 정리를 확인할 수 없으면 이 접속 키를 폐기해 더 이상 사용할 수 없게 한 뒤 닫을 수 있습니다.` |
| `custody.not_observed` | `접속 키를 복사하지 않았다면 화면에서 지워 안전하게 닫으세요.` |
| `custody.secret_copy_requested` | `클립보드 복사 결과를 기다리고 있습니다.` |
| `custody.secret_copy_unknown` | `복사 결과를 확인할 수 없습니다. 접속 키를 폐기해 안전하게 닫으세요.` |
| `custody.secret_present` | `접속 키를 옮겼다면 화면과 클립보드를 정리하세요.` |
| `custody.overwrite_requested` | `클립보드 정리 결과를 확인하고 있습니다.` |
| `custody.overwrite_unknown` | `자동 정리 결과를 확인할 수 없습니다. 정리 문구를 직접 복사해 붙여넣으세요.` |
| `custody.manual_required` | `정리 문구를 직접 복사한 뒤 아래 칸에 붙여넣으세요.` |
| `custody.manual_paste_invalid` | `방금 표시된 정리 문구를 복사해 붙여넣으세요.` |
| `custody.overwrite_verified` | `화면과 클립보드 정리를 확인했습니다.` |
| `custody.manual_overwrite_verified` | `직접 수행한 클립보드 정리를 확인했습니다.` |
| `custody.revoke_pending` | `접속 키 폐기 결과를 확인하고 있습니다. 다시 요청하지 마세요.` |
| `custody.revoked` | `접속 키가 폐기되어 더 이상 사용할 수 없습니다.` |
| `bootstrap.noscript` | `관리 화면을 사용하려면 자바스크립트를 켠 뒤 페이지를 다시 불러오세요.` |
| `bootstrap.failed` | `관리 화면을 안전하게 시작하지 못했습니다. 비밀값을 입력하지 말고 페이지를 다시 불러오세요.` |

`prepared` secret-bearing intents never offer Continue because the plaintext is
not server-persisted; they compose `intent.prepared_secret` with only
`action_abandon_intent` and require a fresh UUID and fresh entry afterward.
Prepared nonsecret intents use `intent.prepared_nonsecret` and, when this
document still owns the exact validated fields, render Continue as the sole
primary action and Abandon as the secondary action. If those fields do not
validate, they offer only Abandon.
`executing` offers only `action_reconcile_intent`. A terminal intent offers only
`action_acknowledge_intent`, except a downstream issue whose stored operation
has `secret_available:false`: it composes `lost_secret_revoke` and
`action_revoke_lost_secret`, and blocks new issue until that exact credential ID
is revoked and the revoke intent acknowledged.

Every recommendation, Attention, resource, readiness, lifecycle, health,
entitlement, protocol, cooldown, route, modality, operation kind/state/phase/
progress/result/error/failure, routing reason/action/cause/remediation, event
kind/severity/outcome, Evidence variant/status/cleanup, intent state/outcome,
loading/empty/offline/conflict, control/dialog/field, and plaintext-custody code
maps to exactly one row or to the explicit composition above. An otherwise
valid response with an enum value not known to the embedded validator is
treated as a contract fault: all mutations lock, `safe_fallback` plus only
`control.view_all_evidence` is rendered, and a fresh snapshot cannot unlock it in
that build. The raw unknown value is not displayed. Unmapped codes fail the
Rust/TypeScript exhaustiveness build; English operation wire messages are never
rendered as owner guidance.

`/admin` and `/showcase` HTML use `Cache-Control: no-store`, `Vary:
Accept-Encoding`, and the strong ETag of the selected identity or deterministic
gzip representation for evidence. They ignore `If-None-Match` and always return
the selected 200 body, never 304. Content-addressed JS/CSS/SVG assets use
`Cache-Control: public,max-age=31536000,immutable`; stable `/favicon.svg` uses
`public,max-age=0,must-revalidate`. Every other compressible response has
`Vary: Accept-Encoding`, distinct strong quoted ETags for identity and
deterministic gzip, and 304 only against the selected representation. Valid
`gzip` with q>0 selects gzip; absent/disabled gzip, a malformed member, or an
unsupported coding selects identity unless identity is explicitly q=0. If both
gzip and identity are forbidden, return 406 with no body. This prevents cached
HTML from referencing removed chunks across generations.

### 12.1 Information architecture and flows

Primary route IDs are the closed enum
`AdminRouteId="overview"|"routing"|"clients"|"models"|"evidence"`; the
canonical navigation/title copy map is:

| `admin_route.*` | exact Korean navigation/title |
| --- | --- |
| `admin_route.overview` | `개요` |
| `admin_route.routing` | `라우팅` |
| `admin_route.clients` | `접속 키` |
| `admin_route.models` | `모델` |
| `admin_route.evidence` | `증거` |

The map is total and order-fixed; navigation labels and document titles use the
same value, so no route string may be invented in a component. The first
Overview viewport has five compact judgments:
structural gateway readiness plus current availability/freshness, configured
slots, eligible profiles/keys, newest attention item, and
last operation result. It has one recommended action or explicitly says no
action is needed. Detailed counters, full labels, profile proofs, events, and
receipts are progressively disclosed.

The first judgment never equates structural `traffic_ready` with momentary
routability. It labels the former `readiness.traffic_ready|traffic_not_ready`,
then immediately renders `현재 요청 가능 모델 <currently_available_profiles>/8`
with `availability.some|none`. A structurally ready installation with every key
cooling, capacity-bound, or manually disabled therefore says “검증 완료” and
“현재 요청 가능한 모델이 없습니다” together, never `API 사용 가능`. Public
health wording is explicitly configuration health, while Routing/Models owns
the current reason and expiry.

These are five link-style in-document routes with exact fragments
`#overview|#routing|#clients|#models|#evidence`, not ARIA tabs. Each navigation
item is a real `<a>`; exactly one has `aria-current="page"`. Activation uses
`history.pushState`, Back/Forward uses `popstate`, and direct/reload fragment
selection occurs only after login without persisting owner data. Before auth,
the shell retains only a closed requested route enum: a known initial fragment
maps to that enum and an absent/unknown fragment maps to Overview; it retains no
URL-derived free text. After successful auth and one fresh Overview, an open
mutation intent takes priority: the app uses `history.replaceState` to
`#overview`, sets Overview title/current navigation, renders the intent surface,
and focuses its heading. With no open intent, it selects the retained requested
route; when the original fragment was absent or unknown it first uses
`history.replaceState` to canonical `#overview`, and for every selected route it
sets the exact title/current navigation and focuses that route h1. A later intent
acknowledgement stays on Overview; it never silently returns to an old fragment.
Thus absent/unknown direct entry, `#models`/`#evidence` direct entry, and
open-intent recovery cannot disagree
about URL, title, `aria-current`, rendered h1, or focus. A successful
route change sets exact title `<Korean label> · NVIDIA Build LB`, resets the
main scroll container to zero for a new click (Back/Forward restores its saved
integer scroll offset), updates the sole `<h1 tabindex="-1">`, focuses that h1
with `preventScroll:true`, then announces `<label> 화면` once in a polite live
region. Polling and row rerender never move focus or announce a route. If
plaintext custody blocks navigation, focus remains in its dialog; route
rendering never changes and same-document traversal is compensated by the
custody guard below. Mobile/rail links share the same state and there is never more
than one current item. Browser gates cover keyboard activation, current state,
title, focus, announcement, Back/Forward, scroll restoration, and polling
focus noninterference for all five routes.

The app sets `history.scrollRestoration="manual"` once before the first route
render. It stores one integer scroll offset in each history state under the
closed field `scroll` of `{version:1,route,nav_seq,scroll}`, writes it
immediately before every push/replace, and
restores it only from the matching `popstate` entry after the route DOM is
stable; a route click always writes zero and never consumes a prior offset.
`requestAnimationFrame` is used once as the bounded DOM-stability fence, then
the h1 focus uses `preventScroll:true`; a second popstate/scroll handler or
browser automatic restoration is forbidden. Browser assertions cover deep-link,
Back, Forward, reload, and custody-blocked traversal with exactly one restore
and no double jump.

The pure
`recommendedPlan(snapshot,snapshotCondition,localCustody,openMutationIntent)`
returns
exactly one first action and never re-derives server lifecycle choices:

| Priority | Closed condition | One recommendation |
| ---: | --- | --- |
| 1 | locally held one-time plaintext | finish custody cleanup or the exact revoke escape; all other actions absent |
| 2 | `snapshotCondition="validator_fault"` | render `safe_fallback` and only `control.view_all_evidence`; no DTO-derived target or mutation is trusted |
| 3 | `snapshotCondition="offline"|"refresh_pending"|"stale"` | `control.refresh` only |
| 4 | fresh snapshot with `readiness.draining` or `management_ready:false`, and no already-accepted controller to reconcile | `control.wait_system` only; no mutation CTA |
| 5 | fresh snapshot and `openMutationIntent` nonnull | `prepared` nonsecret: exact Continue or Abandon for the stored canonical body; `prepared` secret: Abandon only; `executing`: exact reconcile; `terminal`: exact acknowledge/reconcile |
| 6 | fresh snapshot and no open intent | render the exact validated `snapshot.server_recommendation` union member |

`localCustody` is the private nonreactive one-shot state, never browser storage;
`snapshotCondition` is the closed local enum
`fresh|offline|refresh_pending|stale|validator_fault` computed by section 5.3's
clock/visibility/online validator. A validator fault cannot safely reuse an ID
from the rejected DTO, so its Evidence action opens the independently fetched
history route only. Offline/stale always outranks an intent; while
`refresh_pending` the in-flight GET is the sole refresh authority, its control
is present once with `aria-busy:true`, disabled, and `loading` copy, and
no second GET may dispatch. A fresh system drain or management-not-ready state
outranks a prepared intent: only `control.wait_system` is present until a fresh
Overview proves the hold gone. Continue/Abandon are absent in that state;
Reconcile/Acknowledge are allowed only for the already accepted controller's
exact executing/terminal intent. Once a fresh, non-held Overview returns, the
normal intent action resumes.
For a fresh non-held snapshot, `prepared` nonsecret renders only the stored
canonical-body Continue and Abandon actions (both snapshot/intent-bound),
`prepared` secret renders Abandon only, `executing` renders its exact
reconcile action, and `terminal` renders its exact acknowledge/reconcile
action. No state may fall through to the server recommendation or to a client-
reconstructed body; a missing or invalid prepared body is a validator fault.
`wait_system` is also closed: when `retry_at` is nonnull the response records
one local `(snapshot.observed_at,performance.now())` anchor and computes
`monotonic_deadline = anchor.performance_now + max(0,retry_at -
snapshot.observed_at)`; no client wall clock is consulted. The one
`control.wait_system` button is disabled until that monotonic deadline, exposes
`aria-disabled` and a Korean countdown/status copy, and then performs exactly
one fresh Overview GET; when it is null the same button performs one GET
immediately. It never starts a background poll or retries a mutation. The
button retains focus, sets `aria-busy:true` only for that GET, announces the
single transition through the status node, and returns the fresh snapshot to
the same pure plan. The capture matrix includes draining-before-deadline,
deadline-refresh, and release-to-management-ready transitions with zero
duplicate GETs and zero mutation CTA while waiting.
`openMutationIntent` must be byte-equal to the Overview field from that same
snapshot. The browser verifies every recommendation's referenced operation,
key, slot, repair, Attention, and Evidence target exists byte-for-byte in that
snapshot; mismatch is the unknown-contract fallback, not a second plan. While
any of the first four rows applies, every unrelated mutation control is absent from
the DOM rather than merely disabled. A stale/offline snapshot never
advances beyond a refresh action. The login shell is outside this function:
successful token validation loads one fresh overview and exposes no second
dashboard copy. Reload/multi-tab polling uses the same rules; a prepared
secret-bearing intent after document loss can only be abandoned because no
plaintext ownership is inferred. A prepared nonsecret intent needs no local
form-owner input: its exact validated nonsecret request bytes are in the server
intent and therefore always offer Continue plus Abandon once the snapshot is
fresh. A prepared secret intent never persists its body and offers Abandon only.

The validated server union has this total route/copy/control projection. It is
the only mapping from a `ServerRecommendation` to navigation or a primary CTA;
no page chooses a synonym or a second action:

| Server action | Destination and exact visible treatment |
| --- | --- |
| `monitor_operation` | Overview keeps the referenced operation surface in place, focuses it only after login/recovery, and exposes no primary CTA; polling is automatic. |
| `open_evidence` | Open the supplied direct target in the current route with `control.open_evidence`; never navigate or scan an index first. |
| `configure_slot` Slot 1/2 | Routing, exact slot heading, then `control.configure_slot1` or `control.configure_slot2`. |
| `start_probe` | Routing, the referenced staged-key heading, then `control.start_probe`; the displayed profile list is the exact union payload. |
| `review_staged` + `assign` | Routing review surface and `control.assign_slot1|control.assign_slot2` selected only from `target_slot`. |
| `review_staged` + `replace` | Routing review surface and `control.commit_replacement`. |
| `staged_recovery` | Stay on the referenced staged-key surface and use exactly the closed `next_action` mapping below. |
| `routing_repair` | Routing, the referenced profile/slot row, and the one `RoutingRepair.next_action` control below. |
| `issue_first_client` | Clients and `control.issue_credential`. |
| `none` | Overview renders `ready_no_action` and no primary CTA. |

`staged_recovery` maps `wait_cleanup`, `delete_staged`,
`quarantine_staged`, `retry_probe`, `refresh_then_retry`,
and `inspect_evidence` respectively to
`action_wait_cleanup`, `action_delete_staged`, `action_quarantine_staged`,
`action_retry_probe`, `action_refresh_retry`, and
`action_inspect_evidence`. A reset operation monitored by
`monitor_operation` maps its own `wait_reset_recovery` error action to
`action_wait_reset_recovery` without changing routes or allocating another
operation. A nonnull evidence
target is required for `inspect_evidence`; a nonnull retry timestamp is required
only where section 7.1 permits it. `RoutingRepair.next_action` maps `probe`,
`enable`, `replace_slot`, `reset_upstream_pair`, `inspect_evidence`,
`wait_expiry`, and `wait_capacity` to `control.start_probe`,
`action_enable_slot`, `action_replace_slot`, `action_reset_pair`,
`action_inspect_evidence`, `action_wait_expiry`, and
`action_wait_capacity`. Any payload that cannot satisfy this table fails the
DTO validator before rendering.

Routing shows permanent Slot 1 and Slot 2, profile-local eligibility, cooldown
reason/expiry, last proof, and one row-local action. Initial onboarding is two
explicit server-resumable wizards. “Configure Slot 1” is label + fresh
credential -> immediate DOM-field clear -> staged-create reconciliation -> all
seven-profile forced-key probe -> case/cleanup progress -> review key handle and
seven PASS rows -> assign Slot 1. “Configure Slot 2” is the same through full
per-key probe, then shows common voice count and same-voice pair PASS evidence,
reviews both slot handles, and assigns Slot 2. Slot 2 is never offered before
Slot 1. Refresh discovers the exact staged detail and resource-local operation
from the owner snapshot. Every mutation first creates and reads back the
server-side nonsecret intent from section 7.1 and every login reconciles the
one open intent; no guessed replay is needed. A lost secret-bearing create
response whose intent/operation committed never re-enters the credential: the
UI continues with the staged handle. If the intent remains `prepared` because
the create transaction did not commit, the UI abandons/acknowledges it and
requires a new UUID plus fresh credential entry; plaintext cannot be recovered
or replayed. Probe interruption shows the exact terminal
case and cleanup state; only a new operation ID can retry after reconciliation.
Assignment is enabled only for a current complete proof and, for Slot 2, a
current pair proof. After Slot 2, Overview advances to first client issuance.

Failed staged custody has one visible primary action from the server table:
wait shows the operation's `next_retry_at`; retry creates a new probe UUID;
refresh first fetches a fresh generation; Evidence opens the operation's exact
`evidence_target:{kind:"event",id:evidence_event_id}` (the
`evidence_event_id` is only the terminal-equality field, never a second UI
target); and `delete_staged|quarantine_staged` opens a destructive
dialog showing the snapshot-bound target Slot, full label, and
short fingerprint. Delete is enabled only with no other active/recovery pin and
all nonambiguous assets deleted. `create_unknown` changes the action to
`quarantine_staged`: confirmation moves the candidate to cleanup-only
`retirement_pending` with ciphertext retained, releases the staged position,
and leaves its durable watcher/evidence intact; it never claims provider
absence. Confirmation issues the existing idempotent DELETE with a fresh
operation ID, reconciles a lost response before changing the wizard, then
returns focus to the exact Slot heading and recommends fresh credential entry.
A running probe exposes Cancel as a secondary row action; cancel confirmation
uses the cancel-operation contract, focus returns to the probe status, and the
eventual terminal next action remains server-derived. No page presents Retry,
Delete, and Continue as competing primary actions.

The destructive reset is one resumable journey, not a fire-and-forget button.
Its dialog binds the fresh snapshot ID/generation and displays every currently
assigned handle plus the staged handle when present, using compact labels and
slots, followed by `dialog.reset.consequence`. One explicit confirmation
checkbox labelled `field.reset_acknowledgement` is required;
the client inserts the protocol constant `RESET UPSTREAM PAIR` only after that
trusted click and never asks the user to type an English sentinel. Closing the
dialog before dispatch returns focus to the exact reset trigger. Acceptance
closes the dialog, focuses a persistent `두 슬롯 초기화 진행` heading, and
renders the server's reset stage. Cancel is offered only while the returned
operation says `cancellable:true`; the moment it becomes false the control is
removed and `reset.cancel_cutoff` is shown. A cancel response is reconciled by
its own operation and never assumed from a click.

Reload, login, another tab, or Back/Forward never dispatches reset again. The
open mutation intent and active operation restore the same progress surface;
the document focuses its heading after login and polls only the exact operation
ID. Failed/cancelled-before-commit terminal state proves the configuration is
unchanged and offers its server-derived recovery. A succeeded terminal renders
`reset.result_heading`, every `affected_keys` row with prior/final lifecycle,
and `reset.zero_slots`; it asserts `configured_slots:"0"` and that the staged
row is absent before acknowledging the intent. A mismatch locks mutation and
opens exact Evidence. After acknowledgement focus moves to the result heading,
then the sole recommendation becomes `control.configure_slot1`. No success
screen offers Slot 2, Retry Reset, or a stale pre-reset action.

For exact `not_entitled|credits_exhausted`, the first action remains Evidence;
that typed routing/probe evidence alone shows `external_account_action` and one
`action_external_fix_probe`. Activating it opens a confirmation stating that no
automatic retry or account change occurs and requires the user to confirm the
external NVIDIA permission/credit action is complete. It refreshes generation,
prepares a fresh intent/UUID, and calls the existing staged or assigned key probe
with the one failed profile ID. The persistent block remains until success. If
the target has a second identity, the normal current pair proof—including
Magpie same-voice cases—must complete before availability returns. Success
returns focus to the exact profile heading; failure returns to its new Evidence.
No delete/re-register workaround or same operation replay is offered. Staged
and assigned 402/403 journeys are separate browser cases.

Replacement is one wizard:
select source slot -> enter fresh credential -> clear DOM custody -> full probe
-> review exact source/replacement handles -> atomic commit -> automatic old-key
retirement. There is no post-swap Disable/Delete fiction. Lost response shows
unknown state, locks mutation, refreshes, reconciles operation ID, then restores
focus to the exact source row.

Clients shows scoped downstream credentials and one-time issuance. Models shows
only the seven closed profiles, modalities, route, both-key proof state, and
availability. A profile row's Evidence disclosure owns full source URL/hash and
proof receipts; hashes never appear in the default Models hierarchy. Evidence
starts with failed/attention receipts and keeps raw audit behind disclosure. No
dashboard chart, marketing hero, repeated KPI, or decorative telemetry competes
with the next decision.

Evidence is attention-first without duplicating two lists. Entering the route
loads `/attentions?limit=20` and renders `evidence.attention_heading`; its
`control.load_more` follows only that active cursor. An empty result renders
`evidence.attention_empty`. A secondary `control.view_all_evidence` replaces
the list with `/events?limit=20` and `evidence.history_heading`; returning to
attention with `control.view_attention_evidence` replaces it again. Neither
mode merges pages or searches cursors in
the browser. An empty general page renders `evidence.history_empty`; its
pagination also uses only `control.load_more`.

An Evidence trigger is rendered only when its owning DTO has a nonnull closed
direct target `{kind:"event"|"evidence",id}`. Every rendered Overview,
Routing, Models, operation, or Attention trigger carries that supplied target;
rows with a contractually null target—such as capacity—render no inert or
guessed Evidence control. Activation opens one modal, focuses its
`evidence.direct_heading`, and requests
exactly `/events/<id>` or `/evidence/<id>`; it never walks `/events`, guesses an
ID, or scans a cursor page. The heading remains the focus anchor while the body
shows `evidence.direct_loading`, the typed result, the exact 404
`evidence.direct_not_found`, or `evidence.direct_error`. A 404 offers only
`control.refresh`: it closes the dialog, performs one safe fresh Overview GET,
and returns focus to the connected trigger/row heading if it still exists or
the current route h1 otherwise; it never retries the missing ID. A general
error offers primary `control.retry_evidence` and secondary
`control.view_all_evidence`. Retry repeats only the same safe GET and history
first closes the dialog under the route contract. Closing from any state
aborts that GET, removes the dialog, and restores focus to the exact connected
trigger or its owning row heading if polling removed the trigger. Opening a
history route from the error state first closes the dialog, changes route, and
focuses the Evidence h1 under the normal route contract.

Repeated row controls never rely on the visible label alone. Each Evidence,
Cancel, Enable, Replace, and similar control has a unique accessible name built
from `aria-labelledby` references to its owning row heading plus the mapped
resource/profile/slot/operation context; the direct target ID is not spoken as
the only context. The same row context is present in the DOM before polling and
is retained on the connected trigger used for focus return. Browser assertions
enumerate every repeated control, require unique computed names and one owning
heading, and verify that polling cannot attach a control to a different row.

### 12.2 State, accessibility, responsiveness, and visual grammar

Session, snapshot, operation, rotation, and credential custody are typed closed
state machines. The UI surface state is exactly
`loading|empty|partial|ready|degraded|stale|error|offline|conflict|success|recovery`.
`partial` means the immutable Overview snapshot is valid but a noncritical
secondary page/row is unavailable and offers only its exact Evidence/refresh;
`degraded` means a valid snapshot has public-route or structural degradation and
offers the mapped Attention evidence/wait action. Neither state invents a
missing DTO field. `snapshot + local intent` maps through a pure
recommended-plan function. Mutation is disabled for stale/unknown snapshots.
`ready|error|success|recovery|empty` always carry their context discriminant
(overview, models, operation, evidence, or custody) and render only that
context's mapped copy; the generic state names are never displayed as raw
labels.
Every loading, empty, partial, stale, degraded, error, offline, conflict,
success, and recovery state preserves context, explains consequence, and offers
one safe recovery.
On blocking/form failure, the error summary is the one automatic focus target.
Its field links move focus only when the user activates them; code never
immediately steals focus again. Inline validation announced after user input
does not auto-focus. After a resolved/dismissed dialog or operation, focus
returns to the exact connected trigger, or the owning row heading if that
trigger no longer exists.

One persistent visually hidden `role="status" aria-live="polite"
aria-atomic="true"` node announces operation changes. Its pure announcement
key is `(operation_id,state,announced_milestone)`, where the milestone is a
state transition, a reset-stage transition, or probe progress first crossing
25, 50, 75, or 100 percent using exact integer arithmetic. The text composes
the mapped operation kind/state with the mapped reset stage or
`완료 <completed>/<total>`; it contains no UUID. The node is updated only when
the key differs from the last key for that operation in this document. Equal
poll responses, DOM rerenders, direct-Evidence loading, and route changes do
not reannounce it. Login announces at most the current active milestone once,
after heading focus; terminal refresh announces exactly once and polling never
clears/reinserts the live node. Visual progress still updates for every case.

Direct Evidence has a second persistent status node inside the dialog with
`role="status" aria-live="polite" aria-atomic="true"`. Its announcement key
is `(target.kind,target.id,body_state)` over exactly
`loading|typed_result|not_found|error`, so the focused
`evidence.direct_heading` is followed once by the mapped state copy; equal GET
responses and rerenders do not announce again. The node is present before the
request, never replaces heading focus, and is removed only when the dialog
closes. Browser captures assert one announcement per state, including 404,
error, and retry, without leaking raw IDs or response text.

Native semantics, keyboard order, a first-focusable `control.skip_to_content`
skip link targeting the current main h1, visible focus, 44x44 CSS-pixel
targets, label/instruction association, live-region restraint, reduced motion,
forced colors, 200% text zoom, CJK wrapping, and text+icon status redundancy are
required. The gate is WCAG 2.2 AA: normal text contrast is at least 4.5:1,
large text, component boundaries, status marks, and focus indicators at least
3:1 against adjacent colors. Axe serious/critical findings, keyboard traps,
clipped focus, and unintentional horizontal overflow are zero. In addition,
every axe violation carrying any `wcag2a|wcag2aa|wcag21a|wcag21aa|wcag22aa`
tag is zero regardless of axe impact; there is no false-positive allowlist.

WCAG reflow is exercised at native 320x800 CSS pixels and at 1280x900 with
browser zoom 400% (effective 320 CSS px): no two-dimensional scrolling, clipped
content/action/focus, or loss of information is allowed. The 1.4.12 override is
applied to every journey state with line height 1.5, paragraph spacing 2em,
letter spacing 0.12em, and word spacing 0.16em; overlap, clipping, hidden labels,
and horizontal overflow are zero. Separate complete core-journey passes run
`forced-colors:active` and `prefers-reduced-motion:reduce`; forced colors must
retain native/system-color boundaries, visible focus, and text+icon status,
while reduced motion must produce no animation/transition and no timing-
dependent state loss. These are explicit DOM/geometry/style assertions, not axe
substitutes, and each emits a bound receipt.

`packages/design-tokens` exports these exact semantic values:

```text
canvas #0B0D0E; rail #111416; panel #171A1D; raised #1D2124;
hover #262B2E; text-primary #F5F7F6; text-secondary #D0D5D2;
text-muted #AAB2AE; text-disabled #7F8883; border #6B746F;
focus #76B900; healthy #76B900; warning #FFD166; error #FF8A8A;
info #B8A1FF; revoked #C2C7C4; primary-foreground #091006;
space 4 8 12 16 24 32 48; control-radius 6px; panel-radius 8px;
type 12/16 14/20 16/24 20/28 28/36; control-min 44px
```

The sRGB contrast oracle fixes the relevant ratios: primary/secondary/muted/
disabled text on panel are 16.24/11.76/8.06/4.79, border on panel is 3.62,
focus on canvas is 8.08, and primary foreground on green is 8.00. Token drift
fails generation before browser QA.

Shadows are only `0 8px 24px rgb(0 0 0 / 0.28)` on modal/popover layers.
Status always has text plus an icon; green is reserved for focus, ready, and
primary action. Warning/error/info/revoked are not accents. No NVIDIA logo,
proprietary font, copied marketing layout, blue/teal accent, oversized chart,
glass effect, or animation is used.

Responsive geometry is deterministic. At 0..767 CSS px, a 56 px top identity
bar includes `padding-top:env(safe-area-inset-top)` and its measured content
height is `56px + env(safe-area-inset-top)`; the 64 px five-item bottom
navigation includes `padding-bottom:env(safe-area-inset-bottom)` and its
measured safe-area-adjusted height is `64px + env(safe-area-inset-bottom)`.
These insets are applied exactly once to the shell, skip target, focus ring,
and scroll viewport. They surround one column with 16 px page inset and 12 px
gutter; data tables become labeled cards. At 768..1023, a 208 px
persistent rail and 24 px page inset use an eight-column grid with 16 px gutters
and one content column except paired compact judgments. At 1024+, the rail is
224 px, page inset 32 px, and content is a 12-column/24 px-gutter grid capped at
1,120 px; prose is capped at 72ch. Dialogs at every breakpoint use
`width:min(560px,calc(100vw - 32px))` and
`max-height:calc(100dvh - 32px)`, yielding at least 16 px clearance on every
edge. The shell clips no outline; its body alone uses `overflow:auto`,
`overscroll-behavior:contain`, and 16 px scroll padding, while heading and action
footer remain visible. Opening focuses the heading, Tab remains trapped, and a
newly focused/errored body control is scrolled fully into the body viewport.
Native 1280x900 at browser zoom 200%, DPR 2, UI scale 1 produces effective 640
CSS px without emulation and therefore uses the mobile contract. Human labels
are compacted to 48 Unicode scalars in overview/rows—including Clients
credential labels—and the compact label is referenced by each row's accessible
name; full labels appear only in Evidence and destructive confirmation. The
bottom navigation wraps or scrolls within the safe-area inset without clipping
at 400% zoom.

Every modal is a native `<dialog>` opened only with `showModal()`, has an
accessible name from its unique heading via `aria-labelledby`, and sets the
non-dialog application shell `inert` until close. The pinned browser's native
top layer plus explicit inertness is the background-interaction authority; no
ARIA-only modal or hand-written Tab loop exists. Initial focus is the heading,
and native sequential focus remains inside. Backdrop clicks never dismiss.
Informational/direct-Evidence dialogs and pre-dispatch confirmations allow the
`cancel` event from Escape, clear transient fields, then use the documented
trigger focus return. Once a destructive action dispatches, its dialog is
replaced by the persistent operation surface before inertness is removed.
`dialog.external_fix` is an informational pre-dispatch confirmation: it opens
with `dialog.external_fix.title`, places focus on the consequence text before
the two labelled controls, performs no retry/account mutation on open or
Escape, and dispatches the probe only from
`control.confirm_external_action`; Escape returns focus to the exact Evidence
trigger. Its copy and focus/Escape behavior are a distinct browser case.
One-time plaintext custody and revoke-pending dialogs preventDefault on every
`cancel` event and expose no Escape/backdrop close; only verified cleanup or
verified revoke can close them. Browser gates assert accessible name,
`aria-modal`, inert background, Escape policy, focus containment, scroll, and
return focus for both classes.

### 12.3 Browser secret custody

The prerendered login shell has `document.title=title.login`, a single
`<main id="main-content"><h1 id="login-heading" tabindex="-1">` with
`heading.login`, and the first focusable `control.skip_to_content` link points
to that h1. Bootstrap failure keeps the same main target but replaces the h1
with `heading.bootstrap_failed` and `document.title=title.bootstrap_failed`;
the `<noscript>` shell uses `heading.login`/`title.login` and the exact
`bootstrap.noscript` copy. The shell initially has disabled credential input and
submit, visible loading guidance, and `control.login`. Only `onMount` after all
handlers and validators install enables them. Blocked/missing bootstrap, a
manifest JS failure, or a thrown hydration handler leaves secret submission
disabled and renders `bootstrap.failed` plus `control.retry_bootstrap`; native
form navigation and secret-bearing requests remain zero.

Every secret-bearing form has a submit listener installed before enablement;
its first synchronous statement is `event.preventDefault()` inside a wrapper
that cannot throw before that call. Validation, custody transfer, and fetch run
only afterward. The CSP/bootstrap gate injects failures before hydration, after
enablement, inside validator, and inside handler/fetch construction and proves
navigation count zero plus zero secret-bearing native/network requests.

Admin session bearer custody and one-shot mutation-secret custody are distinct.
The mounted Svelte root instance creates one private, nonreactive
`AdminSession` closure. Login reads the native admin field, immediately assigns
`field.value=""`, and creates a pre-auth `loginAttemptEpoch` plus one
AbortController before validating with `/overview`. Credential input and submit
remain disabled while that single flight is active; an explicit
`control.cancel_login` button,
`pagehide`, or document teardown increments the attempt epoch, aborts the
request, clears its bearer reference, and re-enables the empty field. A new
attempt cannot start until the prior attempt is failed, cancelled, or settled.
Only a success whose attempt epoch is still current creates the next
`sessionEpoch`; every late prior success/401/error is discarded. Logout is
unavailable pre-auth and cannot race that path. On success the closure retains
the bearer only in that closure for the current document lifetime. A full
reload requires login again. Internal navigation keeps the same mounted root.
Pages receive only an opaque `adminRequest(method,path,body)` capability whose
closure injects Authorization; there is no token getter, serialization, or
string conversion, and the bearer itself never enters a prop, rune/store,
context value, module variable, SSR payload, cookie, Web Storage, URL, dataset,
HTML interpolation, error, or log. The closure owns a monotonically increasing
`sessionEpoch` and a set of `AbortController`s. Login success creates a new
epoch. Every request captures its epoch; a response, error, or 401 may affect
state only when that epoch is still current and its signal was not aborted.
Logout and `pagehide` first increment the epoch and block dispatch, then abort
every controller, clear the single session reference and all in-flight body
references, clear rendered owner state, and return focus to the empty login
field. A same-epoch 401 atomically invalidates that epoch, aborts its siblings,
and clears owner state; later success/401 from that or an older epoch is
discarded. If one-time plaintext is present, the 401 blocks new authenticated
work but the custody surface remains until its cleanup succeeds, after which
the empty login shell appears. A prior-epoch 401 can never log out a newer
login. Document teardown
drops the closure; JavaScript strings are not claimed to be synchronously
zeroized.

There is no service worker, Web App Manifest, IndexedDB, Cache Storage,
SharedWorker, BroadcastChannel, background sync, Web Storage, or persisted
query cache. Mutation recovery is the server-side singleton intent in section
7.1. After login every tab reads `/mutation-intents/open`; a nonnull row locks
new mutation and shows its exact continue/abandon/reconcile/acknowledge action.
Each mutation prepares and reads back that server row before reading a native
secret field. Polling/visibility refresh makes a row created by another tab
visible within ten seconds, and every pre-dispatch click rechecks the singleton
under the server constraint. No cross-tab client read-modify-write or browser
lease participates in correctness.
`pagehide` clears the admin and one-shot closures and rendered owner state even
when `event.persisted` is true; a persisted `pageshow` renders the empty login
shell and requires authentication again. Back/forward cache can therefore
never resurrect an authenticated console. The browser gate exercises navigate
away/back plus a synthetic persisted lifecycle and requires zero owner data and
zero bearer-capable request until a new login.

Every upstream credential and one-time downstream plaintext instead uses one
private per-form nonreactive custody object in its page instance. Submission
order is exact: read native field -> immediately assign `field.value=""` ->
transfer the shortest-lived value to one-shot custody -> construct the request
body at fetch dispatch -> clear body/custody references in `finally`. That
`finally` never clears the separate admin session needed for later Overview,
Routing, Clients, Models, or Evidence requests. Evidence proves only that no
reachable application/DOM reference remains for one-shot values and that the
admin bearer has exactly the one documented root reference plus transient HTTP
library copies.

Before a downstream plaintext byte is placed in the textarea, the mounted root
must install a same-document history guard. Every owner entry already carries
closed nonsecret state `{version:1,route:RouteId,nav_seq:Decimal,scroll:Decimal}`.
Custody first `replaceState`s the current exact route entry if needed, then
`pushState`s one duplicate guard entry with `nav_seq+1` and the same URL/route/
scroll; this discards any preexisting forward branch before plaintext render.
The private custody object records only guard sequence and nonce. Internal link
clicks are synchronously prevented. A Back or multi-entry same-document
traversal may transiently change the browser URL before `popstate`—that event is
not cancelable—but the handler never renders/announces the reached route or
moves dialog focus. It reads the closed sequence, sets
`history_guard:"compensating"`, and calls `history.go(guard_seq-reached_seq)`
exactly once. The returning guard `popstate` restores `idle`; repeated clicks
while compensating update only the latest reached sequence and cause at most one
additional bounded compensation after return. An absent/foreign state is a
cross-document traversal: `beforeunload` can offer only the native stay prompt,
and leaving records no cleanup success. Forward from the pushed guard is a
no-op because its prior branch was discarded. Closing verified custody
`replaceState`s the guard as an ordinary current route entry and removes the
private guard state; the older duplicate is harmless normal history.

Browser assertions for Back/Forward sample after compensation settles: guard
URL/index/route/title/current-nav/dialog/focus are restored, owner route render
count and network count remain zero, and no PNG is produced. They do not make
the false claim that `popstate` prevents the browser's transient URL traversal.

One-time downstream plaintext is imperatively assigned only to one native
readonly textarea. Dismiss, logout, and internal navigation remain blocked until
field, custody, selection, and (if used) clipboard overwrite/readback are
verified clear. Clipboard custody is the closed per-nonce state
`not_observed|secret_copy_requested|secret_copy_unknown|secret_present|
overwrite_requested|overwrite_unknown|overwrite_verified|manual_required|
manual_overwrite_verified|revoke_pending|revoked`. `control.copy_secret` is a trusted click that durably changes the
local state to `secret_copy_requested` before calling
`navigator.clipboard.writeText(secret)`; fulfillment becomes `secret_present`,
while rejection, abort, page hide, or a late/unknown result becomes
`secret_copy_unknown`. A trusted native `copy` event whose Selection intersects
the plaintext textarea also sets `secret_present`, covering Ctrl+C/context-menu
copy. Synthetic events never do. The UI never reads the secret back.

`control.clear_secret` first synchronously clears textarea, Selection, request
body, and plaintext references. If state is `not_observed`, exact DOM/reference
readback completes custody without touching the clipboard. Otherwise it creates
the nonce-bound CSPRNG cleanup phrase, sets `overwrite_requested`, and from the
same trusted click calls `navigator.clipboard.writeText(cleanup_phrase)`. Only
after fulfillment does it call `navigator.clipboard.readText()`; exact phrase
equality, current session/custody epochs, cleared Selection/control, and no
plaintext reference advance to `overwrite_verified` and permit close. Write or
read rejection becomes `manual_required`; response loss, page hide, nonce/epoch
change, or a late result becomes `overwrite_unknown` and also requires manual
verification or revoke. No promise result from an older custody nonce may clear
a newer dialog. Browser tests inject failure before/after each promise
settlement and prove no false success.

Custody rendering is a total state-to-copy/action map. `not_observed` uses
`custody.not_observed` with primary `control.clear_secret`.
`secret_copy_requested` uses its matching copy and exposes no action until the
promise settles; a bounded five-second timeout changes it to
`secret_copy_unknown`, and because the original OS write may still complete
late, that state offers only `control.revoke_and_close`, never an overwrite
claim. `secret_present` uses its matching copy and primary
`control.clear_secret`. `overwrite_requested` is a no-action pending state;
`overwrite_unknown|manual_required` render the manual controls plus secondary
revoke, and an invalid paste renders `custody.manual_paste_invalid` without a
state change. `overwrite_verified|manual_overwrite_verified` show their exact
success copy and only `control.close_dialog`. `revoke_pending` shows only its
copy and automatic reconcile; there is no second revoke button. `revoked` shows
its copy and closes/focus-returns through the verified path. These are the only
visible custody combinations. A late promise from any prior state/nonce can
only make the state more conservative; it cannot return to a plaintext or
verified branch.

When Clipboard read/write permission is missing or revoked,
the same dialog shows a CSPRNG 128-bit nonsecret cleanup phrase and a native
paste-verification field. The user selects/copies that phrase with ordinary
browser/OS controls and pastes it. Success requires a trusted `copy` event
(`isTrusted=true`) whose DOM Selection is exactly the displayed phrase and a
later trusted `paste` event in the same custody nonce whose
`clipboardData.getData("text/plain")` equals it. The paste-only control rejects
keyboard text, drag/drop, autocomplete, `beforeinput` types other than
`insertFromPaste`, synthetic events, script assignment, and a copy/paste from a
different nonce. The handler prevents insertion, clears selection/control, and
only then records `manual_overwrite_verified`. This path needs neither
clipboard-read nor programmatic clipboard-write permission. If the browser
does not expose both trusted events/data, it cannot claim verification and
keeps the dialog open. Wrong or empty paste preserves the dialog and concise
recovery instruction. `beforeunload` can only offer a stay warning. If the user
forces reload/tab close or neither automatic nor manual clipboard cleanup can
complete, no cleanup-success or review approval is recorded. Screenshots are disabled whenever plaintext is
present; trace, HAR, raw network capture, and video are disabled for the whole
authenticated phase.

The custody dialog also has the safe secondary escape
`control.revoke_and_close`; it is shown once automatic cleanup fails or the
manual path is unsupported, and is always reachable by keyboard while custody
is active. Its confirmation uses `dialog.revoke_credential.title` and explains
`clipboard.revoke_escape`. On confirmation the handler synchronously clears the
textarea, selection, request-body reference, and local plaintext custody before
any await, retains only the nonsecret issued credential/intent IDs, and enters
`revoke_pending`. It then uses the section 7.1 atomic lost-secret transition,
reads back the exact revoke intent, executes that ordinary revoke once, and
reconciles/acknowledges it. The surface cannot close and screenshots remain
disabled until a fresh read proves that credential inactive, the revoke
operation succeeded, and the revoke intent was acknowledged. At that point the
possibly copied value is unusable, so clipboard overwrite proof is no longer
required; the dialog closes and focus returns to the issuance trigger or the
Clients heading. Network/401/commit ambiguity leaves the blocking
`revoke_pending` surface with no plaintext redisplay and no second issue/revoke
dispatch. A later authenticated document resumes from the one server intent
and offers only reconcile/complete-revoke. Thus an environment without trusted
clipboard events has a safe exit without falsely claiming clipboard erasure.

## 13. Cloudflare, host rollout, and Hermes

The application remains a separate independent stack. The existing
`stacks/tunnel-apps` stack only receives one ingress row for
`hostname: nvidia-lb.dongwontuna.net`, `service: http://127.0.0.1:2456`, and
`originRequest.httpHostHeader: nvidia-lb.dongwontuna.net` immediately before
the global 404; relay and Paca rows retain order and bytes. No `localhost`, IPv6
resolution, or inherited Host default is accepted for the new NVIDIA row. The app itself returns 404
for public admin/assets/unknown paths. DNS is created only after local Compose
and tunnel-config validation and is removed independently on rollback.

The infrastructure verifier is route-aware and fail-closed: it must parse the
merged tunnel YAML and assert exactly one NVIDIA row with those three literal
fields, unchanged relay/Paca rows, one final global 404, the reviewed
cloudflared digest and `pull_policy: missing`, and a documented
`/etc/nvidia-build-lb/cloudflare-dns.token` root:root 0400 source. A verifier
that checks only Compose syntax or the legacy rows is an invalid false-green
sensor. The route/DNS token inventory, stage/activate/rollback scripts, and
their focused fixture are part of the infrastructure PR before any DNS API
call; the nvidia hostname is never added with the generic
`cloudflared tunnel route dns --overwrite-dns` shortcut.

Tunnel/DNS rollout is one closed root journal at
`/opt/nvidia-build-lb/state/tunnel-rollout.json`, JCS root:root 0600. It binds
transaction UUID, staged infra commit/config/Compose hashes, exact before/target
container and mount tuples, the live prior OCI manifest/config/image IDs, an
immutable generated rollback-Compose hash, zone ID, and this exact DNS target:

```text
zone_name="dongwontuna.net"
type="CNAME"
name="nvidia-lb.dongwontuna.net"
content="685aeec4-5771-459a-8909-7ccfbb086815.cfargotunnel.com"
proxied=true
ttl=1
comment="nvidia-build-lb:<transaction UUID>"
```

```text
TunnelRollout={
 version:1,transaction_id:UUID,phase:TunnelPhase,
 infra_commit:LowerHex40,target_compose_sha256:LowerHex64,
 target_config_sha256:LowerHex64,
 maintenance:WatchtowerMaintenance,
 prior:{container_id:LowerHex64,image_ref:String,index_digest:CloudflaredOCIRef,
        platform_manifest_digest:CloudflaredOCIRef,config_digest:LowerHex64,
        restart_policy:"unless-stopped",watchtower_enable:"true",
        compose:ManagedRegularFileTuple,config:ManagedRegularFileTuple,credential:DnsRegularFileTuple,
        mounts_sha256:LowerHex64},
 target:{image_digest:CloudflaredOCIRef,compose:ManagedRegularFileTuple,config:ManagedRegularFileTuple,
         credential:DnsRegularFileTuple,mounts_sha256:LowerHex64,
         restart_policy:"unless-stopped",watchtower_enable:"false"},
 rollback:{root:DirectoryTuple,compose:ManagedRegularFileTuple,
           prior_compose_backup:ManagedRegularFileTuple,prior_config_backup:ManagedRegularFileTuple,
           image_digest:CloudflaredOCIRef,pull_policy:"never",
           restart_policy:"unless-stopped",watchtower_enable:"true"},
 dns_token:DnsTokenCustody,
 dns:{zone_id:String,record_id:String|null,state:"preflight"|
      "create_prepared"|"create_maybe_sent"|"owned"|
      "absence_confirmed"|"dns_absence_verified"|"deleted",type:"CNAME",
      name:"nvidia-lb.dongwontuna.net",
      content:"685aeec4-5771-459a-8909-7ccfbb086815.cfargotunnel.com",
      proxied:true,ttl:1,comment:String,
      create_request_sha256:LowerHex64|null,
      staged_config_sha256:LowerHex64|null,
      dispatch_authorized_at:Timestamp|null,create_attempted_at:Timestamp|null,
      observation:null|{mode:"adopt"|"delete_if_late",count:Decimal,
                        started_at:Timestamp,next_at:Timestamp,
                        foreground_deadline:Timestamp,
                        manual_receipt_after:Timestamp,
                        last_observation:null|DnsRecordObservation},
      absence_receipt:DnsAbsenceReceipt|null},
 relay_before_sha256:LowerHex64,paca_before_sha256:LowerHex64,
 failure:"live_digest_drift"|"config_invalid"|"container_tuple_changed"|
         "watchtower_tuple_changed"|"watchtower_quiesce_failed"|
         "watchtower_restore_failed"|
         "existing_route_failed"|"dns_conflict"|
         "dns_observation_failed"|"dns_absence_receipt_invalid"|
         "public_verification_failed"|"rollback_failed"|"tuple_unknown"|null
}
WatchtowerMaintenance={
 name:"watchtower-maintenance",prior:WatchtowerContainerTuple,
 target:WatchtowerContainerTuple,rollback:WatchtowerContainerTuple,
 quiesce_required:Boolean
}
WatchtowerContainerTuple={id:LowerHex64,image_ref:String,
                          config_sha256:LowerHex64,
                          restart_policy:RestartPolicy,
                          state:"running"|"stopped",
                          watchtower_enable:"true"|"false"|"absent",
                          env_sha256:LowerHex64,mounts_sha256:LowerHex64,
                          command_sha256:LowerHex64}
DnsRecordObservation={observed_at:Timestamp,zone_id:String,name:String,
                       record_count:Decimal,records_projection_sha256:LowerHex64,
                       result:"absent"|"owned_exact"|"unowned_exact"|
                              "duplicate"|"drifted"}
DnsRecordProjection is the RFC8785-JCS array of every record returned for the
exact `(zone_id,name)` lookup, sorted by `(type,name,content,proxied,ttl,id)`;
unknown fields are rejected and `records_projection_sha256` is
`SHA-256(the exact UTF-8 JCS bytes)`. An absent observation uses the empty
array and `record_count:0`, so its digest is deterministic.
DnsRegularFileTuple={kind:"regular",path:AbsolutePath,sha256:LowerHex64,
                     uid:Decimal,gid:Decimal,mode:"0400",device:Decimal,
                     inode:Decimal,size:Decimal,nlink:1}
DnsTokenReceipt={version:1,transaction_id:UUID,
                 action:"install"|"rotate"|"revoke",
                 path:"/etc/nvidia-build-lb/cloudflare-dns.token",
                 before_sha256:LowerHex64|null,target_sha256:LowerHex64|null,
                 observed_at:Timestamp,evidence_sha256:LowerHex64,
                 receipt_sha256:LowerHex64}
DnsTokenReceipt.receipt_sha256 is
`SHA-256(RFC8785_JCS(receipt with receipt_sha256 omitted))`; the evidence
digest names a separately retained, secret-free operator receipt.
`install_request_sha256` is the SHA-256 of the exact RFC8785-JCS object
`{version:1,transaction_id,path,requested_action:"install",target_sha256}`;
the request bytes contain no token value. A receipt action must match its
custody phase (`install` to `installed`, `rotate` to `rotated`, `revoke` to
`revoked`), its before/target hashes must equal the phase tuples, and its
transaction ID must equal the enclosing rollout transaction.
DnsAbsenceReceipt={version:1,transaction_id:UUID,zone_id:String,
                   name:"nvidia-lb.dongwontuna.net",
                   observed_absent_at:Timestamp,record_count:0,
                   records_projection_sha256:LowerHex64,
                   staged_config_sha256:LowerHex64,evidence_sha256:LowerHex64,
                   receipt_sha256:LowerHex64}
DnsAbsenceReceipt.receipt_sha256 uses the same omit-self JCS digest rule.
DnsTokenCustody={path:"/etc/nvidia-build-lb/cloudflare-dns.token",
                 before:DnsRegularFileTuple|null,target:DnsRegularFileTuple|null,
                 phase:"absent"|"installed"|"rotated"|"adopted"|"revoked",
                 install_request_sha256:LowerHex64|null,
                 install_receipt_sha256:LowerHex64|null,
                 rotation_receipt_sha256:LowerHex64|null,
                 revocation_receipt_sha256:LowerHex64|null,
                 prior_transaction_id:UUID|null,prior_receipt_sha256:LowerHex64|null}
DnsTokenCustody is the closed union
`absent:{before:null,target:null,install_request_sha256:null,
install_receipt_sha256:null,rotation_receipt_sha256:null,
revocation_receipt_sha256:null,prior_transaction_id:null,
prior_receipt_sha256:null}` |
`installed:{before:null,target:DnsRegularFileTuple,install_request_sha256:LowerHex64,
install_receipt_sha256:LowerHex64,rotation_receipt_sha256:null,
revocation_receipt_sha256:null,prior_transaction_id:null,
prior_receipt_sha256:null}` |
`rotated:{before:DnsRegularFileTuple,target:DnsRegularFileTuple,
install_request_sha256:LowerHex64,install_receipt_sha256:LowerHex64,
rotation_receipt_sha256:LowerHex64,revocation_receipt_sha256:null,
prior_transaction_id:null,prior_receipt_sha256:null}` |
`adopted:{before:DnsRegularFileTuple,target:DnsRegularFileTuple,
install_request_sha256:null,install_receipt_sha256:null,
rotation_receipt_sha256:null,revocation_receipt_sha256:null,
prior_transaction_id:UUID,prior_receipt_sha256:LowerHex64}` |
`revoked:{before:DnsRegularFileTuple,target:null,install_request_sha256:LowerHex64,
install_receipt_sha256:LowerHex64,rotation_receipt_sha256:LowerHex64|null,
revocation_receipt_sha256:LowerHex64,prior_transaction_id:UUID|null,
prior_receipt_sha256:LowerHex64|null}`. Every install/rotation/revocation
receipt transaction_id equals the enclosing `TunnelRollout.transaction_id`;
an `adopted` state references the prior rollout's immutable terminal receipt
and exact current regular-file tuple without rewriting the secret. A revoked
state requires a
fresh `absent` tuple observation (regular-file target absent, not a symlink)
inside the revocation receipt.
```

Its phases are
`prepared|prior_captured|watchtower_quiesce_requested|watchtower_quiesced|
target_config_verified|tunnel_recreate_requested|
tunnel_recreated|existing_routes_verified|dns_absence_verified|
dns_create_prepare_requested|dns_create_prepared|
dns_create_dispatch_requested|dns_create_maybe_sent|dns_observation_running|
dns_absence_receipt_requested|dns_absence_confirmed|dns_created|
nvidia_public_verified|committed|rollback_requested|
dns_delete_requested|dns_deleted|dns_late_delete_requested|dns_late_deleted|
prior_tunnel_restore_requested|prior_tunnel_restored|
existing_routes_reverified|watchtower_restore_requested|watchtower_restored|
rolled_back|failed_attention`.
Every requested phase precedes its side effect and stores the expected CAS
tuple; completed phases require exact API/container/config/public readback.
DNS nullability is state-closed: `preflight` has null request/time/observation/
receipt/record; `create_prepared` has a request hash but null dispatch/attempt
times, observation, and record; `create_maybe_sent` has a request hash, equal
nonnull dispatch/attempt times, and nonnull observation but null record/receipt.
Its initially persisted observation is exactly `count:0,last_observation:null`;
only a completed list readback changes those fields, and every readback stores
`DnsRecordObservation` with the all-types projection digest. `owned` has
nonnnull record ID and null absence receipt;
`absence_confirmed` has null record and nonnull manual receipt; `deleted` retains
the former owned record ID as audit and has no receipt. `rolled_back` requires
either owned-record `dns_deleted|dns_late_deleted`, a manual
`dns_absence_confirmed`, or the pre-create `dns_absence_verified` branch whose
exact all-types absence readback is bound to the staged config and has no
`dns_create_dispatch_requested` phase. That pre-create branch is a closed
no-record/no-receipt union and never makes a network create request. A
maybe-sent create can never be skipped because the prior tunnel was already
restored.

The `dns_absence_verified` phase is a separate pre-create terminal branch:
`record_id:null`, `create_request_sha256:null`,
`dispatch_authorized_at:null`, `create_attempted_at:null`,
`observation:{mode:"adopt",count:1,started_at:T, next_at:T,
foreground_deadline:T,manual_receipt_after:T,
last_observation:{observed_at:T,zone_id,name,record_count:0,
records_projection_sha256:LowerHex64,result:"absent"}}`, where every `T` is
the same readback timestamp and `zone_id/name` are the enclosing DNS target,
`absence_receipt:null`, and `staged_config_sha256` equal to the current
`target_config_sha256`; it may be reached only after the all-types empty
projection readback and can never contain a create-dispatch phase. The
`absence_confirmed` branch is post-maybe-sent and instead requires the closed
manual `DnsAbsenceReceipt` below.

DNS uses a narrowly scoped Cloudflare Zone Read + DNS Edit bearer stored only
at `/etc/nvidia-build-lb/cloudflare-dns.token`, root:root 0400 regular,
nonsymlink, nlink one. It never appears in argv, environment, journal, log, or
evidence. Bootstrap installs the token through a root-only operation that
records the request hash before activation, fsyncs/read-backs bytes, owner,
mode, inode and nlink, and stores only a secret-free receipt. Rotation writes
a same-directory temporary, fsyncs and CAS-exchanges it only when the prior
tuple is unchanged, then records old/new tuple hashes; revocation removes only
the exact current tuple after a durable request and records absence. Any token
permission, inode, nlink, owner, or hash drift stops the rollout and never
calls Cloudflare. The helper resolves exactly one zone ID for the literal zone.
Before first create, every record type at the exact name must be absent. The
POST uses the literal fields above. `dns_create_prepare_requested` durably authorizes only
writing the exact minified request bytes/hash; `dns_create_prepared` proves that
readback and still authorizes no network. Immediately before any socket call,
the helper writes `dns_create_dispatch_requested` and then
`dns_create_maybe_sent`, setting both timestamps and a planned observation with
`count:0,last_observation:null`, and fsyncs/read-backs the journal. The live process
receives one nonrenewable in-memory send permit only from that transition. A
restart can never reconstruct the permit: it treats `create_maybe_sent` as
possibly delivered even if no request byte actually left the host and never
resends the POST. Thus the conservative no-resend boundary exists before the
network, not after a timeout.

An exact success response plus exact list/readback may adopt `owned`. A definite
API rejection records attention but leaves the maybe-sent observation
authority; timeout, connection loss, invalid/missing response, or response/
readback disagreement does the same. None permits resend. A preexisting record, an unowned exact-value record,
duplicates, or field drift stops without overwrite. Journaled `record_id`
remains nullable until an exact list/readback.

Maybe-sent creation is a live recovery state, not “absent.” The first foreground
observer lists every type at the exact name after monotonic delays 1, 2, 4, 8,
16, then 30 seconds repeatedly through a five-minute deadline. Each request and
absent result advances `count,next_at,last_result` durably. Afterward the
root-owned resume timer observes every 15 minutes through 24 hours from the
attempt, then every six hours until a manual receipt closes it; concurrent
resume uses the operations lock. It never creates. If exactly one late record
matches every literal field and transaction comment, `mode:"adopt"` records its
ID and proceeds with public verification while all rollout tuples remain exact.
If rollback has begun, mode is durably changed to `delete_if_late`; the same late
owned record is deleted under requested/completed phases. Any unowned/duplicate/
drifted record is attention and is never adopted or deleted.

No new DNS-create transaction may start while this one is maybe-sent. After 24
hours of journaled absence, an operator may supply the closed canonical receipt
`{"version":1,"transaction_id":UUID,"zone_id":String,
"name":"nvidia-lb.dongwontuna.net","observed_absent_at":Timestamp,
"records_projection_sha256":LowerHex64,"staged_config_sha256":LowerHex64,
"evidence_sha256":LowerHex64,"receipt_sha256":LowerHex64}`. Its
`receipt_sha256` is `SHA-256(RFC8785_JCS(receipt with receipt_sha256 omitted))`
and its evidence digest binds a separately retained manual
Cloudflare DNS/audit view made after the attempted POST and contains no account,
token, response body, or free text. `dns_absence_receipt_requested` precedes
validation; exact transaction/zone/name/time/canonical bytes plus a still-absent
API list are required for `dns_absence_confirmed`. Only then may rollback become
terminal or a fresh transaction be considered. Without the receipt, periodic
observation continues and the rollout remains nonterminal.

Rollback DELETE is allowed only for the same owned record ID
whose live fields/comment still match; response loss is reconciled by ID/name
absence. It never deletes or overwrites a user record. Missing scoped token is
an explicit rollout gate, not permission to use a global key or guess ownership.

The currently running tunnel's observed restart policy is `unless-stopped` and
its observed `com.centurylinklabs.watchtower.enable` label is exactly `true`.
Those values are part of `prior`, not silently normalized during capture. The
currently running tunnel image is pinned in the infrastructure PR as
`cloudflare/cloudflared@sha256:4f6655284ab3d252b7f28fedb19fe6c8fc82ee5b1295c20ac74d475e5398a52d`.
`pull_policy: always`, `:latest`, and Watchtower enablement are replaced with
immutable digest, `pull_policy: missing`, and Watchtower disabled only in the
successful target (`watchtower_enable:"false"`). Route change
does not update the tunnel binary. Preflight resolves the live container's
registry index/platform manifest/config chain and requires it to equal this
reviewed pin; if Watchtower changed it after design review, rollout stops before
recreate and a new observed digest requires a new design/review snapshot. It
never silently downgrades to preserve an old pin. Tunnel identity and credential
mount remain unchanged.

The single host Watchtower controller is an independent mutable actor and is
therefore part of this journal, even though the target tunnel is Watchtower
disabled. `prior_captured` records the complete `watchtower-maintenance`
container tuple (ID, image/config/command/environment/mount digests, restart
policy, label, and state) and requires it to remain an exact CAS target. Before
any tunnel config or container side effect, a running controller advances
`watchtower_quiesce_requested`, atomically stops only that exact container ID,
and records `watchtower_quiesced` after zero Watchtower processes and an exact
stopped tuple; an already stopped controller is an explicit verified no-op.
The controller stays stopped through target recreate, relay/Paca checks, DNS
create/observation, public verification, and every rollback delete/late-record
branch. The controller's `watchtower_enable` value is an observed
`true|false|absent` label independent of the tunnel service's
`true -> false` rollout label; target and rollback keep the exact captured
controller value and never normalize it to `false`. A changed
ID/config/image/mount/restart/label tuple fails attention and
never falls back to a name-only stop. On a successful commit,
`watchtower_restore_requested` precedes starting only the journaled prior tuple
and `watchtower_restored` requires the same ID-independent fields plus the
expected prior state and zero unrelated containers. During rollback the
controller remains stopped until the complete ordered sequence `prior tunnel
restore -> relay/Paca/public/DNS rollback readback -> late-DNS closure ->
watchtower_restore_requested -> watchtower_restored`; it is never restored
before the prior tunnel is back and the public rollback proof is complete. The
target label remains `false`; the restored controller's exact prior label
remains the captured value. These requested/completed phases are reconciled
before tunnel phases after a crash, so Watchtower cannot race a force-recreate
or silently change the image between the captured and restored tuples.

Observed live authority is currently the dirty/behind worktree
`/home/dongwonttuna/Documents/Programming/home-server-infra-paca-stack` at
`1b40ddba763329d65ef1afc885b09915027504cb`, and `cloudflared-apps` bind-mounts
that worktree's config plus
`/home/dongwonttuna/.cloudflared/685aeec4-5771-459a-8909-7ccfbb086815.json`.
The rollout never edits, cleans, rebases, or treats that worktree as merged
authority. Before change it journals the live container/config/image/mount/
restart-policy/Watchtower-label tuples and copies exact compose/config bytes and
metadata to the prebound root rollback directory. `rollback.root`,
`prior_compose_backup`, and `prior_config_backup` name and hash those root-owned
copies; rollback never reads either byte sequence from the dirty worktree.

After the infrastructure PR is user-merged, the deployer verifies the merged
commit on `origin/main`, extracts only its reviewed `stacks/tunnel-apps` files
with `git archive` into
`/opt/nvidia-build-lb/infra-generations/<merged-commit>/tunnel-apps/`, and makes
that tree root:root 0755/0644 and immutable by manifest hash. The staged Compose
uses the absolute credential source above, never `${HOME}`, so sudo cannot
silently select `/root/.cloudflared`; source metadata/hash and read-only mount
are verified unchanged. It validates Compose and every ingress rule, then uses
project name `tunnel-apps` and the staged absolute compose path to force-recreate
only `cloudflared-apps`. The resulting container must bind the staged config,
same credential file, same tunnel UUID, reviewed immutable image, and
Watchtower false.

Before and after recreate, the verifier requires relay ingress/service bytes
unchanged and public `GET https://relay-ai.dongwontuna.net/health` exact 200 JSON
`{"status":"ok"}`; Paca ingress/service bytes unchanged and public
`GET https://paca.dongwontuna.net/health` 200 HTML containing exactly one
`<title>Paca</title>`; unchanged tunnel UUID/DNS answers; and the global 404.
Only then is the NVIDIA DNS route created and its public health checked. Failure
removes only that new DNS route, force-recreates `cloudflared-apps` from the
journaled prior compose/config/image bytes and same credential source, and
re-proves relay/Paca. The old dirty worktree remains byte-for-byte untouched;
the root infra generation becomes live bind authority only after successful
adoption.

The rollback Compose is generated and hashed during `prior_captured`: it uses
the journaled running platform manifest digest rather than prior `:latest`, sets
`pull_policy: never`, restores the exact prior Watchtower label `true` and
restart policy `unless-stopped`, and preserves the exact prior command, network,
mounts, credential source, root-backed-up config bytes, and container name. Rollback
first proves that immutable image/config are still local and invokes Compose
with `--pull never --force-recreate`; it never re-resolves or pulls `latest`.
If target recreate, relay/Paca proof, DNS creation, or NVIDIA public proof fails,
the phase matrix keeps Watchtower stopped, deletes only the owned DNS record
when present, restores that exact prior runtime, re-proves relay/Paca/public and
closes any late-DNS branch, and only then restores the journaled Watchtower
tuple before terminalizing.
For create-unknown it may restore/re-prove the prior tunnel immediately but
cannot terminalize rollback; it keeps `delete_if_late` observation until an
owned late record is deleted or a valid manual absence receipt closes the DNS
branch.

Named root-owned scripts implement bootstrap, status, stage, activate, backup,
restore, rollback, and uninstall; README line slices are not an operator API.
The release command verifies merged infra pins and the complete generation
tuple before staging. It never hot-patches a checked-out source tree or deploys
an unmerged defect fix.

`/run/lock/nvidia-build-lb-ops.lock` and
`/run/lock/nvidia-build-lb-hermes.lock` are created at boot by tmpfiles as
root:root 0600 regular files. They are never unlinked, replaced, or truncated.
Every opener uses `O_RDWR|O_CLOEXEC|O_NOFOLLOW`, then rejects non-regular,
uid/gid other than 0, mode other than 0600, or `st_nlink != 1` before `flock`.
The previously checked-in shell helper and its
`/opt/nvidia-build-lb/hermes-cutover-state/cutover.lock` are retired and must
not be installed or accepted by the verifier. The canonical updater wrapper is
an unsafe-free Rust parent at
`/usr/local/libexec/nvidia-build-lb-agent-apps-delayed-update`, with the exact
systemd drop-in
`/etc/systemd/system/agent-apps-delayed-update.service.d/nblb-cutover-lock.conf`.
It uses `/run/lock/nvidia-build-lb-hermes.lock`, not a state-directory lock,
and its `UpdaterRun` journal is required for every invocation. The updater
wrapper is not an `exec`-only shell. It keeps that CLOEXEC descriptor and `flock` in the parent, spawns the
updater child without the descriptor, forwards TERM/INT/HUP, waits/reaps, and
records the child's exact exit/signal status before release. Thus CLOEXEC
prevents inheritance without dropping mutual exclusion during child runtime.
The steady-state global host order is operations lock then Hermes lock. During
the one legacy-authority transition only, the exact order is operations lock,
legacy `runtime.env.lock`, then Hermes lock; no steady-state v3 path opens the
legacy lock. Release, backup, restore, rollback, and uninstall always take the
operations lock; when a phase also pauses/changes Hermes it then takes the
Hermes lock. Cutover/reapply take both in that order. The delayed updater takes
only the Hermes lock and can therefore never invert the order.

Every wrapper invocation that passes the prior-run gate has a root-owned
permanent journal at
`/opt/nvidia-build-lb/state/updater-runs/<run-uuid>.json`; terminal runs are
never rewritten or automatically removed. `INVOCATION_ID`, `/proc/sys/kernel/random/
boot_id`, the effective wrapper/drop-in/unit hashes, and the child executable
tuple are captured without environment values. Its closed schema is:

```text
UpdaterRun={
 version:1,run_id:UUID,phase:UpdaterRunPhase,
 invocation_id:LowerHex32,boot_id:UUID,started_at:Timestamp,
 wrapper_runtime:{pid:Decimal,start_ticks:Decimal,boot_id:UUID},
 ended_at:Timestamp|null,
 wrapper:PathFileTuple,lock_dropin:PathFileTuple,unit_sha256:LowerHex64,
 child_command:{path:"/opt/agent-apps/bin/check-delayed-updates",
                file:PathFileTuple,argv_sha256:LowerHex64},
 before:UpdaterRuntimeTuple,
 child:
  {state:"planned",pid:null,start_ticks:null,terminal:null}
 |{state:"spawn_requested",pid:null,start_ticks:null,terminal:null}
 |{state:"spawned",pid:Decimal,start_ticks:Decimal,terminal:null}
 |{state:"terminal",pid:Decimal,start_ticks:Decimal,
   terminal:{kind:"exit",code:0..255}|{kind:"signal",number:1..64},
   observed_at:Timestamp}
 |{state:"abandoned",prior_state:"spawn_requested"|"spawned",
   pid:Decimal|null,start_ticks:Decimal|null,terminal:null,
   quiesced_at:Timestamp,cgroup_receipt:CgroupQuiesceReceipt},
 after_child:UpdaterRuntimeTuple|null,
 drift:UpdaterDrift|null,
 reconcile:{state:"planned"|"start_requested"|"started"|"verified",
            hermes_transaction_id:UUID|null,
            e2e_receipt_sha256:LowerHex64|null},
 recovery:UpdaterRecovery,
 failure:"tuple_changed"|"child_spawn_unknown"|
         "child_terminal_unknown"|"post_child_unknown"|
         "binding_invalid"|"reapply_failed"|"e2e_failed"|
         "recovery_claim_failed"|"orphan_quiesce_failed"|
         "tuple_unknown"|null
}
UpdaterRuntimeTuple={
 active_generation_id:UUID,active_pointer_sha256:LowerHex64,
 active_json_sha256:LowerHex64,credential_id:UUID,
 credential_digest:LowerHex64,compose:PathFileTuple,
 agent_apps_env:PathFileTuple,image_lock:PathFileTuple|null,
 hermes_env:PathFileTuple,hermes_config:PathFileTuple,
 compose_projection_sha256:LowerHex64,
 agent_apps_env_nonsecret_projection_sha256:LowerHex64,
 containers:[UpdaterContainerTuple],
 hermes:{id:LowerHex64,image:String,config_sha256:LowerHex64,
         restart_policy:RestartPolicy,state:"running"|"stopped"}
}
UpdaterContainerTuple={service:String,id:LowerHex64,image:String,
 config_sha256:LowerHex64,restart_policy:RestartPolicy,
 state:"running"|"stopped"}
UpdaterRecovery=
 {state:"none",transaction_id:null,recovery_boot_id:null}
|{state:"claim_requested"|"claimed"|"service_stop_requested"|
        "service_stopped"|"reconciled",
  transaction_id:UUID,recovery_boot_id:UUID}
UpdaterDrift=
 {kind:"exact_before"}
|{kind:"reviewed_image_update",changed_keys:[
    "N8N_IMAGE"|"N8N_RUNNERS_IMAGE"|"POSTGRES_IMAGE"|
    "OPENCLAW_IMAGE"|"HERMES_IMAGE"],
  child_output_projection_sha256:LowerHex64}
|{kind:"unknown",reason:"active_generation"|"active_binding"|
    "hermes_live_file"|"compose_structure"|"agent_env_structure"|
    "image_lock_structure"|"container_set"|"unowned_file"|
    "noncausal_change"}
UpdaterRunPhase="prepared"|"before_verified"|
 "hermes_lock_requested"|"hermes_lock_acquired"|
 "child_spawn_requested"|"child_spawned"|"child_terminal"|
 "after_child_captured"|"hermes_lock_release_requested"|
 "hermes_lock_released"|"reconcile_start_requested"|
 "reconcile_started"|"binding_verified"|"committed"|
 "recovery_claim_requested"|"recovery_claimed"|
 "orphan_service_stop_requested"|"orphan_service_stopped"|
 "recovery_after_child_captured"|"recovery_reconcile_requested"|
 "recovery_binding_verified"|"abandoned_committed"|"failed_attention"
```

Only `committed` and `abandoned_committed` are terminal journal phases.
`failed_attention` is deliberately nonterminal: it inhibits the timer and all
new wrapper runs until the same run is either recovered to
`abandoned_committed` or reconciled forward to `committed`; it is not a third
success/failure terminal. `ended_at` is nonnull only in those two terminal
phases. Normal execution keeps `recovery:none`; the other recovery branch has
nonnull IDs from `claim_requested` onward and never returns to `none`.
`after_child` and `drift` become nonnull together after the exact child or
orphan cgroup is quiescent; neither phase nor a missing PID substitutes for
that tuple.

`containers` contains every service in the effective `agent-apps` Compose
model in unsigned UTF-8 service-name order, not only Hermes. The updater-owned
`.env` projection parses duplicate-free assignments and exposes only key names,
line ordering, value hashes, and exact image-reference structure; it never puts
values in the journal. `changed_keys` is the unique subset in the fixed order
shown in the type. A `reviewed_image_update` is valid only when the child-owned
before/after diff changes those assignment values and their matching
`image-lock.json` rows, leaves all other `.env` bytes/Compose structure/Hermes
live files/active binding unchanged, and every changed container image equals
the newly hash-bound assignment. Added/removed services, comments or unrelated
assignments, a changed Hermes credential/config byte, an unowned inode, or a
runtime change not causally bracketed by this InvocationID is `unknown`.

Before allocating a run UUID or writing an `UpdaterRun`, the wrapper scans and
locks the run index. If an earlier journal is nonterminal, it emits
`prior_incomplete_run`, returns 75, and writes only the terminal O_EXCL JCS
receipt
`/opt/nvidia-build-lb/state/updater-rejections/<INVOCATION_ID>.json` containing
version, InvocationID, boot ID, observed time, prior run UUID/journal hash, and
`reason:"prior_incomplete_run"`. It has no run UUID/phase/child/runtime tuple,
is not an UpdaterRun, and can never count toward the sole-nonterminal query.
An exact duplicate InvocationID adopts the same receipt; a mismatch is
attention. Thus repeated timer wakeups cannot manufacture a second recovery
owner. It never infers that an absent PID means the original child did not run.
Only after a clean index does it durably create `prepared`.
`child_spawn_requested` is durable before `spawn`. A crash there,
after spawn, before wait receipt, or across a boot-ID change is permanently
no-resend/fail-closed: the original run requires operator reconciliation and no
later timer invocation executes the updater child. PID plus `/proc/<pid>/stat`
start ticks and boot ID prevent PID adoption. Only the live wrapper that reaps
the exact child may write `child_terminal`.

Recovery is the explicit root command
`nblb-ops recover-updater-run <run-uuid>`, never another timer run. It requires
the exact sole nonterminal journal, proves the original wrapper PID/start ticks
absent or a different boot ID, writes a fresh recovery transaction/boot ID at
`recovery_claim_requested`, then acquires operations followed by Hermes. Before
interpreting any post-child state it writes `orphan_service_stop_requested`,
stops the exact `agent-apps-delayed-update.service` InvocationID/cgroup, and
requires MainPID/ControlPID plus every cgroup PID zero at
`orphan_service_stopped`. This handles a child that outlived its non-inheriting
wrapper lock. It never writes an exit/signal receipt it did not reap and never
spawns the child.

The recovery captures the complete post-quiesce runtime tuple, changes the
child union to truthful `abandoned`, then runs the same binding verification or
reviewed reconciliation plus fresh E2E while holding locks in global order. Only
`recovery_binding_verified` may advance to terminal `abandoned_committed`.
That terminal authorizes a later new systemd invocation, but does not make the
abandoned updater action successful; hold-release catch-up still requires a
new exact zero-exit committed UpdaterRun. Unknown cgroup/runtime/file tuples
remain failed attention with the timer held. Fault tests kill the wrapper before
spawn, after spawn, during child mutation, after host reboot, and during
recovery, and prove no child resend plus eventual binding-safe closure.

After child terminal, while still holding only Hermes, the wrapper captures the
complete post-child tuple, then durably requests/releases Hermes. It invokes the
reconciler by run ID; that separate process takes operations then Hermes in the
global order, so no lock inversion exists. It accepts only the exact before
tuple or a `reviewed_image_update` causally produced by this child. It
revalidates generation, `active.json`, live token digest/scopes, both Hermes
files, updater `.env`/image lock, Compose, every container, and health. The exact
before branch performs no file mutation. The reviewed image-update branch
adopts the allowed updater files/containers and, when Hermes changed or
restarted, starts the exact current container if needed and performs a fresh
binding/health/E2E verification; because the Hermes credential/config files
must be unchanged, it does not rotate a token merely for an image update. A
causally reviewed container that is healthy but whose live binding files differ
is not auto-repaired. Every `UpdaterDrift::unknown` records the closed reason,
stops before overwrite/restart/reapply, leaves the timer held, and requires
operator evidence; a later separately journaled Hermes `reapply` is allowed
only after the unknown tuple is explicitly resolved to an exact accepted
source. The updater reconciler never treats “a reapply could probably fix it”
as ownership of user drift. It then runs a fresh durable Hermes E2E and records
its receipt hash before `binding_verified`.
Normal timer execution therefore cannot report success merely because the
update child exited zero.

`committed` records both child outcome and restored binding. If reconciliation
fails, the service returns 75 regardless of child status and remains attention.
If reconciliation succeeds, a zero child returns zero, an exit child returns
that exact nonzero code, and a signalled child is re-raised as the same signal
after the journal commit. The hold-release catch-up accepts only a committed run
whose child is exact exit zero and whose binding/E2E receipt matches its
InvocationID. Routine timer runs use the same gate, including after reboot or a
container image replacement.

### 13.1 Hermes interlock and exact paths

Current Hermes truth is container `agent-hermes`, Compose
`/opt/agent-apps/compose.yml`, live files
`/opt/agent-apps/data/hermes/.env` and
`/opt/agent-apps/data/hermes/config.yaml`, and delayed updater unit
`agent-apps-delayed-update.service/timer`. Existing files, owners, modes, data,
sessions, and hidden backup directories are preserved.

Cutover state is `/opt/nvidia-build-lb/hermes-cutover-state/active.json`, journal
is `journal.json`, and backups are
`/opt/nvidia-build-lb/hermes-cutover-backups/<transaction-uuid>/`; parents are
root:root 0700 and files 0600. The Hermes-specific second lock is
`/run/lock/nvidia-build-lb-hermes.lock`; the operations lock remains first.
Under both locks, the root-only journal
records each live file's complete-byte SHA-256 plus uid, gid, mode, device,
inode, size, and nlink before and after change. Secret-redacted projection hashes
are derived only for external evidence and never control CAS.

`active.json` contains monotonically increasing `epoch`, active downstream
credential ID, digest version/lowercase-hex 32-byte digest, `bound_generation_id`,
provider/model, and both exact live-file tuples. Its exact schema is:

```text
{"version":1,"epoch":Decimal,"bound_generation_id":UUID,
 "credential_id":UUID,"digest_version":"downstream_v2",
 "credential_digest":LowerHex64,"provider":"nvidia",
 "model":"z-ai/glm-5.2","env":FileTuple,"config":FileTuple}
FileTuple={"sha256":LowerHex64,"uid":Decimal,"gid":Decimal,"mode":Octal4,
           "device":Decimal,"inode":Decimal,"size":Decimal,"nlink":1}
```

`Octal4` is a four-character lowercase octal mode such as `0600`.
It is root-only and its digest never enters external evidence. The journal is a
closed `HermesCutoverJournal`; no phase name stands in for an unrecorded admin,
file, task, or CAS subtransaction:

```text
HermesCutoverJournal={
 version:1,transaction_uuid:UUID,kind:"initial_cutover"|"reapply",
 phase:HermesCutoverPhase,target_epoch:Decimal,
 bound_generation_id:UUID,
 issue:AdminMutationTxn,
 superseded_revoke:AdminMutationTxn|null,
 new_token_revoke:AdminMutationTxn|null,
 new_credential_id:UUID|null,superseded_credential_id:UUID|null,
 env:HermesFileTxn,config:HermesFileTxn,
 active_cas:HermesActiveCas,
 boot_epochs:[HermesBootEpoch],
 e2e:HermesE2ETxn,rollback_e2e:HermesE2ETxn|null,
 recovery_e2e:[HermesE2ETxn],
 before_container:HermesContainerTuple,target_container:HermesTargetContainer,
 failure:"admin_mutation_unknown"|"plaintext_lost"|"file_tuple_unknown"|
         "container_tuple_unknown"|"e2e_dispatch_unknown"|
         "e2e_receipt_missing"|"active_cas_unknown"|
         "revoke_failed"|"rollback_failed"|"tuple_unknown"|null
}
AdminMutationTxn={
 role:"issue"|"superseded_revoke"|"new_token_revoke",
 state:"planned"|"prepare_requested"|"prepared_readback"|
       "execute_requested"|"reconcile_requested"|"terminal_readback"|
       "ack_requested"|"acknowledged",
 intent_id:UUID,operation_id:UUID,
 request:{method:String,path:String,body_base64url:String,
          body_sha256:LowerHex64,operation_id:UUID,
          expected_configuration_generation:Decimal},
 fingerprint:LowerHex64,
 terminal:null|{outcome:"succeeded"|"rejected_stable",
                operation_id:UUID,resource_id:UUID|null},
 acknowledged_at:Timestamp|null
}
HermesFileTxn=
 {live_path:AbsolutePath,staged_path:AbsolutePath,backup_path:AbsolutePath,
  before:FileTuple,target_expected:null,target:null,state:"planned"}
|{live_path:AbsolutePath,staged_path:AbsolutePath,backup_path:AbsolutePath,
  before:FileTuple,target_expected:ExpectedFileTuple,target:null,
  state:"sealed"|"stage_requested"}
|{live_path:AbsolutePath,staged_path:AbsolutePath,backup_path:AbsolutePath,
  before:FileTuple,target_expected:ExpectedFileTuple,target:FileTuple,
  state:"staged"|"exchange_requested"|"exchanged"|
        "backup_move_requested"|"installed"|
        "restore_exchange_requested"|"restored_exchanged"|
        "restore_backup_move_requested"|"restored"}
ExpectedFileTuple={sha256:LowerHex64,uid:Decimal,gid:Decimal,mode:Octal4,
                   size:Decimal,nlink:1}
HermesActiveCas=
 {path:"/opt/nvidia-build-lb/hermes-cutover-state/active.json",
  before_bytes_base64url:String|null,before_sha256:LowerHex64|null,
  target_bytes_base64url:null,target_sha256:null,
  rollback_tombstone_path:AbsolutePath,rollback_tombstone:null,
  state:"planned"}
|{path:"/opt/nvidia-build-lb/hermes-cutover-state/active.json",
  before_bytes_base64url:String|null,before_sha256:LowerHex64|null,
  target_bytes_base64url:String,target_sha256:LowerHex64,
  rollback_tombstone_path:AbsolutePath,rollback_tombstone:FileTuple|null,
  state:"sealed"|"swap_requested"|"swapped"|
        "restore_requested"|"restored"}
HermesBootEpoch={ordinal:Decimal,boot_id:UUID,first_observed_at:Timestamp}
HermesE2ESupersession={
 prior_id:UUID,prior_for_phase:"cutover"|"rollback"|"hold_release",
 prior_boot_ordinal:Decimal,prior_projection_sha256:LowerHex64,
 reason:"boot_changed"|"container_replaced"|"post_updater",
 receipt_sha256:LowerHex64
}
HermesE2ETxn={
 id:UUID,ordinal:Decimal,boot_ordinal:Decimal,
 kind:"cutover"|"rollback"|"hold_release"|"post_reboot"|"post_updater",
 for_phase:"cutover"|"rollback"|"hold_release",
 supersedes:HermesE2ESupersession|null,
 state:"planned"|"dispatch_requested"|"dispatch_unknown"|
       "task_identified"|"events_streaming"|"terminal"|"verified",
 request_sha256:LowerHex64,dispatch_authorized_at:Timestamp|null,
 boot_id:UUID,container_id:LowerHex64|null,run_id:null|HermesRunId,
 health_receipt_sha256:LowerHex64|null,
 nonstream_receipt_sha256:LowerHex64|null,
 sse_receipt_sha256:LowerHex64|null,
 tool_started_receipt_sha256:LowerHex64|null,
 tool_completed_receipt_sha256:LowerHex64|null,
 terminal_receipt_sha256:LowerHex64|null,
 terminal_status:null|"completed"|"failed"|"cancelled"
}
HermesRunId="run_" + 32LowerHex
HermesContainerTuple={id:LowerHex64,image:String,config_sha256:LowerHex64,
 restart_policy:RestartPolicy,state:"running"|"stopped"}
HermesTargetContainer=
 {state:"planned",expected:null,tuple:null}
|{state:"sealed",expected:{image:String,config_sha256:LowerHex64,
                           restart_policy:RestartPolicy,state:"running"},
  tuple:null}
|{state:"observed",expected:{image:String,config_sha256:LowerHex64,
                             restart_policy:RestartPolicy,state:"running"},
  tuple:HermesContainerTuple}
```

Every `AdminMutationTxn.request` is the exact persisted nonsecret request
record; decoded bytes must reproduce section 7's fingerprint and its nested
operation ID. The exact top-level phase enum is:

```text
prepared|
token_issue_prepare_requested|token_issue_prepared|
token_issue_execute_requested|token_issue_reconciling|token_issued|
target_stage_requested|target_staged|
token_issue_ack_requested|token_issue_acknowledged|
env_exchange_requested|env_exchanged|env_backup_move_requested|env_installed|
config_exchange_requested|config_exchanged|config_backup_move_requested|config_installed|
hermes_restart_requested|hermes_restarted|
e2e_dispatch_requested|e2e_task_identified|e2e_verified|
active_swap_requested|active_swapped|
superseded_revoke_prepare_requested|superseded_revoke_prepared|
superseded_revoke_execute_requested|superseded_revoke_reconciling|
superseded_revoked|superseded_revoke_ack_requested|
superseded_revoke_acknowledged|committed|
rollback_started|rollback_stop_requested|rollback_stopped|
active_restore_requested|active_restored|
config_restore_exchange_requested|config_restored_exchanged|
config_restore_backup_move_requested|config_restored|
env_restore_exchange_requested|env_restored_exchanged|
env_restore_backup_move_requested|env_restored|
rollback_restart_requested|rollback_restarted|
rollback_e2e_dispatch_requested|rollback_e2e_task_identified|rollback_verified|
new_token_revoke_prepare_requested|new_token_revoke_prepared|
new_token_revoke_execute_requested|new_token_revoke_reconciling|
new_token_revoked|new_token_revoke_ack_requested|
new_token_revoke_acknowledged|rollback_keep_stopped_verified|
rolled_back|rolled_back_no_serve|failed_attention
```

Initial cutover has null superseded revoke/rollback E2E until required and skips
the superseded phases. Reapply has a distinct nonnull superseded transaction.
At `prepared`, `boot_epochs` has exactly ordinal zero with the current boot ID,
the base `e2e` has ordinal zero, that boot ordinal, `for_phase:"cutover"`, and
null supersedes; `recovery_e2e` is empty. A created `rollback_e2e` is the base
ordinal-zero row of the rollback lineage, uses the then-current boot ordinal,
`for_phase:"rollback"`, and null supersedes. No epoch, intent, operation, E2E,
or revoke ID is reused across transactions.

The HoldReleaseJournal's base E2E is the same closed shape with
`kind:"hold_release"`, `for_phase:"hold_release"`, ordinal zero, the current
boot ordinal, and `supersedes:null`; its required health/nonstream/SSE/tool and
terminal receipts are bound before `hermes_binding_verified`. Any reboot or
updater replacement creates only a contiguous `hold_release` recovery row and
supersedes the immediately prior row with the same rules as cutover and
rollback. A cutover/rollback row cannot be reused to satisfy hold release.

The initial `prepared` row deliberately knows no value derived from an
unissued credential. `new_credential_id` is null, both file transactions are
`planned` with null expected/observed targets, and `active_cas` is the planned
branch with null target bytes/hash. For reapply only,
`superseded_credential_id` and the nonnull active-CAS before bytes/hash are
known; initial cutover has both null. `token_issued` requires the issue
terminal's exact nonnull resource ID to be copied once into
`new_credential_id`, but still leaves every target field null. While the
one-time plaintext is in custody, the helper constructs both complete target
byte sequences in bounded memory, persists only each `ExpectedFileTuple`, and
changes both nested rows to `sealed` before any staged-file creation. Expected
tuples intentionally omit device/inode because those do not exist yet.
`target_stage_requested` then advances each file independently through
`stage_requested|staged`; a staged row records the observed full `FileTuple`
and must equal its expected hash/uid/gid/mode/size/nlink on the required
filesystem. Only when both are `staged` may the helper build the exact
`active.json` target from the observed tuples, set `active_cas:sealed`, and
write top-level `target_staged`. Thus a phase never asserts a future file or
credential value. `target_container` likewise remains planned through file
installation. After exact effective Compose/target-file readback and before
`hermes_restart_requested`, it becomes `sealed` with the expected image/config/
restart-policy/running tuple but no invented container ID. Only
`hermes_restarted` may store an observed tuple, whose non-ID fields must equal
that expectation. A restart-requested phase with either no container or one
exact matching observed container is reconciled; no future container ID is
predeclared.

A crash with a nonnull issued ID but without two exact staged targets is the
closed lost-plaintext branch: it never recreates either target from guesses,
never asks issuance to repeat, and runs the recorded new-token revoke. An exact
partially staged path is retained as transaction-owned evidence until revoke
is acknowledged and may then be quarantined by its journaled cleanup; a
different path tuple is `file_tuple_unknown`. Recovery may continue installation
only from two exact staged files plus a sealed active CAS. Once the CAS is
swapped, an initial-cutover rollback moves the exact target to the prebound
tombstone and fills `rollback_tombstone`; reapply restores only its exact
nonnull before bytes. The planned and sealed union branches are validated in
Rust, generated TypeScript, JSON Schema, and every crash fixture.

Token issue uses label `Hermes epoch <target-decimal>` and scopes in exact order
`["models:read","chat:write"]`. For issue and each possible revoke, the helper
first writes `prepare_requested`, POSTs the exact section 7.1 intent, GETs and
byte-compares `prepared_readback`, then writes `execute_requested` before the
actual mutation request. Any response loss or process restart goes first to
`reconcile_requested`: it GETs the exact intent and operation and obeys only
prepared, executing, or terminal readback. An exact still-prepared intent proves
the mutation transaction never won and permits the same body once through the
idempotent endpoint; executing/terminal is never resent. Stable rejection is
terminal. A successful terminal is read back before `ack_requested`, and a lost
ack response is reconciled by GET until `acknowledged`. A 404 after prepared
readback, another UUID/path/fingerprint, or a mixed acknowledgement is
`admin_mutation_unknown`, never permission to create a replacement request.

Issue acknowledgement occurs only after the plaintext has been cleared from
HTTP/body memory and the exact fsynced staged target owns custody. If the 201
body is lost but reconciliation proves issuance, the operation's
`secret_available:false` and issued ID are recorded. The helper uses the exact
atomic lost-secret transition from section 7.1 to acknowledge issue and prepare
`new_token_revoke`, then runs that revoke's full prepare/readback/execute/
reconcile/terminal/ack sequence and rolls back without touching live files.
Stale generation requires a new outer transaction/epoch only after the old
intent is terminal and acknowledged. Issue/revoke never invents body bytes or
skips a readback phase during recovery.

The editor requires exactly one LF-terminated `.env` assignment named
`NVIDIA_API_KEY` and replaces only its value; missing/duplicate/malformed/NUL
input fails. Despite its legacy name, that value is the scoped nblb downstream
token, never an NVIDIA hosted key. YAML is duplicate-key/alias rejecting and
requires exactly one top-level `model` mapping with scalar keys `default`,
`provider`, and `base_url`; a span editor replaces only those scalar tokens with
`z-ai/glm-5.2`, `nvidia`, and `http://127.0.0.1:2456/v1`, preserving every other
byte/comment/order. Missing/duplicate/non-scalar targets fail.

Hermes cutover uses only the v3 owner API
`/admin/api/v1/downstream-credentials` (POST issue and POST
`/<credential-id>/revoke`, with operation/generation idempotency). The retired
`/downstream-tokens` GET/POST/DELETE helper surface is not a compatibility
fallback; finding it in the production helper fails the cutover contract.

Once plaintext is received, the helper builds and fsyncs complete target files
in hidden same-filesystem paths, with each live file's observed uid/gid/mode
(currently `.env` 1000:1000 0600 and config 1000:1000 0640) and nlink one,
only after `target_stage_requested`; exact tuple readback advances
`target_staged`. This is the only crash-recovery copy of the new token and is
never external evidence. Issue acknowledgement and in-memory plaintext cleanup
must complete before a live-file exchange begins.

Preflight requires live parents and backup directory on one filesystem. Install
uses Linux `renameat2(RENAME_EXCHANGE)` through the safe `rustix` API (no local
unsafe): exchange live with staged target, fsync both parents, move the now-
backup original path into the transaction directory without changing inode, and
read back the exact target tuple before its phase. For each file, the
top-level `*_exchange_requested` and nested `exchange_requested` are durable
before `RENAME_EXCHANGE`; `*_exchanged` requires both path tuples. Then
`*_backup_move_requested` precedes moving the exchanged original to its bound
backup path, and `*_installed` requires live/backup/parent readback. Env and
config run in that order. Rollback uses the symmetric
`*_restore_exchange_requested|*_restored_exchanged|
*_restore_backup_move_requested|*_restored` states, exchanges each current live
file with its journaled original backup, restores the exact original
inode/bytes/uid/gid/mode/nlink, and moves the replaced target to its retained
path. An unsupported filesystem/exchange or
device mismatch fails before token issue. General rename is never used for live
install/restore, so inode is a valid CAS field.

If recovery finds `token_issued` but no exact fsynced target tuple, the
plaintext is treated as lost: it revokes the recorded new credential and rolls
back. It never asks issuance to reproduce the token. If `target_staged` is
durable, recovery may continue from those exact bytes without extracting or
displaying the token.

After a crash, only the journaled complete before tuple, complete target tuple,
or target-env/before-config intermediate tuple is recognized. The immutable
kind/epoch selects initial versus reapply recovery; file contents never infer
mode. Recovery completes or rolls back according to phase using the staged
target bytes; any other full-byte/metadata tuple stops at `failed_attention`
without overwrite.

Hermes currently generates `run_<32-lowerhex>` only in the 202 response to
`POST /v1/runs`; it accepts no client-selected run ID or idempotency key. The
design therefore does not pretend a lost POST response is replayable.
`e2e_dispatch_requested` durably stores the request hash and authorization time
before that single POST. If a valid 202 is received, its exact run ID is
fsynced in `e2e_task_identified` before opening
`/v1/runs/<id>/events` or polling `/v1/runs/<id>`. If the response/run ID is
lost—including a crash between provider response and journal write—the E2E
enters `dispatch_unknown`, is never sent again, stops the exact target under a
requested/completed phase, and forces rollback/attention. A later E2E is a new
transaction only after that container stop proves no old run can continue; it
is never claimed as recovery of the unknown task.

For an identified run, the helper journals secret-free canonical receipts for
health, Korean nonstream, SSE terminal `[DONE]`, `tool.started`,
`tool.completed` with `error:false`, and terminal run `completed`. Each task
event must carry the exact run ID; tool complete must follow tool start, and the
terminal GET plus SSE terminal must agree. A helper/event-stream crash or a
Hermes restart that loses either tool receipt is failure, not a reconstructed
success. Prompt, tool arguments/output, model response, bearer, and event body
are never retained—only closed projection hashes. `e2e_verified` requires all
six hashes, both exact files, effective Compose, generation binding, container
health, and the known run ID.

Only then does `active_swap_requested` durably authorize the exact
`before -> target` `active.json` CAS. `active_swapped` requires target-byte and
file-tuple readback. Initial absence uses `RENAME_NOREPLACE`; replacement uses
`RENAME_EXCHANGE`, and rollback uses `active_restore_requested|active_restored`
plus the bound tombstone rather than unchecked unlink/overwrite. Reapply may
request revocation of its superseded token
only after `active_swapped`; a crash there resumes that exact revoke and never
revokes the new credential. Once superseded revocation succeeds, recovery only
finishes commit, never rolls back to a revoked credential. A pre-swap rollback
restores both original bytes/metadata and revokes the new token through its
recorded operation. For `reapply`, and only when `active.json` proves the
restored token is a still-active nblb downstream credential bound to the active
generation, it then restarts/verifies Hermes before `rolled_back`. For
`initial_cutover`, the originals may contain the exposed/revoked NVIDIA value:
they are restored only as inode/byte evidence, Hermes remains stopped with
restart policy `no`, no health/provider call occurs, and the terminal is
`rolled_back_no_serve`. The journal binds exact before/target container ID,
image/config/restart-policy/running-state tuples and adds phases
`rollback_keep_stopped_verified|rolled_back_no_serve`; every phase/runtime
combination outside these two branches is `failed_attention`. Legacy-imported
tokens are already inactive. Unknown tuples always stop.

For a reapply rollback, `rollback_restart_requested` precedes starting only the
journaled before container/Compose tuple; `rollback_restarted` requires its
exact ID/image/config/restart-policy, restored binding, and health. It then runs
a distinct `rollback_e2e` through the same dispatch/task/tool/terminal no-replay
contract, and only that fresh receipt permits `rollback_verified`. Initial
cutover instead reaches `rollback_keep_stopped_verified` only with the exact
container stopped, restart policy `no`, both legacy credentials revoked, and no
E2E dispatch.

After any host reboot, boot-ID readback invalidates a pre-reboot health or E2E
receipt. Before any other recovery side effect, the journal appends exactly one
new `boot_epochs` row with the next contiguous ordinal and the newly observed
boot ID; an existing boot ID can never be appended again. Recovery reconciles
files, active CAS, token rows, and the journal phase first. If that phase
requires a running Hermes, it durably records the applicable restart request
and starts the exact journaled target—not a newly resolved image or Compose
default—then appends a new `recovery_e2e` row with the next global contiguous
ordinal starting at one, that latest boot ordinal, `kind:"post_reboot"`, and
`for_phase` equal to the current forward or rollback lineage. Its nonnull
`supersedes` names the immediately prior effective E2E in that same lineage and
hashes its complete last durable projection. The canonical supersession receipt
binds both boot epochs, both E2E IDs, the current journal phase, the exact prior
projection, the reason, and the restarted container tuple; it never rewrites or
pretends the prior receipt passed. A same-boot updater/container replacement
uses another contiguous recovery row with `kind:"post_updater"`, the same boot
ordinal, and the same supersession rule. If the phase requires stopped Hermes it
proves it remains stopped and appends no E2E row.

Only the greatest-ordinal effective E2E for the current `for_phase` may advance
or justify a top-level E2E phase. Every earlier effective row must be named by
the next row's exact supersession receipt, recovery ordinals may not gap or
branch, and a row may supersede only the immediately prior row of its lineage.
Any phase requiring running Hermes, including `committed`, `rollback_verified`,
and hold/updater release handoff, requires that latest row to be `verified` with
the greatest `boot_epochs.ordinal`, current boot ID, current container ID, and
all six receipts. Thus two or more reboots remain representable and neither
cutover/rollback nor hold/updater release can become terminal from an E2E
receipt whose boot ID predates the latest start.

Initial/reapply begins only when the active generation pointer matches
`active.json.bound_generation_id` (or active.json is absent for initial), and
the target DB is the mutation authority. Reapply writes the current generation
into its target active state. Generation release follows section 10.3: it
state-forwards the credential row, verifies the live token digest/scopes, and
CAS-rebinds only the generation field while Hermes is paused. A historical DB
with a missing/revoked/superseded credential can never be selected to satisfy
Hermes.

The shared updater wrapper is installed while the section 1.1 hold remains
active. The separate hold-release journal in section 1.1 makes recovery exact,
and steps 2 onward cannot cross `hermes_binding_verified`:

1. keep the timer disabled/stopped, both persistent condition drop-ins
   installed, and the unblock path absent; bind the current boot's
   `HoldBootInhibitionJournal` hash and require it to have rematerialized and
   verified both runtime masks;
2. install root-owned 0755
   `/usr/local/libexec/nvidia-build-lb-agent-apps-delayed-update` and 0644
   drop-in `/etc/systemd/system/agent-apps-delayed-update.service.d/nblb-cutover-lock.conf`;
3. daemon-reload, verify the bound current-boot child hash again, remove only
   the runtime mask for the service while its
   persistent condition still blocks execution, and verify effective
   `ExecStart` is exactly the wrapper parent which takes the Hermes lock, spawns
   `/opt/agent-apps/bin/check-delayed-updates --apply`, waits while retaining
   the lock, forwards signals, and returns its status;
4. remove only the service hold drop-in, perform its independently journaled
   daemon-reload, and, if the hold captured an active service, persist the exact
   handoff tuple and run the nested catchup-attempt loop: each ordinal releases
   Hermes then operations, starts the service exactly once through its verified
   wrapper, waits for the bound InvocationID/UpdaterRun, then reacquires
   operations followed by Hermes; `abandoned_committed` requires a new ordinal
   and only a later exact zero-exit `committed` run closes the loop. An
   originally inactive service performs the journaled empty-attempt branch;
5. start an exact stopped target when reboot/updater recovery requires it, then
   repeat binding/file/health/real-task E2E and restore Hermes restart policy
   only after the fresh receipt; and
6. verify the same current-boot child hash, remove the timer hold drop-in and
   runtime mask, perform the final independently
   journaled daemon-reload, restore only
   the prior timer enabled/disabled and active/inactive states, fsync, and mark
   the journal `restored`. The static service is never restored to `active`;
   step 4's successful wrapper invocation replaces that transient state; any
   abandoned/repaired predecessors remain evidence. The immutable
   hold journal remains `complete`. Any crash/failure before step 6 leaves the
   timer disabled and persistently inhibited across reboot; recovery follows the
   exact InvocationID/container matrix and never guesses prior enablement.

Hermes proof is health, Korean nonstream response, SSE `[DONE]`, one real agent
task with tool-start/tool-complete, per-key attempt evidence, one-key exclusion
with task success, Hermes restart with repeat success, rollback/reapply, and
plaintext-secret scan. Every real task receipt binds its journal E2E UUID,
server-returned run ID, boot ID, container ID, dispatch time, ordered tool event
hashes, and terminal hash; an unknown dispatch or missing event is never
replayed or counted. Hermes never receives an NVIDIA credential.

## 14. Fast feedback, full candidate gate, reviews, and release order

During implementation, `scripts/qa/affected` maps changed paths to the smallest
sound Rust crate, TS/Svelte package, SQL migration test, browser journey, or ops
verifier. The exact failure runs while its cause changes, then its directly
affected group. Workspace-wide, full browser, container, secret, and live gates
do not run after each edit.

One source revision is frozen into a source manifest and immutable app/PG
generation. The complete gate runs once for that candidate; a source change
creates a new candidate and reruns it only when focused verification is green:

- Rust format, Clippy `-D warnings`, workspace/unit/doc/integration/property/
  concurrency tests, dependency/license/unsafe audits;
- frozen Bun install, Biome, strict TypeScript, pure-validator drift,
  `svelte-check`, component tests, knip, and production build;
- fresh and legacy-import SQLx migrations, real PostgreSQL race/fault tests,
  generation backup, isolated restore, activation crash matrix, and rollback;
- differential framework-neutral oracle fixtures and proof that retired runtime
  artifacts are absent from the image;
- fake NVIDIA JSON/SSE/202, every modality and representation, asset lifecycle,
  deterministic inline-to-asset boundaries, delayed list visibility that keeps
  create-unknown/no-alternate while freeing staged custody through quarantine,
  malformed/oversized and quality-degenerate media, exact 1,024-vector/
  1024x1024-image/video-duration-frame/audio-RMS oracles, canonical SSE grammar,
  global/profile health sequence races, RR, cooldown, credential/profile
  classification, safe/unsafe failover, every adjacent terminal-seal race and
  pre-seal request-future/body Drop, post-seal downstream handoff/body Drop,
  cancellation, restart, and secret
  canaries;
- two fresh browser runs at 320/375/768/1280, native 200% and native 400%,
  plus text-spacing/forced-colors/reduced-motion variants, deterministic DOM/state,
  axe/focus/overflow, cold Lighthouse performance/accessibility/best-practices/
  SEO 100, login, empty 0->1->2 onboarding, first client issue/revoke,
  slot disable/enable, CSP/bootstrap failure, offline/conflict/replacement/
  automatic and manual clipboard cleanup, operation-recovery-ledger reload,
  authenticated loading, stale mutation locking, advertised-but-temporarily-
  unavailable Models state, unknown-enum fail-closed, exact direct Evidence,
  clipboard-unavailable revoke escape, reset terminal recovery, custody
  Back/Forward guard, logout/login/pagehide epoch races, screenshot review, and
  cleanup zero; and
- source/history/image secret scan, dependency/image scan, Compose/config render,
  app/DB-init/steady-DB capability and UID/GID inspection, healthcheck,
  PostgreSQL fresh/existing/partial-volume and two-role SCRAM validation,
  envelope all-or-none and forced nonce-collision/exhaustion, immutable registry
  and generation binding, hold reboot persistence plus prior-active updater
  catch-up and stopped-Hermes recovery, updater-child full-runtime lock
  competition, backup snapshot-owner loss/new-ID and local/two-SFTP exclusive-
  open crash adoption, restore all-before/all-after/mixed rejection,
  authority-pointer absent/tombstone bootstrap CAS, and tunnel/DNS request-loss,
  late adopt/delete, manual-absence, and rollback journals.

Browser evidence is produced only by lockfile-pinned `@playwright/test` 1.61.1
(`sha512-8nKv6+0RJSL9FE4jYOEGXnPeM/Hg12qZpmqzZjRh3qM0Y7c3z1mrOTfFLids72RDQYVh9WpLEfR5WdpNX4fkig==`)
and its managed Chromium revision 1228, Chrome for Testing 149.0.7827.55. The
Ubuntu executable is 278,568,152 bytes with SHA-256
`2d18db9d8608b052b6a552ee00ec1e830f93692e928b65ecc67d693bd33fe801`.
Dependency preparation downloads it before candidate freeze; the gate verifies
these values and forbids browser download, system Chrome, `channel`, executable
override, or fallback.

Each of the two runs starts only when its complete evidence root is absent,
creates a new browser profile, and records source commit, source-manifest hash,
app image digest, asset-manifest hash, schema generation, Playwright/package
integrity, browser revision/version/executable hash, locale `ko-KR`, timezone
`Asia/Seoul`, color scheme, reduced-motion/forced-colors setting, viewport,
DPR, zoom, capture ID, fixture-state ID, and monotonic sequence. The ordered
capture IDs are:

```text
01-login-disabled, 02-login-error-focus, 03-login-success-empty-overview,
04-slot1-enter-empty, 05-slot1-probe-running-cleared,
06-slot1-cancel-confirm, 07-slot1-cancelled-retry,
08-slot1-transient-retry, 09-slot1-invalid-delete-confirm,
10-slot1-deleted-focus-return, 11-slot1-probe-recovered, 12-slot1-review,
13-slot1-assigned, 14-slot2-enter-empty, 15-slot2-pair-probe-running-cleared,
16-slot2-review-common-voice, 17-slot2-assigned-traffic-ready,
18-client-issue-validation, 19-client-secret-present-no-png,
20-client-secret-cleared, 21-client-revoke-confirm, 22-client-revoked,
23-slot-disable-confirm, 24-routing-one-key-degraded, 25-slot-enable-recovered,
26-replacement-enter-empty, 27-replacement-probe-running-cleared,
28-replacement-failed-delete-confirm, 29-replacement-restarted,
30-replacement-review, 31-replacement-success, 32-overview-ready,
33-overview-attention, 34-clients-list, 35-models-ready,
36-models-unavailable, 37-evidence-attention, 38-operation-unknown-reconcile,
39-offline, 40-conflict, 41-csp-bootstrap-failure,
42-validator-handler-throw, 43-create-unknown-quarantine,
44-clipboard-manual-overwrite, 45-session-epoch-late-response-discard,
46-invalid-pair-reset-confirm, 47-intent-prepared-abandon,
48-intent-executing-cross-tab-reconcile, 49-lost-client-secret-revoke,
50-route-history-focus-restored, 51-routing-capacity-wait,
52-authenticated-loading, 53-stale-mutation-locked,
54-model-advertised-currently-unavailable, 55-unknown-enum-fail-closed,
56-direct-evidence-loading, 57-direct-evidence-success-result,
58-direct-evidence-close-focus-return, 59-direct-evidence-not-found,
60-direct-evidence-error, 61-direct-evidence-retry-success,
62-direct-evidence-history-transition,
63-clipboard-unavailable-revoke-pending-no-png,
64-clipboard-revoked-dialog-closed, 65-reset-reload-recovery,
66-reset-cancel-cutoff, 67-reset-terminal-mismatch-fail-closed,
68-reset-terminal-zero-slots,
69-custody-back-navigation-blocked-no-png,
70-custody-forward-navigation-blocked-no-png,
71-login-h1-title-skip-target, 72-bootstrap-failed-h1-title,
73-partial-secondary-state, 74-degraded-public-route-attention,
75-wait-system-before-deadline, 76-wait-system-deadline-refresh,
77-wait-system-management-ready, 78-external-fix-dialog,
79-direct-evidence-live-state-announcements, 80-mobile-safe-area-geometry,
81-route-scroll-manual-single-restore
```

Captures 19, 63, 69, and 70 record only custody/zero-network or revoke-pending
assertions and must have no PNG. Captures 56 and 57 prove that the loading body
is replaced in the same focused dialog; 58 proves connected-trigger/row-heading
focus return. Captures 59..61 independently prove exact 404, general error, and
safe-GET retry success, while 62 proves dialog close then Evidence route/title/
h1/history mode. Captures 65..68 separately bind same-operation reload recovery,
the irreversible cancel cutoff, a forged/mismatched terminal result that locks
all mutation and opens Evidence, and the valid zero-slot terminal. Captures 69
and 70 are distinct Back and Forward attempts during plaintext custody; both
must preserve the dialog, URL/history index, zero owner-route transition, and
zero screenshot. Captures 71 and 72 bind the pre-auth/bootstrap-failure h1,
skip target, and document title. Capture 73 binds the partial secondary-state
copy/action; 74 binds public-route degraded Attention/evidence; 75 and 76 bind
wait-system before/at its monotonic deadline with exactly one GET; 77 binds the
management-ready transition; 78 binds external-fix copy, focus, Escape, and
zero pre-confirmation network; 79 binds one live-region announcement for each
direct-Evidence body state keyed by target kind/ID; 80 binds both safe-area
insets and focus geometry; and 81 binds manual scroll restoration with one
Back/Forward restore. All other required captures bind DOM bytes and PNG bytes where visually
meaningful. Fixtures own a fixed clock, UUIDs, request IDs, and counters; DOM
canonicalization may only normalize CRLF to LF, serialize attributes in
lexicographic `(namespace,name)` order, and omit Playwright's own transient
node handles. It may not replace text, timestamps, IDs, classes, styles,
visibility, order, dimensions, or accessibility attributes.

A separate Rust verifier, not either test run, reopens and SHA-256 rehashes every
receipt/DOM/PNG, validates candidate bindings and capture order, compares the
two canonical DOM and PNG hashes, and checks the allowed no-PNG exception.
Cleanup receipts then prove browser/context/profile closed, target-owned PIDs,
ports, containers, temporary directories, and clipboard references zero while
retaining the immutable evidence roots. A run's self-reported PASS is never the
comparison authority.

The release image has one nonpublic live-QA control on the already root-peer
Unix control transport; it is unavailable over HTTP and accepts an additional
action only while a root:root 0400, nlink-one, generation-bound
`/run/nvidia-build-lb/live-qa-permit.json` exists. The closed command arms one
30-second, one-use permit for an exact run UUID, key UUID, profile ID, and fault
`pre_dispatch_connect_failure|received_429`:
`{version:1,transaction_id:UUID,action:"arm_live_qa_fault",run_id:UUID,
permit_id:UUID,key_id:UUID,profile_id:ProfileId,
fault:"pre_dispatch_connect_failure"|"received_429",
expected_configuration_generation:Decimal}`. The command and permit contain no
credential. A serializable row is durable before arming; the next matching
public reservation consumes it after selection and before provider bytes. The
connect class records a zero-upstream-byte safe transport terminal; the 429
class records a synthetic, explicitly QA-tagged fully received 429 terminal
and applies the ordinary rate cooldown. Both then exercise the ordinary
alternate selector and must succeed on the other key. A permit cannot target a
probe/Hermes/internal request, cannot alter a provider response, expires
closed, and is never recreated after restart. The final live receipt proves
every permit consumed/expired, the permit file and control action unavailable,
and zero active QA rows before Hermes production evidence. These tagged
attempts remain immutable evidence but never masquerade as NVIDIA responses.

`live_qa_fault_permits` has primary key `permit_id`, unique
`(run_id,key_id,profile_id,fault)`, the exact command fingerprint, 30-second
`expires_at`, nullable `consumed_attempt_id`, and state
`armed|consumed|expired`. It foreign-keys the key/profile and consumed attempt,
is included in backup/state projections, and has a partial uniqueness constraint
allowing only one armed row globally. Reservation locks it after the normal
configuration/profile/key selection locks and before inserting the attempt;
consumption and the synthetic terminal commit atomically. Startup expires but
never consumes an old armed row. No DELETE path exists; "zero active QA rows"
means zero `armed`, while immutable consumed/expired rows remain auditable.

Each unchanged-source real-NVIDIA run uses this exact order and allocates a new
run UUID; a later row cannot compensate for an earlier failure:

1. Revalidate hold/revocation, generation/image/schema/profile/evaluator/suite
   digests, two fresh distinct fingerprinted keys, no active QA permit, and
   `codex-lb` health. With Key A forced and alternates forbidden, execute all
   seven profile case groups in manifest order, including every quality
   sub-attempt; then repeat independently with Key B. Every attempt receipt
   names only run/attempt/key/profile/case IDs, fingerprint, status class,
   latency, contract/suite/evaluator/shape/quality/predicate/cleanup digests,
   timestamps and `upstream_bytes_maybe_sent`; it contains no request/media/
   response/header/asset/secret field.
2. Commit Slot 1 then Slot 2 and the pair proofs. For each profile in manifest
   order, send two minimal valid public requests from one normal downstream
   credential. The persisted cursor is initially Slot 1, so attempt receipts
   must be exactly Slot 1 then Slot 2 and both responses must satisfy the public
   schema/quality predicate. No admin/probe/quality sub-attempt advances the
   public cursor.
3. Disable Slot 1 and run one minimal request for every profile; each must use
   Slot 2. Re-enable and prove current proof unchanged. Disable Slot 2 and
   repeat through Slot 1, then re-enable. Next, align each cursor to Slot 1,
   consume a `pre_dispatch_connect_failure` permit for every profile, and
   require failed Slot 1 attempt followed by one successful Slot 2 attempt.
   For GLM additionally consume `received_429`, prove the exact local cooldown,
   one successful alternate, pre-expiry exclusion with zero Slot-1 attempt, and
   post-expiry Slot-1 recovery without a manual clear.
4. Exercise every public route with exact valid and invalid schema cases,
   `/v1/models`, nonstream JSON, GLM/Phi/VILA SSE through client-observed finish
   then `[DONE]`, embeddings, image, speech, and native video. For each stream,
   the client terminal receipt, gateway tracked-body terminal, upstream attempt
   terminal, and delivery receipt must share request/attempt IDs and ordering;
   a server-side terminal without client receipt is failure.
5. Persist the per-profile next cursor, current proof revisions, pair/map
   digests, cooldown history, attempt/event IDs, and configuration generation.
   Restart only the app, verify exact readback and continue the expected next
   slot; then controlled-restart only PostgreSQL plus the app dependency,
   verify the same projection and next slot again. No proof/cursor is rebuilt
   from memory. Run the isolated backup/restore/rollback matrix and recheck the
   same logical state-forward projection.
6. Verify the loopback and public hostname route/schema, secret-free admin DOM/
   screenshot/network-summary receipts, log/journal/DB evidence projections,
   and negative canary scan. Remove the live-QA permit, prove the control action
   rejected and zero active permits, then perform the Hermes sequence in
   section 13.1, including pair distribution, one-key exclusion/failover,
   Hermes restart, and a second completed real tool task.

The ordered run is PASS only when every step has one terminal receipt and the
final run manifest rehashes all children. A real provider 429 may be retained as
additional evidence but is never required or induced by wasteful traffic; the
closed synthetic 429 proves rate-aware routing without evading or exhausting a
provider limit.

The reboot/recovery oracle is explicit rather than implied by one restart. For
each of forward cutover, rollback/reapply, and hold-release, QA performs at
least two controlled host reboots on the unchanged source. Each lineage must
show `boot_epochs` exactly `0,1,2`, `recovery_e2e` exactly contiguous ordinals
`1,2` (plus any separately required same-boot `post_updater` row), a
supersession receipt from every row to the immediately prior row, and no
rewritten predecessor. After epoch 2, the accepted receipt must name the
current boot ID, current container ID, latest updater/container tuple where
applicable, and all six health/nonstream/SSE/tool-start/tool-complete/terminal
receipts. The same three sequences are run once with a same-boot accepted
Watchtower/updater container replacement to require a `post_updater` ordinal;
ordinary deterministic schema/static checks are not repeated. A missing,
gapped, branched, stale-boot, stale-container, or incomplete-receipt lineage
is failure even if the task response itself succeeded.

Timing, race, restart, cooldown, fairness, ambiguous commit, cleanup, and release
recovery run ten times only where repetition detects defects. Deterministic
static/schema/type checks do not. The unchanged-source live matrix runs to three
consecutive complete PASSes and includes both fresh keys independently, every
profile proof case, inline and asset media, streaming/nonstream, distribution,
disable/failover/recovery, restart persistence, backup/restore/rollback, public
domain, secret nonexposure, `codex-lb` before/after, and Hermes.

The ordered gate is:

1. Codex authors the complete design; independent read-only PdM, Rust, Svelte/
   accessibility, PostgreSQL/secrets, operations/Hermes, and multimodal QA
   reviewers each return exact `VERDICT: LGTM_NO_BLOCKING`. Valid blockers are
   repaired and the complete new snapshot is independently re-reviewed.
2. Codex implements only `scripts/ops/compromised-hold.sh` and its no-root
   fixture/syntax tests, verifies and hashes them, then the operator enters the
   hold. Both exposed credential IDs are revoked/deleted in the NVIDIA control
   plane, the two closed receipts are confirmed, and hold status must be exact
   `complete`. No other implementation or provider call precedes this gate.
3. Codex directly implements the remaining source, tests, runtime, scripts, and docs.
4. Independent read-only implementation and user-journey reviewers report no
   blocker; Codex repairs valid findings and re-verifies affected areas.
5. Codex freezes and runs the complete local candidate gate, commits/pushes the
   complete verified app branch, updates Draft PR #1 evidence, requests
   `@codex review`, and waits. Valid comments are fixed, reviewed, verified,
   pushed, and re-reviewed until the unchanged head is LGTM.
6. The user merges the app PR. The exact merged commit publishes the immutable
   app digest.
7. Codex pins the merged app digest, PostgreSQL/tunnel digests, and generation
   contract in the independent `home-server-infra` branch, verifies it, opens a
   Draft PR, and repeats the Codex review loop. The user merges it.
8. Only merged source is staged. The existing two revocation receipts and hold
   journal are revalidated (provider revocation is not deferred or repeated),
   then legacy import, fresh-key registration, Cloudflare DNS, live matrix, and
   Hermes cutover proceed. Hold release remains gated on the successful initial
   Hermes E2E and binding contract.

If a live defect is in app source, work returns through a new app PR, user
merge, image publication, and infra repin PR. If it is infrastructure, it
returns through a new infra PR and user merge. There is no direct production
hotfix or unmerged deployment. Missing user merges, fresh credentials, provider
revoke receipt, or off-host custody configuration are explicit external gates;
they do not permit partial Goal completion.

## 15. Final evidence index

The final evidence manifest binds source SHA, app and PostgreSQL registry
digests, unauthenticated GitHub public-repository/API/ls-remote proof,
unauthenticated public GHCR manifest/config pull, merged-commit publication
workflow/provenance, infra SHA, schema/profile/asset snapshots, DB/key generation, both PR
URLs and current-head review verdicts, focused and full test receipts, browser
DOM/screenshot/cleanup receipts, all three live runs, both non-secret key IDs/
fingerprints, per-profile attempt IDs, RR/failover/restart observations,
provider revoke receipt, backup/restore/rollback IDs, Cloudflare DNS/tunnel
checks, compromised-hold journal/receipts, secret-free admin-token handoff
receipt, `codex-lb` before/after health, and Hermes task/tool evidence. It contains
no bearer, plaintext key/token, ciphertext, nonce, media, prompt, raw provider
body, query, secret path content, HAR, trace, or authenticated video.

### 15.1 Release-recovery timing and capacity invariants

The release checker is fail-closed and uses one stable runtime lock. There is
**No intermediate cap value**: a forward recovery either commits the complete
requested capacity tuple or leaves intake withdrawn. **Permanent evidence blockers never enter this path**, and the recovery decision is **independent of eligible-key readiness**. The checker covers an **empty first-run** and is a
**target-image-independent host checker**; it never relies on a
**legacy-overview fallback**.

The first host health sample has a **two-second absolute deadline**. The second exact sample 30 seconds later must agree with the same
container generation before operational evidence is accepted. This timing
contract applies to the Rust image as well as the retained QA surface.
