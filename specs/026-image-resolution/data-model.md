# Data model

ImageSpec, ImageSubmitRequest and ImageCapabilityProfile dimensions accept integer
output pixels. A shared helper calculates ceil16 native dimensions; bounded padded
area is validated. No new recipe fields: requested dimensions remain exact outputs;
internal alignment is deterministic and documented.

ImageResolutionQualificationRequest: requested maximum dimensions, bounded native
reservation request; schema extra=forbid. ImageResolutionQualificationReceipt: job
identity and model identity, durable stage. ImageAssetValidateRequest gains optional
qualification bounds; ImageAssetValidationResult gains content-free measured size
records. Existing requests omit these and retain legacy validation.

DownloadJobRow.image_validation retains requested bounds, qualification discriminator,
prior usable profile/memory and pending reservation cleanup. Per-copy validation results
are persisted before expanding current capability. No database migration.
