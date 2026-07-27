# Privacy

Agent Console processes communications data selected by its operator. A
deployment may handle message text, message and conversation identifiers,
participant display names and platform identifiers, media, moderation events,
knowledge documents, memories, prompts, replies, and operational metadata.

## Storage

The server stores configured product data in PostgreSQL, Redis, Qdrant, and
operator-selected media directories. The Windows companion may store local
message queues, decrypted media, authentication state, and derived indexes
under `wxbot_client/data/`. These locations must not be committed, included in
container build contexts, or shared as diagnostics.

Operators are responsible for access controls, retention periods, backups,
deletion requests, and applicable notices or consent for the conversations they
connect.

## Logging

Application logs are intended to contain event names, opaque identifiers,
counts, durations, status codes, and redacted errors. Raw message text, prompts,
addresses, authentication payloads, tokens, hardware identifiers, and local
file paths must not be logged. Treat logs as sensitive even when redaction is
enabled.

## External services

LLM, embedding, map, image-generation, webhook, tracing, and message-platform
providers receive data only when an operator configures and enables them.
Organization-specific feeds and report branding are disabled by default.
Review each provider's data handling terms before enabling it.

The optional Windows companion remote-authorization flow requires explicit
device-binding consent. It sends a pseudonymous device identifier and a generic
device label; it must not send raw MachineGuid, BIOS UUID, CPU ID, disk serial,
hostname, SystemRoot, or an unredacted hardware fingerprint.

## Security and privacy reports

Use the private process described in `SECURITY.md`. Never attach live messages,
credentials, databases, screenshots, or decryption keys unless maintainers
provide an approved secure transfer channel.
