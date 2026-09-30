# Feature Specification: Deleted Conversation File Purge

**Feature Branch**: `feat/014ai-file-purge`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AI1: A deleted conversation remains unreadable immediately. After active file work has ended and its deadline has passed, the scheduler requests idempotent generated-job output purge from the private worker. It records completion durably; restart repeats the safe purge until complete.
- FR-AI2: The authenticated worker purge route refuses an active job and deletes only a validated generated ULID output directory. It does not expose file names or content and works after worker restart when the in-memory job ledger is empty.
- FR-AI3: API maintenance removes generated originals and attachment quota/jobs/metadata only when all derived jobs are marked purged. Deletion is idempotent after partial filesystem/DB failure. Existing text purge then removes conversation content and marks the tombstone.
- FR-AI4: Abandoned generated staging files are removed by a bounded age check. Operations emit content-free metrics/spans and are covered by worker, scheduler and API cleanup tests. Chat remains default-off until remaining feature gates pass.
