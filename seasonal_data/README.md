# Seasonal data

This directory contains public, general seasonal metadata only. It must never
contain account details, character data, credentials, saved lots, orders, or
conversation data.

Each YAML record has a `schema_version` and `data_version`. Populate a record
only from current, verifiable HTTPS sources, record its `checked_at` date, then
set `confirmation_status: confirmed`. The application refuses to create a
description preview from `unconfirmed` or `superseded` data. Empty starter
records therefore remain safe to publish and cannot make claims about live
rewards.

For confirmed Mythic+ records, reward and crest keys must use `key_<level>`.
The preview generator selects only the requested key level and refuses stale
records or missing exact values.
