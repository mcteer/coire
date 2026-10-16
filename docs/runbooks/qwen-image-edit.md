# Qwen photo editing

Choose **Qwen Image Edit 2509 (Q8)** in Images. Its mode is **Edit a photo**. Upload a
source photo, choose output size/aspect ratio and describe the changes. For a portrait,
ask to retain the same person's facial features, hair and appearance while changing the
setting, clothing or pose. Review the likeness before using the result. This workflow
uses the reference photograph itself, rather than Canny outlines. It improves consistency
but does not guarantee identity fidelity.

The source and output sizes are independent. Source ownership, digest, normalized image
limits and authenticated private transfer remain enforced. The original source dimensions
are retained in the saved recipe. A source upload uses the existing `init` purpose; the
new `edit` mode requires `init_image_id` and rejects denoising strength, masks, ControlNet,
LoRAs and upscaling. Those belong to other supported workflows. Output dimensions are
padded internally to 16px and cropped back to the exact requested dimensions.

The quality default is 40 steps, guidance 4, one 1024×1024 output. The size picker advertises
only capability measured on both Studio copies, including 1980×1980 and both non-square
orientations. Four-step expanded-size samples exercise bounds and memory; they are not a
quality recommendation. Use the default 40 steps for actual photo editing. Cancellation,
queue/progress feedback, saved recipe reuse, preview/download, history deletion and output
bulk deletion use the existing owner-scoped image paths.

## Reviewed model and native runtime

- Artifact: `mflux-community/qwen-image-edit-2509-mflux-q8`.
- Reviewed source revision: `9e6e769beaf66eff466272e782668cf52d356407`.
- Base: `Qwen/Qwen-Image-Edit-2509`, Apache 2.0. No new Python/JavaScript dependency.
- Existing pinned native mflux 0.20.0 runs QwenImageEdit directly on the Studios. Do not
  advertise 2511: the pinned implementation does not implement that newer architecture.
- The Q8 conversion was saved with 0.22; source comparison confirmed the 2509 architecture,
  native weight layout and tokenizer are compatible candidates for 0.20. Runtime qualification
  remains mandatory. Transformer is 8-bit; the vision/text encoder and VAE retain their
  stored unquantized precision. Both component indexes/tokenizer files and exact local
  manifest/shards are required. Arbitrary configuration-free repositories are not allowed.
- Loading is offline with no Hub credentials. Only the audited admin acquisition pipeline
  downloads model weights. The resident pipeline never acquires weights or uses Core for
  inference. Reference-dependent embeddings do not enter the Z-Image prompt-only cache.

## Operations and rollback

Acquisition reserves memory and validates both existing replicas before publication. The
validator runs a 40-step 1024px edit and 4-step 1980px square/landscape/portrait edits using an
owned geometric reference with different dimensions. Every sample checks RGB content,
nondegenerate pixels and exact output size. A physical-memory watchdog terminates only
the owned validation child if its reservation is exceeded or measurement is unavailable.

Watch image job progress, `coire_image_validation_total`, the base/photo-edit qualification
dashboard panel and `CoireImageBaseOrPhotoEditQualificationFailed`. Existing node-stage,
publication, transfer and classification alerts apply. Admin model detail retains both-copy
validation results. Cancel through authenticated job APIs; unload only an idle image worker
through `DELETE /api/v1/admin/image-workers/{instance_id}`. Never kill unrelated engines.

Rollback requires the retained API/scheduler/web image pins and immutable node baseline.
Drain active edit jobs before reverting contracts; old runtimes cannot accept `edit` mode.
Hide the Qwen model before rolling back. Keep model copies private if validation fails.
No database migration is needed. Do not weaken image safety or entitlement checks during
rollout; the [existing content guard limitations](image-child-safety.md) still apply.

Official references: [model](https://huggingface.co/Qwen/Qwen-Image-Edit-2509),
[Q8 artifact](https://huggingface.co/mflux-community/qwen-image-edit-2509-mflux-q8),
[native implementation](https://github.com/mflux-community/mflux/blob/v.0.20.0/src/mflux/models/qwen/variants/edit/qwen_image_edit.py).
