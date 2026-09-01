# Data Model: Model Instances and Cluster State

## `model_instances`

`id UUID PK`; model/variant/placement decision FKs; policy; `instance_state`; failure fields;
in-flight count; lifecycle timestamps; optional drain deadline. Index `(model_id,state)`.

## `instance_members`

One row per rank: instance, node, rank, optional engine/reservation, host and port. Unique
`(instance_id,rank)` and `(instance_id,node_id)`.

## `instance_transitions`

Bigserial id, instance, per-instance sequence, previous/current state, reason and timestamp. Append-only.

## Registration additions

Nodes gain declaration/token lifecycle and health observation/GPU fields. `registration_attempts`
stores node name, outcome, reason, agent version, remote identity and time—never the token.

## State rules

`requested -> reserving -> launching -> warming -> ready -> draining -> stopped`; any non-terminal
state may fail. Each transition appends an event atomically. Unreachable nodes project ready instances
as unavailable without falsifying persisted lifecycle state.

## Cluster projection

Nodes include observed health, CPU/GPU/thermal, budgets, reservations and member instances. Instances
include model/variant, persisted/effective states, policy, ranks, in-flight and last transition.
