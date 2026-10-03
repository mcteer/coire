# Feature Specification: Image job capacity reservation

**Feature Branch**: `feat/015g5-image-job-capacity`
**Parent**: `specs/015-image-generation/` (part of T026/T035/T041)
**Dependency**: draft PR #73

## Goal

Reserve queue slots, daily output allowance and worst-case output bytes in one transaction before accepting a generation job.

## Acceptance

1. Global then owner quota rows are locked under the existing transaction advisory lock. An owner may hold at most four pending jobs, the cluster at most 32, and accepted outputs stay within the owner daily allowance. Limits use configured values and never silently clamp.
2. Each new job reserves `n * image_output_max_bytes` as a storage hold and checks both quota limits and the physical safety floor against all outstanding holds.
3. The job lifecycle can move a pending reservation to started exactly once, consuming the daily output allowance, or release a pending reservation on cancellation. Terminal work releases its outstanding byte hold only after publication or confirmed no retained bytes in the later execution path.
4. UTC day rollover resets consumed outputs but preserves held outputs for jobs crossing midnight. Failed checks leave counters unchanged. The helper does not open a public route or enable image admission.
5. Tests cover owner/global queue bounds, daily rollover, storage/quota refusal and counter transitions.
